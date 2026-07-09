import { useState, useCallback, useRef, useEffect } from 'react';
import { useStreamingTranscription } from './useStreamingTranscription';
import { useAsyncTaskPolling } from './useAsyncTaskPolling';
import { useLlmTaskWarnings } from './useLlmTaskWarnings';
import { useTTSStreamSSE } from './useSSE';
import { useAudio } from '../contexts/AudioContext';
import { useMediaSession } from './useMediaSession';
import { useOnlineStatus } from './useOnlineStatus';
import { useToast } from '../contexts/ToastContext';
import api from '../api';

// iOS devices can't autoplay audio regardless of warmup, and playing silent audio
// while the mic stream is active crashes Bluetooth headphones on multi-device setups.
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

/**
 * Hook for voice conversation workflow.
 *
 * Manages the full cycle: recording → transcription → LLM → TTS playback.
 * VoicePage provides UI and workflow-specific logic via callbacks.
 *
 * @param {Object} options
 * @param {string} options.apiEndpoint - API path to post transcripts to ('/voice')
 * @param {string} options.ttsTitle - Label for the audio player
 * @param {Function} options.onLLMComplete - Called with (nodeId, content) when LLM response is ready
 * @param {number|null} options.initialLlmNodeId - Resume in processing phase, polling this LLM node
 * @param {number|null} options.initialParentId - Resume in ready phase with thread parent pre-set
 */
