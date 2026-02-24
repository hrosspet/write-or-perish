import { useState, useCallback, useRef, useEffect } from 'react';
import { useStreamingTranscription } from './useStreamingTranscription';
import { useAsyncTaskPolling } from './useAsyncTaskPolling';
import { useTTSStreamSSE } from './useSSE';
import { useAudio } from '../contexts/AudioContext';
import { useMediaSession } from './useMediaSession';
import api from '../api';

// iOS devices can't autoplay audio regardless of warmup, and playing silent audio
// while the mic stream is active crashes Bluetooth headphones on multi-device setups.
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

/**
 * Shared hook for voice-based conversation workflows (Reflect, Orient, etc.).
 *
 * Manages the full cycle: recording → transcription → LLM → TTS playback.
 * Pages provide their own UI and any workflow-specific logic via callbacks.
 *
 * @param {Object} options
 * @param {string} options.apiEndpoint - API path to post transcripts to ('/reflect', '/orient')
 * @param {string} options.ttsTitle - Label for the audio player ('Reflection', 'Orient')
 * @param {Function} options.onLLMComplete - Called with (nodeId, content) when LLM response is ready
 */
/**
 * @param {Object} options
 * @param {string} options.apiEndpoint - API path to post transcripts to ('/reflect', '/orient')
 * @param {string} options.ttsTitle - Label for the audio player ('Reflection', 'Orient')
 * @param {Function} options.onLLMComplete - Called with (nodeId, content) when LLM response is ready
 * @param {number|null} options.initialLlmNodeId - Resume in processing phase, polling this LLM node
 * @param {number|null} options.initialParentId - Resume in ready phase with thread parent pre-set
 */
