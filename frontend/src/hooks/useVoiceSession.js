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
export function useVoiceSession({ apiEndpoint, ttsTitle = 'Audio', onLLMComplete, initialLlmNodeId = null, initialParentId = null, model = null }) {
  const audio = useAudio();
  const [phase, setPhase] = useState(initialLlmNodeId ? 'processing' : 'ready');
  const [llmNodeId, setLlmNodeId] = useState(initialLlmNodeId);
  const [isStopping, setIsStopping] = useState(false);
  const [hasError, setHasError] = useState(false);
  const transcriptRef = useRef('');
  const threadParentIdRef = useRef(initialParentId);
  const lastUserNodeIdRef = useRef(null);
  const initialResumeRef = useRef(initialLlmNodeId != null);

  // Keep URL params in sync so a page refresh resumes correctly
  useEffect(() => {
    const url = new URL(window.location);
    if (llmNodeId) {
      url.searchParams.set('resume', String(llmNodeId));
      // Use the future threadParentId value: for initial resume it stays as
      // initialParentId; for subsequent turns it will become llmNodeId.
      const parentForUrl = initialResumeRef.current
        ? threadParentIdRef.current
        : llmNodeId;
      if (parentForUrl != null) {
        url.searchParams.set('parent', String(parentForUrl));
      }
    } else {
      url.searchParams.delete('resume');
      if (threadParentIdRef.current != null) {
        url.searchParams.set('parent', String(threadParentIdRef.current));
      } else {
        url.searchParams.delete('parent');
      }
    }
    window.history.replaceState({}, '', url);
  }, [llmNodeId]);

  // Warn before leaving the page while recording is active.
  // beforeunload handles browser refresh/close; useBlocker handles SPA navigation.
  useEffect(() => {
    if (phase !== 'recording') return;
    const handleBeforeUnload = (e) => {
      e.preventDefault();
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [phase]);

  // NOTE: SPA navigation during recording is not blocked. useBlocker/usePrompt
  // require createBrowserRouter (data router) but the app uses <BrowserRouter>.
  // beforeunload above covers browser refresh/close/external navigation.

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
  // Derive label from apiEndpoint: '/reflect' → 'Reflect', '/orient' → 'Orient'
  const workflowLabel = apiEndpoint ? apiEndpoint.replace('/', '').charAt(0).toUpperCase() + apiEndpoint.replace('/', '').slice(1) : null;
  const streaming = useStreamingTranscription({
    privacyLevel: 'private',
    aiUsage: 'chat',
    label: workflowLabel,
    onTranscriptUpdate: (text) => {
      transcriptRef.current = text;
    },
    onComplete: async (data) => {
      setIsStopping(false);
      setPhase('processing');
      // Clear any stale audio from previous pages (e.g. SpeakerIcon in Log)
      // so it can't play if new TTS chunks fail to arrive.
      audio.stop();
      const finalTranscript = data.content || transcriptRef.current;
      if (!finalTranscript.trim()) {
        setPhase('ready');
        return;
      }

      // Server-side LLM chain: if the finalize task already created the LLM
      // node, skip the frontend POST and use the server-provided node ID.
      if (data.llmNodeId) {
        console.log('[VoiceSession] Server-side LLM chain: llmNodeId=', data.llmNodeId);
        setLlmNodeId(data.llmNodeId);
        return;
      }

      // Fallback: trigger LLM via frontend POST (non-streaming or legacy path)
      try {
        const payload = { content: finalTranscript };
        if (model) {
          payload.model = model;
        }
        if (threadParentIdRef.current) {
          payload.parent_id = threadParentIdRef.current;
        }
        if (data.sessionId) {
          payload.session_id = data.sessionId;
        }
        const res = await api.post(apiEndpoint, payload);
        console.log('[VoiceSession] API response:', { llm_node_id: res.data.llm_node_id, user_node_id: res.data.user_node_id, parent_id: payload.parent_id });
        setLlmNodeId(res.data.llm_node_id);
        lastUserNodeIdRef.current = res.data.user_node_id;
      } catch (err) {
        console.error(`${apiEndpoint} API error:`, err);
        setHasError(true);
        setPhase('ready');
      }
    },
  });

  // Poll LLM completion (don't gate on phase — the 15s safety-net timeout
  // changes phase to 'playback' which would kill polling before slow LLM
  // responses arrive; polling self-stops on completed/failed status, and
  // is disabled when llmNodeId is cleared on cancel/continue)
  const { data: llmData, status: llmStatus } = useAsyncTaskPolling(
    llmNodeId ? `/nodes/${llmNodeId}/llm-status` : null,
    { enabled: !!llmNodeId, interval: 1500 }
  );

  // TTS SSE subscription
  const ttsSSE = useTTSStreamSSE(llmNodeId, {
    enabled: ttsGenerating,
    onChunkReady: async (data) => {
      console.log('[VoiceSession] TTS chunk ready:', { audio_url: data.audio_url, chunk_index: data.chunk_index, firstChunk: firstChunkRef.current });
      if (firstChunkRef.current) {
        firstChunkRef.current = false;
        stopSilentAudio(); // Real audio takes over
        // Await so duration state is set before we show the playback UI.
        // loadAudioQueue also starts preloading the audio for instant play.
        await audio.loadAudioQueue(
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
    if (llmStatus === 'completed' && llmData?.content && ttsTriggeredForNodeRef.current !== llmNodeId && llmData.node_id === llmNodeId) {
      console.log('[VoiceSession] TTS trigger:', { llmNodeId, pollNodeId: llmData.node_id, contentPreview: llmData.content?.substring(0, 50) });
      ttsTriggeredForNodeRef.current = llmNodeId;
      const wasInitialResume = initialResumeRef.current;
      if (initialResumeRef.current) {
        initialResumeRef.current = false;
        // Keep threadParentIdRef from initialParentId for the resumed playback
      } else {
        threadParentIdRef.current = llmNodeId;
      }

      // Sync URL parent after threadParentIdRef update
      const url = new URL(window.location);
      if (threadParentIdRef.current != null) {
        url.searchParams.set('parent', String(threadParentIdRef.current));
      }
      window.history.replaceState({}, '', url);

      // Let the page handle workflow-specific logic (e.g. parsing, auto-apply)
      if (onLLMCompleteRef.current) {
        onLLMCompleteRef.current(llmNodeId, llmData.content, wasInitialResume);
      }

      // If LLM returned no text (tool-only response), skip TTS entirely
      if (!llmData.content || !llmData.content.trim()) {
        stopSilentAudio();
        setPhase('playback');
        return;
      }

      firstChunkRef.current = true;
      // Await the TTS POST before enabling SSE to avoid the race where
      // the EventSource connects before tts_task_status is set to 'pending'.
      api.post(`/nodes/${llmNodeId}/tts`).then((res) => {
        if (res.status === 200 && res.data.tts_url) {
          // TTS was already generated — load directly, skip SSE
          const ttsUrl = res.data.tts_url.startsWith('http')
            ? res.data.tts_url
            : `${process.env.REACT_APP_BACKEND_URL || ''}${res.data.tts_url}`;
          stopSilentAudio();
          audio.loadAudio({ url: ttsUrl, title: ttsTitle });
          setPhase('playback');
        } else {
          // TTS generation started (202) — enable SSE now that backend is ready
          setTtsGenerating(true);
        }
      }).catch((err) => {
        console.error('TTS trigger error:', err);
        setHasError(true);
        setTtsGenerating(false);
        setPhase('ready');
      });
    } else if (llmStatus === 'failed') {
      setHasError(true);
      setPhase('ready');
    }
  }, [llmStatus, llmData, llmNodeId, audio, ttsTitle, stopSilentAudio]);

  // Safety net: if stuck on "processing" for 60s (e.g. SSE never delivers
  // chunks), transition to playback anyway. The normal path transitions via
  // onChunkReady above; this only fires if something goes wrong.
  // Increased from 15s to 60s to accommodate tool-use responses.
  useEffect(() => {
    if (phase === 'processing') {
      const timer = setTimeout(() => setPhase('playback'), 60000);
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
    streaming.startStreaming(threadParentIdRef.current);
  }, [streaming, startSilentAudio]);

  const handleStop = useCallback(() => {
    setIsStopping(true);
    // Unlock audio on desktop Safari/Chrome during user gesture.
    // Skip on iOS — autoplay is blocked there regardless, and the silent audio
    // playback conflicts with active Bluetooth mic streams (crashes headphones).
    if (!isIOS) audio.warmup();
    // Pass workflow params so the server can kick off LLM + TTS without
    // waiting for the frontend to foreground.
    const extraParams = {};
    if (threadParentIdRef.current) extraParams.parent_id = threadParentIdRef.current;
    if (model) extraParams.model = model;
    // Keep silent audio playing until stopStreaming completes — on iOS it's the
    // only thing preventing the OS from suspending JS while the final chunk
    // upload and finalize request are in flight.
    streaming.stopStreaming(extraParams).finally(() => {
      stopSilentAudio();
    });
  }, [streaming, stopSilentAudio, audio, model]);

  const handleContinue = useCallback((extraReset) => {
    audio.stop();
    ttsSSE.disconnect();
    ttsSSE.reset();
    setLlmNodeId(null);
    ttsTriggeredForNodeRef.current = null;
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
    streaming.startStreaming(threadParentIdRef.current);
  }, [audio, ttsSSE, streaming, startSilentAudio]);

  const setThreadParentId = useCallback((id) => {
    threadParentIdRef.current = id;
    const url = new URL(window.location);
    url.searchParams.set('parent', String(id));
    window.history.replaceState({}, '', url);
  }, []);

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

  // Resume an interrupted session (continue recording from where it left off)
  const handleResumeSession = useCallback(({ sessionId, draftId, chunkCount, parentId: draftParentId }) => {
    // Restore thread context so finalization uses the correct parent
    if (draftParentId != null) {
      threadParentIdRef.current = draftParentId;
      // Update URL to reflect the thread context
      const url = new URL(window.location);
      url.searchParams.set('parent', String(draftParentId));
      url.searchParams.delete('resume');
      window.history.replaceState({}, '', url);
    }
    setPhase('recording');
    setHasError(false);
    startSilentAudio();
    streaming.resumeStreaming(sessionId, draftId, chunkCount);
  }, [streaming, startSilentAudio]);

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
    duration: streaming.duration,
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
    handleResumeSession,
    handleCancelProcessing,
    setThreadParentId,
  };
}