export function useVoiceSession({ apiEndpoint, ttsTitle = 'Audio', onLLMComplete, initialLlmNodeId = null, initialParentId = null, model = null, aiUsage = 'none' }) {
  const audio = useAudio();
  const isOnline = useOnlineStatus();
  const [phase, setPhase] = useState(initialLlmNodeId ? 'processing' : 'ready');
  const [llmNodeId, setLlmNodeId] = useState(initialLlmNodeId);
  const [isStopping, setIsStopping] = useState(false);
  const [hasError, setHasError] = useState(false);
  const transcriptRef = useRef('');
  const threadParentIdRef = useRef(initialParentId);
  const lastUserNodeIdRef = useRef(null);
  const initialResumeRef = useRef(initialLlmNodeId != null);
  const { addToast } = useToast();

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
  // Within-turn tool chain (#158 Slice 4, voice). When the backend runs
  // the within-turn tool loop for voice, a turn produces an interim node
  // (e.g. "on it…") linked to a continuation node that holds the answer.
  // We play the whole chain as ONE continuous audio queue: each node's TTS
  // chunks are appended in order and `audio.generatingTTS` stays true
  // across the boundary so the queue waits (not stops) if it drains before
  // the next node's chunks arrive. The backend dispatches each node's TTS
  // at that node's own finalization, so the interim is playable while the
  // continuation call is still generating — if the interim finishes
  // playing before the continuation's audio arrives, the UI drops back to
  // "Thinking..." (see the drain effect below) and returns to playback
  // when the first continuation chunk lands. All refs stay inert for
  // single-node turns (continuation_node_id is null), keeping that path
  // byte-identical.
  const pendingContinuationRef = useRef(null);
  const continuingChainRef = useRef(false);
  // True from advancing to the continuation node until its FIRST audio
  // chunk arrives — the window where a playback drain means "the rest of
  // the answer isn't ready yet" rather than "buffering mid-node".
  const awaitingNextNodeRef = useRef(false);
  // Latest advanceChain (defined after ttsSSE, which its body needs);
  // ref-called from the SSE callbacks above its definition.
  const advanceChainRef = useRef(null);

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
  // Derive label from apiEndpoint: '/voice' → 'Voice'
  const workflowLabel = apiEndpoint ? apiEndpoint.replace('/', '').charAt(0).toUpperCase() + apiEndpoint.replace('/', '').slice(1) : null;
  const streaming = useStreamingTranscription({
    privacyLevel: 'private',
    aiUsage,
    label: workflowLabel,
    onTranscriptUpdate: (text) => {
      transcriptRef.current = text;
    },
    onError: (err) => {
      // Surface (1) startup failures (getUserMedia/MediaRecorder ctor) and
      // (2) fatal upload failures (server rejected chunk 0 with
      // init_parse_failed — recorder was reset, session is dead).
      // Non-fatal upload failures (transient network) preserve prior silent
      // retry-queue behavior so they don't interrupt an in-progress recording.
      if (!err?.startup && !err?.fatal) return;

      const name = err?.name || err?.error?.name;
      let message;
      if (err?.fatal) {
        // Use the fatal-error message verbatim — it's already user-facing.
        message = err.message;
      } else if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
        message = "Microphone access is blocked. On Android, update this browser app's microphone permission in your phone's Settings. On iPhone or desktop, re-grant microphone access for this site via the browser's site-permissions menu (tap the address-bar icon). Then reload and try again.";
      } else if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
        message = "No microphone was found on this device.";
      } else if (name === 'NotReadableError' || name === 'TrackStartError') {
        message = "The microphone is in use by another app. Close other apps using it (calls, voice memos, video meetings) and try again.";
      } else if (name === 'SecurityError') {
        message = "Microphone access was blocked by browser security settings.";
      } else if (name === 'NotSupportedError') {
        message = err?.message || "Your browser does not support the required audio format. Please try Chrome or update your operating system.";
      } else if (err?.message) {
        message = `Could not start recording: ${err.message}`;
      } else {
        message = "Could not start recording. Please try again.";
      }
      addToast(message, 8000);
      stopSilentAudio();
      setIsStopping(false);
      setHasError(true);
      setPhase('ready');
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

      // Server-side validation rejected the request (e.g. misconfigured
      // {user_export}). No LLM node was created — surface the toast and
      // return to ready so the user can re-record without seeing a stub
      // failed response.
      if (data.warning) {
        addToast(data.warning, 8000);
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
        if (aiUsage) {
          payload.ai_usage = aiUsage;
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
        // Server returned a structured validation error (400) — surface
        // the message as a toast so the user knows what to fix.
        const apiErr = err?.response?.data?.error;
        if (apiErr) {
          addToast(apiErr, 8000);
          setPhase('ready');
          return;
        }
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

  // Surface server-side warnings (e.g. typoed {user_export} keys) as toasts
  useLlmTaskWarnings(llmData, llmStatus);

  // TTS SSE subscription
  const ttsSSE = useTTSStreamSSE(llmNodeId, {
    enabled: ttsGenerating,
    onChunkReady: async (data) => {
      console.log('[VoiceSession] TTS chunk ready:', { audio_url: data.audio_url, chunk_index: data.chunk_index, firstChunk: firstChunkRef.current });
      awaitingNextNodeRef.current = false;
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
        // A continuation's first chunk can arrive while the UI dropped
        // back to "Thinking..." (interim playback drained before the
        // answer's audio was ready) — return to the playback UI.
        // Autoplay of the appended chunk is handled by the audio queue
        // (drain-waiting resume); on iOS the play button is there.
        setPhase('playback');
      }
    },
    onAllComplete: () => {
      const nextId = pendingContinuationRef.current;
      if (nextId != null) {
        // Within-turn chain (Slice 4): the current node's TTS is fully
        // generated; advance to the continuation and append its TTS to the
        // SAME audio queue so playback flows seamlessly.
        advanceChainRef.current(nextId);
        return;
      }
      continuingChainRef.current = false;
      setTtsGenerating(false);
      audio.setGeneratingTTS(false);
    },
  });

  // Advance the within-turn chain to the continuation node: poll its
  // llm-status, and when completed its TTS-trigger appends to the SAME
  // audio queue. Keep audio.generatingTTS TRUE so the queue waits — not
  // stops — if it drains before the continuation's chunks arrive. Disable
  // the local ttsGenerating (SSE) momentarily; the continuation's
  // TTS-trigger re-enables it AFTER its /tts POST, avoiding the
  // connect-before-pending race.
  const advanceChain = useCallback((nextId) => {
    pendingContinuationRef.current = null;
    continuingChainRef.current = true;
    awaitingNextNodeRef.current = true;
    ttsTriggeredForNodeRef.current = null;
    ttsSSE.reset();
    setTtsGenerating(false);
    audio.setGeneratingTTS(true);
    setLlmNodeId(nextId);
  }, [ttsSSE, audio]);
  advanceChainRef.current = advanceChain;

  // Interim playback finished but the continuation's audio isn't ready yet
  // (the answer is still generating) — drop back to "Thinking..." until
  // its first chunk arrives (onChunkReady flips back to playback).
  // llmNodeId is a dep because the drain can precede the chain advance
  // (last chunk finishes playing before all_complete arrives): the advance
  // changes llmNodeId, re-running this check with awaitingNextNode now set.
  useEffect(() => {
    if (audio.waitingForChunks && awaitingNextNodeRef.current) {
      setPhase('processing');
    }
  }, [audio.waitingForChunks, llmNodeId]);

  // When an LLM node completes, trigger TTS (and, for the FINAL node of the
  // turn, notify the page). With the voice retrieval loop on (Slice 4) a turn
  // can be a chain (interim "looking that up" node → continuation answer);
  // each node's TTS is triggered in turn and onAllComplete advances the chain.
  useEffect(() => {
    if (!llmNodeId) return;
    if (llmStatus === 'completed' && llmData?.content && ttsTriggeredForNodeRef.current !== llmNodeId && llmData.node_id === llmNodeId) {
      const continuationId = llmData.continuation_node_id ?? null;
      console.log('[VoiceSession] TTS trigger:', { llmNodeId, pollNodeId: llmData.node_id, continuationId, contentPreview: llmData.content?.substring(0, 50) });
      ttsTriggeredForNodeRef.current = llmNodeId;
      // Remember the continuation so onAllComplete advances to it once this
      // node's TTS finishes generating (null for the final node / flag off).
      pendingContinuationRef.current = continuationId;

      // Thread bookkeeping + page callback belong to the FINAL node only: the
      // next turn parents off the answer (not an interim retrieval step), and
      // interim text carries no proposals to parse.
      if (continuationId == null) {
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
      }

      // If LLM returned no text (tool-only response), skip TTS entirely.
      // Interim nodes always carry fallback text, so an empty node is a
      // final answer — end any chain so the audio queue can finish.
      if (!llmData.content || !llmData.content.trim()) {
        stopSilentAudio();
        continuingChainRef.current = false;
        audio.setGeneratingTTS(false);
        setPhase('playback');
        return;
      }

      // Continuing a chain appends to the existing queue; a fresh turn starts
      // a new one. Single-node turns are byte-identical (continuingChain is
      // false → firstChunk true, exactly as before).
      firstChunkRef.current = !continuingChainRef.current;
      // Await the TTS POST before enabling SSE to avoid the race where
      // the EventSource connects before tts_task_status is set to 'pending'.
      api.post(`/nodes/${llmNodeId}/tts`).then((res) => {
        if (res.status === 200 && res.data.tts_url) {
          // TTS was already fully generated — no SSE. Load queue-style so
          // a chain can still append/advance (loadAudio broke the chain
          // here: the continuation was never picked up).
          const ttsUrl = res.data.tts_url.startsWith('http')
            ? res.data.tts_url
            : `${process.env.REACT_APP_BACKEND_URL || ''}${res.data.tts_url}`;
          stopSilentAudio();
          if (continuingChainRef.current) {
            // Mid-chain: append this node's full audio to the live queue.
            // Flip to playback only once the append resolved (it preloads
            // the duration first) — a simultaneous chain-advance below
            // could otherwise flip to "Thinking..." after us and strand
            // the UI there while the appended audio plays.
            awaitingNextNodeRef.current = false;
            audio.appendChunkToQueue(ttsUrl).then(() => setPhase('playback'));
          } else {
            firstChunkRef.current = false;
            audio.loadAudioQueue([ttsUrl], { title: ttsTitle, url: ttsUrl });
            setPhase('playback');
          }
          const nextId = pendingContinuationRef.current;
          if (nextId != null) {
            advanceChain(nextId);
          } else {
            continuingChainRef.current = false;
            audio.setGeneratingTTS(false);
          }
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
  }, [llmStatus, llmData, llmNodeId, audio, ttsTitle, stopSilentAudio, advanceChain]);

  // Safety net: TTS is being generated (POST /tts returned 202) but no
  // chunk arrived within 60s — SSE is probably dead; transition to
  // playback anyway. The normal path transitions via onChunkReady above.
  // Deliberately NOT armed while the LLM itself is still generating
  // (ttsGenerating false): long turns legitimately think for minutes and
  // the llm-status polling handles failures — flipping to a dead player
  // mid-generation was worse than honest "Thinking...".
  useEffect(() => {
    if (phase === 'processing' && ttsGenerating) {
      const timer = setTimeout(() => setPhase('playback'), 60000);
      return () => clearTimeout(timer);
    }
  }, [phase, ttsGenerating]);

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
    // New turn → fresh audio queue; drop any in-flight retrieval chain.
    pendingContinuationRef.current = null;
    continuingChainRef.current = false;
    awaitingNextNodeRef.current = false;
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
    // New turn → fresh audio queue; drop any in-flight retrieval chain.
    pendingContinuationRef.current = null;
    continuingChainRef.current = false;
    awaitingNextNodeRef.current = false;
    transcriptRef.current = '';
    if (extraReset) extraReset();
    streaming.cancelStreaming();
  }, [audio, ttsSSE, streaming, stopSilentAudio]);

  // Resume an interrupted session (continue recording from where it left off).
  // mimeType is the family-only mime ('audio/webm' or 'audio/mp4') from the
  // /drafts/interrupted payload — required so the recorder records in the
  // same family chunk 0 was uploaded with (otherwise the server rejects with
  // mime_mismatch).
  const handleResumeSession = useCallback(({ sessionId, draftId, chunkCount, parentId: draftParentId, mimeType }) => {
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
    streaming.resumeStreaming(sessionId, draftId, chunkCount, mimeType);
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
    isOnline,
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