export function useVoiceSession({ apiEndpoint, ttsTitle = 'Audio', onLLMComplete, initialLlmNodeId = null, initialParentId = null }) {
  const audio = useAudio();
  const [phase, setPhase] = useState(initialLlmNodeId ? 'processing' : 'ready');
  const [llmNodeId, setLlmNodeId] = useState(initialLlmNodeId);
  const [isStopping, setIsStopping] = useState(false);
  const [hasError, setHasError] = useState(false);
  const transcriptRef = useRef('');
  const threadParentIdRef = useRef(initialParentId);
  const lastUserNodeIdRef = useRef(null);

  // TTS state
  const ttsTriggeredForNodeRef = useRef(null);
  const [ttsGenerating, setTtsGenerating] = useState(false);
  const firstChunkRef = useRef(true);

  // Silent audio loop for iOS lock-screen controls during recording.
  // Playing audio is required for Media Session API to activate on iOS.
  const silentAudioRef = useRef(null);

  const startSilentAudio = useCallback(() => {
    if (!isIOS) return;
    try {
      // Generate an infinite silent audio stream via Web Audio API.
      // Using a stream (no finite duration) prevents iOS from showing
      // a cycling progress bar on the lock screen.
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const oscillator = ctx.createOscillator();
      const gain = ctx.createGain();
      const dest = ctx.createMediaStreamDestination();
      gain.gain.value = 0;
      oscillator.connect(gain);
      gain.connect(dest);
      oscillator.start();

      const el = new Audio();
      el.srcObject = dest.stream;
      el.play().catch(() => {});
      silentAudioRef.current = { el, ctx, oscillator };
    } catch (_) { /* audio not available */ }
  }, []);

  const stopSilentAudio = useCallback(() => {
    if (silentAudioRef.current) {
      const { el, ctx, oscillator } = silentAudioRef.current;
      el.pause();
      el.srcObject = null;
      try { oscillator.stop(); } catch (_) {}
      ctx.close().catch(() => {});
      silentAudioRef.current = null;
    }
  }, []);

  // Stable ref for onLLMComplete to avoid effect re-runs
  const onLLMCompleteRef = useRef(onLLMComplete);
  useEffect(() => {
    onLLMCompleteRef.current = onLLMComplete;
  }, [onLLMComplete]);

  // Streaming transcription
  const streaming = useStreamingTranscription({
    privacyLevel: 'private',
    aiUsage: 'chat',
    onTranscriptUpdate: (text) => {
      transcriptRef.current = text;
    },
    onComplete: async (data) => {
      setIsStopping(false);
      setPhase('processing');
      const finalTranscript = data.content || transcriptRef.current;
      if (!finalTranscript.trim()) {
        setPhase('ready');
        return;
      }
      try {
        const payload = { content: finalTranscript };
        if (threadParentIdRef.current) {
          payload.parent_id = threadParentIdRef.current;
        }
        if (data.sessionId) {
          payload.session_id = data.sessionId;
        }
        const res = await api.post(apiEndpoint, payload);
        setLlmNodeId(res.data.llm_node_id);
        lastUserNodeIdRef.current = res.data.user_node_id;
      } catch (err) {
        console.error(`${apiEndpoint} API error:`, err);
        setHasError(true);
        setPhase('ready');
      }
    },
  });

  // Poll LLM completion
  const { data: llmData, status: llmStatus } = useAsyncTaskPolling(
    llmNodeId ? `/nodes/${llmNodeId}/llm-status` : null,
    { enabled: !!llmNodeId && phase === 'processing', interval: 1500 }
  );

  // TTS SSE subscription
  const ttsSSE = useTTSStreamSSE(llmNodeId, {
    enabled: ttsGenerating,
    onChunkReady: (data) => {
      if (firstChunkRef.current) {
        firstChunkRef.current = false;
        stopSilentAudio(); // Real audio takes over
        audio.loadAudioQueue(
          [data.audio_url],
          { title: ttsTitle, url: data.audio_url },
          [data.duration]
        );
        audio.setGeneratingTTS(true);
        // Show playback UI as soon as first chunk arrives.
        // Autoplay may or may not work (iOS blocks it); the playback UI
        // has a play button the user can tap if autoplay was blocked.
        setPhase('playback');
      } else {
        audio.appendChunkToQueue(data.audio_url, data.duration);
      }
    },
    onAllComplete: () => {
      setTtsGenerating(false);
      audio.setGeneratingTTS(false);
    },
  });

  // When LLM completes, trigger TTS (and notify the page)
  useEffect(() => {
    if (!llmNodeId) return;
    if (llmStatus === 'completed' && llmData?.content && ttsTriggeredForNodeRef.current !== llmNodeId) {
      ttsTriggeredForNodeRef.current = llmNodeId;
      threadParentIdRef.current = llmNodeId;

      // Let the page handle workflow-specific logic (e.g. parsing, auto-apply)
      if (onLLMCompleteRef.current) {
        onLLMCompleteRef.current(llmNodeId, llmData.content);
      }

      setTtsGenerating(true);
      firstChunkRef.current = true;
      api.post(`/nodes/${llmNodeId}/tts`).catch((err) => {
        console.error('TTS trigger error:', err);
        setHasError(true);
        setTtsGenerating(false);
        setPhase('ready');
      });
    } else if (llmStatus === 'failed') {
      setHasError(true);
      setPhase('ready');
    }
  }, [llmStatus, llmData, llmNodeId]);

  // Safety net: if stuck on "processing" for 15s (e.g. SSE never delivers
  // chunks), transition to playback anyway. The normal path transitions via
  // onChunkReady above; this only fires if something goes wrong.
  useEffect(() => {
    if (phase === 'processing') {
      const timer = setTimeout(() => setPhase('playback'), 15000);
      return () => clearTimeout(timer);
    }
  }, [phase]);

  // Clear error indicator after a few seconds
  useEffect(() => {
    if (hasError) {
      const timer = setTimeout(() => setHasError(false), 3000);
      return () => clearTimeout(timer);
    }
  }, [hasError]);

  const handleStart = useCallback(() => {
    setPhase('recording');
    setHasError(false);
    startSilentAudio(); // User gesture context — activates iOS lock screen controls
    streaming.startStreaming();
  }, [streaming, startSilentAudio]);

  const handleStop = useCallback(() => {
    setIsStopping(true);
    stopSilentAudio();
    // Unlock audio on desktop Safari/Chrome during user gesture.
    // Skip on iOS — autoplay is blocked there regardless, and the silent audio
    // playback conflicts with active Bluetooth mic streams (crashes headphones).
    if (!isIOS) audio.warmup();
    streaming.stopStreaming();
  }, [streaming, audio, stopSilentAudio]);

  const handleContinue = useCallback((extraReset) => {
    audio.stop();
    ttsSSE.disconnect();
    ttsSSE.reset();
    setLlmNodeId(null);
    // Keep threadParentIdRef — continues the conversation thread

    setTtsGenerating(false);
    firstChunkRef.current = true;
    transcriptRef.current = '';
    setHasError(false);
    if (extraReset) extraReset();
    streaming.cancelStreaming();
    // Go straight to recording — skip the ready phase
    setPhase('recording');
    startSilentAudio(); // User gesture context
    streaming.startStreaming();
  }, [audio, ttsSSE, streaming, startSilentAudio]);

  const handleCancelProcessing = useCallback((extraReset) => {
    // Parent next recording to the user node (not the LLM node).
    // The cancelled LLM response completes async as a dead-end sibling.
    if (lastUserNodeIdRef.current) {
      threadParentIdRef.current = lastUserNodeIdRef.current;
    }
    stopSilentAudio();
    audio.stop();
    ttsSSE.disconnect();
    ttsSSE.reset();
    setPhase('ready');
    setLlmNodeId(null);
    setTtsGenerating(false);
    firstChunkRef.current = true;
    transcriptRef.current = '';
    if (extraReset) extraReset();
    streaming.cancelStreaming();
  }, [audio, ttsSSE, streaming, stopSilentAudio]);

  const handlePauseRecording = useCallback(() => {
    streaming.pauseRecording();
  }, [streaming]);

  const handleResumeRecording = useCallback(() => {
    streaming.resumeRecording();
  }, [streaming]);

  // iOS lock screen controls
  useMediaSession({
    phase,
    isPaused: streaming.isPaused,
    handlePauseRecording,
    handleResumeRecording,
    handleStop,
    handleCancelProcessing,
    ttsTitle,
  });

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopSilentAudio();
      audio.stop();
      ttsSSE.disconnect();
      streaming.cancelStreaming();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return {
    phase,
    isStopping,
    isPaused: streaming.isPaused,
    hasError,
    llmData,
    streaming,
    audio,
    ttsGenerating,
    handleStart,
    handleStop,
    handlePauseRecording,
    handleResumeRecording,
    handleContinue,
    handleCancelProcessing,
  };
}
