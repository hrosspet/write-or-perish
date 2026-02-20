import { useState, useCallback, useRef, useEffect } from 'react';
import { useAsyncTaskPolling } from './useAsyncTaskPolling';
import api from '../api';

/**
 * useConversation — Manages a Converse workflow conversation.
 *
 * Returns: { messages, conversationId, isWaitingForAI, latestLlmNodeId, sendMessage, reset }
 */
export function useConversation() {
  const [conversationId, setConversationId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [isWaitingForAI, setIsWaitingForAI] = useState(false);
  const [latestLlmNodeId, setLatestLlmNodeId] = useState(null);
  const [pendingLlmNodeId, setPendingLlmNodeId] = useState(null);
  const conversationIdRef = useRef(null);

  // Poll for LLM completion when waiting
  const { data: llmData, status: llmStatus } = useAsyncTaskPolling(
    pendingLlmNodeId ? `/nodes/${pendingLlmNodeId}/llm-status` : null,
    { enabled: !!pendingLlmNodeId, interval: 1500 }
  );

  // When LLM completes, update messages
  useEffect(() => {
    if (llmStatus === 'completed' && llmData?.content) {
      setMessages(prev => prev.map(m =>
        m.id === pendingLlmNodeId
          ? { ...m, content: llmData.content, llm_task_status: 'completed' }
          : m
      ));
      setLatestLlmNodeId(pendingLlmNodeId);
      setPendingLlmNodeId(null);
      setIsWaitingForAI(false);
    } else if (llmStatus === 'failed') {
      setMessages(prev => prev.map(m =>
        m.id === pendingLlmNodeId
          ? { ...m, content: 'Response failed. Please try again.', llm_task_status: 'failed' }
          : m
      ));
      setPendingLlmNodeId(null);
      setIsWaitingForAI(false);
    }
  }, [llmStatus, llmData, pendingLlmNodeId]);

  const sendMessage = useCallback(async (content) => {
    if (!content.trim()) return;

    // Add user message optimistically
    const tempUserMsg = {
      id: `temp-user-${Date.now()}`,
      role: 'user',
      content,
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, tempUserMsg]);
    setIsWaitingForAI(true);

    try {
      let res;
      if (!conversationIdRef.current) {
        // Start new conversation
        res = await api.post('/converse/start', { content });
        const convId = res.data.conversation_id;
        setConversationId(convId);
        conversationIdRef.current = convId;
      } else {
        // Continue conversation
        res = await api.post(`/converse/${conversationIdRef.current}/message`, { content });
      }

      const { user_node_id, llm_node_id } = res.data;

      // Replace temp user message with real one
      setMessages(prev => {
        const updated = [...prev];
        const tempIdx = updated.findIndex(m => m.id === tempUserMsg.id);
        if (tempIdx >= 0) {
          updated[tempIdx] = { ...updated[tempIdx], id: user_node_id };
        }
        // Add placeholder AI message
        updated.push({
          id: llm_node_id,
          role: 'assistant',
          content: '',
          created_at: new Date().toISOString(),
          llm_task_status: 'pending',
        });
        return updated;
      });

      setPendingLlmNodeId(llm_node_id);

    } catch (err) {
      console.error('Send message error:', err);
      setMessages(prev => prev.filter(m => m.id !== tempUserMsg.id));
      setIsWaitingForAI(false);
    }
  }, []);

  const reset = useCallback(() => {
    setConversationId(null);
    conversationIdRef.current = null;
    setMessages([]);
    setIsWaitingForAI(false);
    setLatestLlmNodeId(null);
    setPendingLlmNodeId(null);
  }, []);

  return {
    conversationId,
    messages,
    isWaitingForAI,
    latestLlmNodeId,
    sendMessage,
    reset,
  };
}
