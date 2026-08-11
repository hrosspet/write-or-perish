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

// #242 REST recovery pacing. iOS Safari suspends/kills in-flight XHRs and
// EventSources when the app backgrounds or the screen locks — often without
// firing any error event — so every step of the post-LLM delivery layer
// (the /tts POST, the SSE chunk stream) can be silently lost. The reconcile
// loop below checks REST truth at this cadence while a turn is undelivered.
const TTS_RECOVERY_POLL_MS = 7000;
// How long an armed /tts POST may stay silent (no 200/202 outcome, no SSE
// activity) before we assume the request was lost and re-fire it.
const TTS_TRIGGER_WATCHDOG_MS = 20000;

// Chapter label for a chain node: the node text's first words, cleaned of
// markdown furniture, cut at a word boundary.
const CHAPTER_TITLE_MAX = 44;
function chapterTitleFromContent(content) {
  const clean = (content || '')
    .replace(/[#*_`>[\]]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!clean) return null;
  if (clean.length <= CHAPTER_TITLE_MAX) return clean;
  const cut = clean.slice(0, CHAPTER_TITLE_MAX);
  return `${cut.slice(0, cut.lastIndexOf(' ') > 20 ? cut.lastIndexOf(' ') : CHAPTER_TITLE_MAX)}…`;
}

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
  // #242 recovery bookkeeping. When the /tts POST fired for the current
  // node (watchdog reference point); which node got audio via SSE chunks
  // (a REST full-file delivery would duplicate it); which node got audio
  // via the full-file path (late SSE chunks for it must be dropped).
  const ttsAttemptAtRef = useRef(null);
  const sseDeliveredForNodeRef = useRef(null);
  const restDeliveredForNodeRef = useRef(null);
  // Bumped by the recovery watchdog to re-run the TTS-trigger effect after
  // it re-arms ttsTriggeredForNodeRef (llm-status polling has already
  // stopped on 'completed', so no other dep will change).
  const [ttsRetriggerNonce, setTtsRetriggerNonce] = useState(0);
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
  // Chapter title for the NEXT node whose audio lands in the queue —
  // set at TTS-trigger time from the node's text, consumed once by
  // whichever path delivers that node's first audio. Chain playback
  // shows one chapter per node (like .md section chapters).
  const nextChapterTitleRef = useRef(null);
  const takeChapterTitle = () => {
    const t = nextChapterTitleRef.current;
    nextChapterTitleRef.current = null;
    return t;
  };

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
      // #242: this node's audio already landed whole via REST recovery —
      // a late-waking SSE stream must not append it again. (Same-URL
      // appends are also caught by the queue invariant, but multi-chunk
      // nodes have chunk URLs that differ from the full-file URL.)
      if (restDeliveredForNodeRef.current === llmNodeId) return;
      sseDeliveredForNodeRef.current = llmNodeId;
      awaitingNextNodeRef.current = false;
      if (firstChunkRef.current) {
        firstChunkRef.current = false;
        stopSilentAudio(); // Real audio takes over
        const chapterTitle = takeChapterTitle();
        // Await so duration state is set before we show the playback UI.
        // loadAudioQueue also starts preloading the audio for instant play.
        await audio.loadAudioQueue(
          [data.audio_url],
          {
            title: ttsTitle,
            url: data.audio_url,
            chapters: chapterTitle
              ? [{ title: chapterTitle, start_time: 0, chunk_index: 0 }] : [],
          },
          [data.duration]
        );
        audio.setGeneratingTTS(true);
        // Show playback UI as soon as first chunk arrives.
        // Autoplay may or may not work (iOS blocks it); the playback UI
        // has a play button the user can tap if autoplay was blocked.
        setPhase('playback');
      } else {
        audio.appendChunkToQueue(data.audio_url, data.duration, takeChapterTitle());
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
    // Deliberately NOT clearing ttsTriggeredForNodeRef here: it still
    // holds the JUST-TTS'd node's id, which keeps the trigger effect from
    // re-firing for that node in any render between now and the llmNodeId
    // update landing (re-firing hit the already-generated 200 path and
    // appended the same audio to the queue a second time). The
    // continuation triggers fine — its id differs from the stored one.
    ttsSSE.reset();
    setTtsGenerating(false);
    audio.setGeneratingTTS(true);
    setLlmNodeId(nextId);
  }, [ttsSSE, audio]);
  advanceChainRef.current = advanceChain;

  // Deliver a node's fully-generated TTS from its final URL (no SSE).
  // Shared by the POST /tts 200 path and the #242 REST recovery: loads or
  // appends queue-style and advances the chain exactly like the chunked
  // path, so a rescue is indistinguishable from a normal delivery.
  const deliverFullTts = useCallback((rawUrl) => {
    const ttsUrl = rawUrl.startsWith('http')
      ? rawUrl
      : `${process.env.REACT_APP_BACKEND_URL || ''}${rawUrl}`;
    restDeliveredForNodeRef.current = llmNodeId;
    stopSilentAudio();
    if (continuingChainRef.current) {
      // Mid-chain: append this node's full audio to the live queue.
      // Flip to playback only once the append resolved (it preloads
      // the duration first) — a simultaneous chain-advance below
      // could otherwise flip to "Thinking..." after us and strand
      // the UI there while the appended audio plays.
      awaitingNextNodeRef.current = false;
      audio.appendChunkToQueue(ttsUrl, null, takeChapterTitle())
        .then(() => setPhase('playback'));
    } else {
      firstChunkRef.current = false;
      const chapterTitle = takeChapterTitle();
      audio.loadAudioQueue([ttsUrl], {
        title: ttsTitle,
        url: ttsUrl,
        chapters: chapterTitle
          ? [{ title: chapterTitle, start_time: 0, chunk_index: 0 }] : [],
      });
      setPhase('playback');
    }
    const nextId = pendingContinuationRef.current;
    if (nextId != null) {
      advanceChain(nextId);
    } else {
      continuingChainRef.current = false;
      audio.setGeneratingTTS(false);
    }
  }, [llmNodeId, audio, ttsTitle, stopSilentAudio, advanceChain]);

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
    // NOTE: deliberately not gated on llmData.content being non-empty — a
    // completed node with EMPTY content must still resolve the phase (the
    // skip-TTS branch below), otherwise the turn hangs on "Thinking...".
    if (llmStatus === 'completed' && llmData && ttsTriggeredForNodeRef.current !== llmNodeId && llmData.node_id === llmNodeId) {
      const continuationId = llmData.continuation_node_id ?? null;
      console.log('[VoiceSession] TTS trigger:', { llmNodeId, pollNodeId: llmData.node_id, continuationId, contentPreview: llmData.content?.substring(0, 50) });
      ttsTriggeredForNodeRef.current = llmNodeId;
      // Remember the continuation so onAllComplete advances to it once this
      // node's TTS finishes generating (null for the final node / flag off).
      pendingContinuationRef.current = continuationId;
      // Chapter label for this node, consumed when its first audio lands.
      nextChapterTitleRef.current = chapterTitleFromContent(llmData.content);

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
          onLLMCompleteRef.current(llmNodeId, llmData.content || '', wasInitialResume);
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
      // Watchdog reference point (#242): if this POST is silently lost to
      // an iOS suspension (no response EVER — not even an error), the
      // recovery loop below re-arms and re-fires after
      // TTS_TRIGGER_WATCHDOG_MS. The endpoint is idempotent (200 if
      // already generated, 202-no-reenqueue if in progress).
      ttsAttemptAtRef.current = Date.now();
      // Await the TTS POST before enabling SSE to avoid the race where
      // the EventSource connects before tts_task_status is set to 'pending'.
      api.post(`/nodes/${llmNodeId}/tts`).then((res) => {
        if (res.status === 200 && res.data.tts_url) {
          // TTS was already fully generated — no SSE. Deliver queue-style
          // so a chain can still append/advance (loadAudio broke the chain
          // here: the continuation was never picked up). Skip if the
          // recovery loop already delivered while this POST was in flight.
          if (restDeliveredForNodeRef.current !== llmNodeId) {
            deliverFullTts(res.data.tts_url);
          }
        } else {
          // TTS generation started (202) — enable SSE now that backend is ready
          setTtsGenerating(true);
        }
      }).catch((err) => {
        console.error('TTS trigger error:', err);
        if (!err.response) {
          // Network-level loss (#242): the request died without an HTTP
          // response — on iOS typically an XHR killed by app suspension.
          // The turn is still deliverable (the server-side POST is
          // idempotent), so stay on "Thinking..." and let the recovery
          // watchdog re-fire instead of kicking the user back to ready.
          ttsTriggeredForNodeRef.current = null;
          return;
        }
        setHasError(true);
        setTtsGenerating(false);
        setPhase('ready');
      });
    } else if (llmStatus === 'failed') {
      setHasError(true);
      setPhase('ready');
    }
    // ttsRetriggerNonce is deliberately an "unused" dep: the recovery
    // watchdog bumps it to re-run this effect after re-arming the trigger
    // (llm-status polling already stopped on 'completed', so no other dep
    // changes).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [llmStatus, llmData, llmNodeId, audio, ttsTitle, stopSilentAudio, advanceChain, deliverFullTts, ttsRetriggerNonce]);

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

  // REST recovery loop (#242). iOS Safari suspends/kills in-flight XHRs
  // and EventSources when the app backgrounds or the screen locks — often
  // WITHOUT firing any error event — so each step of the post-LLM delivery
  // layer can silently vanish: the one-shot /tts POST, and the SSE chunk
  // stream. The transcription layer has had a REST fallback for exactly
  // this since March (see useStreamingTranscription's finalizing poll);
  // this is the equivalent for the TTS side. While the turn is
  // undelivered, reconcile against REST truth every TTS_RECOVERY_POLL_MS,
  // and immediately on foreground:
  //  - trigger watchdog: LLM completed but the /tts POST produced no
  //    outcome (no 200/202, no SSE activity) within
  //    TTS_TRIGGER_WATCHDOG_MS → re-arm and re-run the trigger effect.
  //  - dead-SSE fallback: SSE enabled but the server says TTS already
  //    completed → deliver the full file via REST; if SSE chunks for this
  //    node were already queued, force an SSE reconnect instead (the
  //    stream replays every chunk; received-index + queue-URL dedup make
  //    the replay safe).
  // Latest values for the reconcile closure. The effect below must depend
  // only on the primitives that define "undelivered turn" (phase,
  // ttsGenerating, llmNodeId) — llm-status polling re-renders every 1.5s
  // and ttsSSE/deliverFullTts get fresh identities each render, which as
  // deps would reset the interval before it ever fired.
  const llmStatusRef = useRef(llmStatus);
  llmStatusRef.current = llmStatus;
  const deliverFullTtsRef = useRef(deliverFullTts);
  deliverFullTtsRef.current = deliverFullTts;
  const ttsSSERef = useRef(ttsSSE);
  ttsSSERef.current = ttsSSE;

  useEffect(() => {
    if (!llmNodeId) return;
    if (phase !== 'processing' && !ttsGenerating) return;

    let cancelled = false;

    const reconcile = async () => {
      if (cancelled) return;
      if (
        phase === 'processing' &&
        !ttsGenerating &&
        llmStatusRef.current === 'completed' &&
        restDeliveredForNodeRef.current !== llmNodeId &&
        ttsAttemptAtRef.current != null &&
        Date.now() - ttsAttemptAtRef.current > TTS_TRIGGER_WATCHDOG_MS
      ) {
        console.warn('[VoiceSession] TTS watchdog: /tts trigger lost, re-firing for node', llmNodeId);
        ttsTriggeredForNodeRef.current = null;
        ttsAttemptAtRef.current = Date.now();
        setTtsRetriggerNonce((n) => n + 1);
        return;
      }
      if (ttsGenerating) {
        try {
          const res = await api.get(`/nodes/${llmNodeId}/tts-status`, { timeout: 10000 });
          if (cancelled) return;
          if (res.data.status === 'completed' && res.data.node?.audio_tts_url) {
            if (sseDeliveredForNodeRef.current === llmNodeId) {
              // Part of this node's audio already arrived via SSE — a
              // full-file load would duplicate it. Reconnect and let the
              // replayed stream fill the gap (and fire all_complete,
              // which advances the chain).
              console.warn('[VoiceSession] TTS SSE stalled mid-stream — reconnecting for node', llmNodeId);
              ttsSSERef.current.reconnect();
            } else if (restDeliveredForNodeRef.current !== llmNodeId) {
              console.warn('[VoiceSession] TTS SSE silent but generation complete — REST delivery for node', llmNodeId);
              setTtsGenerating(false);
              deliverFullTtsRef.current(res.data.node.audio_tts_url);
            }
          } else if (res.data.status === 'failed') {
            setHasError(true);
            setTtsGenerating(false);
            setPhase('ready');
          }
        } catch (_) {
          // Transient — retry on the next tick.
        }
      }
    };

    const intervalId = setInterval(reconcile, TTS_RECOVERY_POLL_MS);
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') reconcile();
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      cancelled = true;
      clearInterval(intervalId);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [phase, ttsGenerating, llmNodeId]);

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
    ttsAttemptAtRef.current = null;
    sseDeliveredForNodeRef.current = null;
    restDeliveredForNodeRef.current = null;
    // Keep threadParentIdRef — continues the conversation thread

    setTtsGenerating(false);
    firstChunkRef.current = true;
    // New turn → fresh audio queue; drop any in-flight retrieval chain.
    pendingContinuationRef.current = null;
    continuingChainRef.current = false;
    awaitingNextNodeRef.current = false;
    nextChapterTitleRef.current = null;
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
    ttsTriggeredForNodeRef.current = null;
    ttsAttemptAtRef.current = null;
    sseDeliveredForNodeRef.current = null;
    restDeliveredForNodeRef.current = null;
    setTtsGenerating(false);
    firstChunkRef.current = true;
    // New turn → fresh audio queue; drop any in-flight retrieval chain.
    pendingContinuationRef.current = null;
    continuingChainRef.current = false;
    awaitingNextNodeRef.current = false;
    nextChapterTitleRef.current = null;
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
    // Restart the silent keepalive inside the user-gesture context (lock
    // screen play press) so the lock screen flips back to "playing".
    const el = silentAudioRef.current?.el;
    if (el) el.play().catch(() => {});
    streaming.resumeRecording();
  }, [streaming]);

  // Pause the silent keepalive ONLY during a mic interruption. iOS
  // lock-screen controls follow the audio ELEMENT's actual state, not the
  // declared MediaSession playbackState — a still-playing keepalive made
  // the lock screen show a Pause button during an interruption, hiding the
  // play/resume affordance (#245 field test). During an interruption the
  // OS holds the audio session anyway, so there is nothing to keep alive.
  //
  // A plain user pause must NOT pause the keepalive: it is what preserves
  // the page's audio session on iOS, and tearing it down killed the
  // capture side — resume() then "recorded" silence (duration ticking, no
  // words; #245 field test round 4, regression from round 2).
  useEffect(() => {
    const el = silentAudioRef.current?.el;
    if (!el || phase !== 'recording') return;
    if (streaming.isInterrupted) {
      el.pause();
    } else if (!streaming.isPaused) {
      el.play().catch(() => {});
    }
  }, [streaming.isInterrupted, streaming.isPaused, phase]);

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
