import React, { useState, useRef, useEffect, useCallback } from 'react';
import { useConversation } from '../hooks/useConversation';
import { useStreamingTTS } from '../hooks/useStreamingTTS';
import { useStreamingTranscription } from '../hooks/useStreamingTranscription';

function Message({ message }) {
  const isUser = message.role === 'user';
  const isPending = message.llm_task_status === 'pending' || message.llm_task_status === 'processing';

  return (
    <div style={{
      display: 'flex',
      justifyContent: isUser ? 'flex-end' : 'flex-start',
      marginBottom: '16px',
    }}>
      <div style={{
        maxWidth: '75%',
        padding: '12px 16px',
        borderRadius: '12px',
        background: isUser ? 'var(--accent-subtle)' : 'var(--bg-card)',
        border: isUser ? 'none' : '1px solid var(--border)',
      }}>
        {isPending && !message.content ? (
          <div style={{
            display: 'flex',
            gap: '4px',
            padding: '4px 0',
          }}>
            {[0, 1, 2].map(i => (
              <span key={i} style={{
                width: '6px', height: '6px', borderRadius: '50%',
                background: 'var(--text-muted)',
                animation: `typingDot 1.2s ease-in-out ${i * 0.2}s infinite`,
              }} />
            ))}
            <style>{`
              @keyframes typingDot {
                0%, 60%, 100% { opacity: 0.3; }
                30% { opacity: 1; }
              }
            `}</style>
          </div>
        ) : (
          <div style={{
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 300,
            color: isUser ? 'var(--text-secondary)' : 'var(--text-muted)',
            lineHeight: 1.6,
            whiteSpace: 'pre-wrap',
          }}>
            {message.content}
          </div>
        )}
      </div>
    </div>
  );
}

export default function ConversePage() {
  const { messages, isWaitingForAI, latestLlmNodeId, sendMessage } = useConversation();
  const [inputText, setInputText] = useState('');
  const [isVoiceRecording, setIsVoiceRecording] = useState(false);
  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);

  // Auto-scroll to bottom on new messages
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Streaming TTS for AI responses
  const tts = useStreamingTTS(
    latestLlmNodeId ? latestLlmNodeId : null,
    { autoPlay: true }
  );

  // Start TTS when new AI response arrives
  const lastTTSNodeRef = useRef(null);
  useEffect(() => {
    if (latestLlmNodeId && latestLlmNodeId !== lastTTSNodeRef.current) {
      lastTTSNodeRef.current = latestLlmNodeId;
      tts.startTTS();
    }
  }, [latestLlmNodeId]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSend = useCallback(() => {
    const text = inputText.trim();
    if (!text || isWaitingForAI) return;
    sendMessage(text);
    setInputText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [inputText, isWaitingForAI, sendMessage]);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  // Auto-grow textarea
  const handleInputChange = (e) => {
    setInputText(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  };

  // Voice input via streaming transcription
  const voiceStreaming = useStreamingTranscription({
    privacyLevel: 'private',
    aiUsage: 'chat',
    onTranscriptUpdate: (text) => {
      setInputText(text);
    },
    onComplete: (data) => {
      const text = data.content || '';
      setInputText(text);
      setIsVoiceRecording(false);
      if (text.trim()) {
        sendMessage(text);
        setInputText('');
      }
    },
  });

  const handleVoiceToggle = () => {
    if (isVoiceRecording) {
      voiceStreaming.stopStreaming();
    } else {
      setIsVoiceRecording(true);
      voiceStreaming.startStreaming();
    }
  };

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: 'calc(100vh - 60px)',
      maxWidth: '800px',
      margin: '0 auto',
      padding: '0 16px',
    }}>
      {/* Messages area */}
      <div style={{
        flex: 1,
        overflow: 'auto',
        padding: '24px 0',
      }}>
        {messages.length === 0 && (
          <div style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            opacity: 0.5,
          }}>
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1">
              <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
            </svg>
            <p style={{
              fontFamily: 'var(--serif)',
              fontStyle: 'italic',
              fontSize: '1rem',
              color: 'var(--text-muted)',
              marginTop: '16px',
            }}>
              Ask anything. Think out loud.
            </p>
          </div>
        )}

        {messages.map((msg) => (
          <Message key={msg.id} message={msg} />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Input bar */}
      <div style={{
        borderTop: '1px solid var(--border)',
        padding: '16px 0',
        display: 'flex',
        gap: '8px',
        alignItems: 'flex-end',
      }}>
        {/* Mic button */}
        <button
          onClick={handleVoiceToggle}
          style={{
            width: '40px',
            height: '40px',
            borderRadius: '50%',
            border: isVoiceRecording ? '2px solid #dc3545' : '1px solid var(--border)',
            background: isVoiceRecording ? 'rgba(220,53,69,0.1)' : 'transparent',
            cursor: 'pointer',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          {isVoiceRecording ? (
            <svg width="16" height="16" viewBox="0 0 20 20" fill="#dc3545">
              <rect x="4" y="4" width="12" height="12" rx="2" />
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="var(--text-muted)">
              <path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z" />
              <path d="M17 11c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" />
            </svg>
          )}
        </button>

        {/* Text input */}
        <textarea
          ref={textareaRef}
          value={inputText}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          placeholder="Type a message..."
          rows={1}
          style={{
            flex: 1,
            background: 'var(--bg-input)',
            border: '1px solid var(--border)',
            borderRadius: '12px',
            color: 'var(--text-primary)',
            fontFamily: 'var(--sans)',
            fontSize: '0.9rem',
            fontWeight: 300,
            padding: '10px 16px',
            lineHeight: 1.5,
            resize: 'none',
            maxHeight: '160px',
          }}
        />

        {/* Send button */}
        <button
          onClick={handleSend}
          disabled={!inputText.trim() || isWaitingForAI}
          style={{
            width: '40px',
            height: '40px',
            borderRadius: '50%',
            border: 'none',
            background: inputText.trim() && !isWaitingForAI ? 'var(--accent)' : 'var(--border)',
            cursor: inputText.trim() && !isWaitingForAI ? 'pointer' : 'not-allowed',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
            transition: 'background 0.2s ease',
          }}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill={inputText.trim() && !isWaitingForAI ? 'var(--bg-deep)' : 'var(--text-muted)'}>
            <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" />
          </svg>
        </button>
      </div>
    </div>
  );
}
