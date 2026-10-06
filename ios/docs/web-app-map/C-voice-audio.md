# C. Voice and audio pipeline: map for the native iOS port

Source: `.claude/worktrees/ios-app` at origin/main `2774ab2` (2026-09-30, includes #370, #372, #376, #383).
Frontend files read: `contexts/AudioContext.js`, `components/{GlobalAudioPlayer,AudioPlayer,SpeakerIcon,DownloadAudioIcon,StreamingMicButton,RegenerateTtsDialog}.js`, `pages/VoicePage.js`, `hooks/{useVoiceSession,useStreamingMediaRecorder,useMediaRecorder,useStreamingTranscription,useStreamingTTS,useSSE,useMediaSession,useInterruptedRecovery,useAsyncTaskPolling}.js`, `utils/{chunkedUpload,voiceTiming}.js`, `api.js`.
Backend files read: `routes/{voice,sse,drafts,media}.py`, the audio/TTS/status routes in `routes/nodes.py`, `tasks/{tts,streaming_transcription}.py`, the voice parts of `tasks/llm_completion.py`, `utils/{tts_stream,tts_stream_text,llm_stream,audio_storage,audio_processing,webm_utils,voice_timing,encryption}.py`, `config.py`, `configs/nginx.txt`, gunicorn service config.

Dead code (do not port): `hooks/useMediaRecorder.js` (unused), `components/AudioPlayer.js` (unused), `hooks/useStreamingTTS.js` (only used by `StreamingAudioPlayer.js`, which nothing imports).

All paths below are relative to the repo root. API paths are given with their full prefix (`/api/...`, `/media/...`).

---

## 0. The short version

- A voice turn is: record 15 s fMP4/WebM segments and upload each one → `finalize` → the server transcribes, creates the user node and the LLM reply node, and starts the LLM task → the client learns the reply node id → the client attaches to the reply node's TTS SSE stream → MP3 chunk URLs arrive (with `STREAMING_VOICE_TTS=true`, which prod has, chunks are synthesized *while the reply is written*) → the client plays them in a queue → `all_complete` either ends the turn or names a continuation node whose audio goes into the same queue.
- Everything after the user taps Stop is server-driven. The client only needs to (a) upload, (b) find out the reply node id, (c) listen for chunk URLs, (d) play them. The browser's difficulty is (d) on a locked iPhone: nothing keeps the page alive between the finalize request and the first chunk, and nothing can start audio without a tap.
- A native app solves this by keeping its audio session active and audio running without a break from the record tap to the end of the reply (UIBackgroundModes `audio`), and by playing an audible "thinking" cue in every gap. Details and App Review risks are in §8.

---

## 1. State machine of one voice turn (web, as built)

### 1.1 State variables

| Layer | Variable | Values | Where |
|---|---|---|---|
| Page phase | `phase` | `ready`, `recording`, `processing` ("Thinking..."), `playback` | `useVoiceSession` |
| Stop in flight | `isStopping` | bool | `useVoiceSession` |
| Error dot | `hasError` | bool, auto-cleared after 3 s | `useVoiceSession` |
| Transcription session | `sessionState` | `idle`, `initializing`, `recording`, `finalizing`, `complete`, `error` | `useStreamingTranscription` |
| Recorder | `status` | `idle`, `recording`, `paused`, `recorded` | `useStreamingMediaRecorder` |
| Mic interruption | `interrupted` | bool | `useStreamingMediaRecorder` |
| Reply node | `llmNodeId` | int or null; mirrored to URL `?resume=<id>&parent=<id>` | `useVoiceSession` |
| TTS stream attached | `ttsGenerating` | bool; enables the TTS SSE | `useVoiceSession` |
| Queue still growing | `audio.generatingTTS` | bool | `AudioContext` |
| Queue drained, waiting | `audio.waitingForChunks` | bool | `AudioContext` |
| Chain | `pendingContinuationRef`, `continuingChainRef`, `awaitingNextNodeRef` | refs | `useVoiceSession` |
| Delivery bookkeeping (#242) | `ttsTriggeredForNodeRef`, `ttsAttemptAtRef`, `sseDeliveredForNodeRef`, `restDeliveredForNodeRef`, `completionHandledForNodeRef` | refs | `useVoiceSession` |

### 1.2 Transitions

```
            record tap (spend cap ok, online)
 ready ──────────────────────────────────────────► recording
   ▲                                                  │  ▲
   │ startup error / spend cap (402) / fatal upload   │  │ pause (lock screen) / mic interruption
   │◄─────────────────────────────────────────────────┤  │ ◄──► paused (recorder), interrupted flag
   │                                                  │
   │                         stop tap (isStopping=true)│
   │                                                  ▼
   │                                  [finalizing: final chunk emitted, all uploads settled,
   │                                   POST finalize; silent keepalive stops when it returns]
   │                                                  │ draft all_complete (SSE) or /status poll
   │  empty transcript / server warning / API error   ▼
   │◄──────────────────────────────────────────── processing ("Thinking...")
   │  llm failed (toast), TTS failed, cancel ✕           │  first chunk_ready, or REST full-file delivery,
   │                                                     │  or empty/unspeakable reply, or 60 s safety net
   │                                                     ▼
   │                                                  playback ◄──┐ continuation's first chunk
   │                                                     │        │
   │                                                     ├────────┘ interim drained while the
   │                                                     │          continuation isn't ready → processing
   │  "Continue" tap (record button) = handleContinue ────┴──────► recording (skips ready)
```

`handleCancelProcessing` (the ✕ in "Thinking..." or lock-screen *next track* during processing): stops audio, closes the TTS SSE, resets every ref, goes to `ready`. It does **not** cancel anything on the server: the reply and its TTS keep generating and are billed. The next recording parents to `lastUserNodeIdRef`, which is only set on the legacy `/api/voice` path; on the normal server-chain path the next turn parents to the previous thread parent (the cancelled reply becomes a dead-end sibling).

`handleContinue` (record button in playback): `audio.stop()`, TTS SSE disconnect/reset, all refs reset, thread parent kept (= last completed reply node), `phase = recording`, silent keepalive restarted, `startStreaming(parent)`.

### 1.3 Timeline of a normal turn (prod config, `STREAMING_VOICE_TTS=true`)

Client side is `useVoiceSession` + `useStreamingTranscription` + `useStreamingMediaRecorder` + `AudioContext`; server side is noted in brackets.

1. **Record tap** → `handleStart`: `phase=recording`; `startSilentAudio()` (iOS only, see §7); `POST /api/drafts/streaming/init` `{parent_id, privacy_level:'private', ai_usage, label:'Voice'}` → `201 {session_id, draft_id, sse_url}` [creates `Draft(streaming_status='recording')` and dir `data/audio/drafts/<uid>/<session_id>/`]. 402 = monthly spend cap (toast, back to `ready`, mic never opened).
2. `getUserMedia({audio:true})`; `new MediaRecorder(stream, {mimeType})`; `start(15000)`. Duration ticks every 1 s. Draft transcription SSE opens (`GET /api/sse/drafts/<sid>/transcription-stream`).
3. **Every 15 s** `ondataavailable` → `POST /api/drafts/streaming/<sid>/audio-chunk` multipart (§2.3). The server stores the chunk; every 20 stored chunks (5 min) it queues `transcribe_chunk_batch`.
4. **Stop tap** → `handleStop`: `voiceTiming.startTurn()` (mark `rec_stop`); `isStopping=true`; non-iOS: `audio.warmup()`; `stopStreaming({parent_id, model})`:
   - `sessionState=finalizing`; `recorder.stop()` → final `ondataavailable` (upload enqueued) → `onstop`;
   - `await Promise.allSettled(pendingUploads)`;
   - `POST /api/drafts/streaming/<sid>/finalize {total_chunks, label:'Voice', parent_id?, model?}` (timeout 120 s) → 202;
   - `.finally`: mark `finalize_acked`; `stopSilentAudio()`. **From here on nothing keeps an iOS page alive.**
   - [Server `finalize_draft_streaming`: optional Anthropic cache prewarm; transcribes the remaining stored chunks as one batch (`gpt-4o-transcribe`); polls chunk rows every 0.25 s (max 600 s); builds the transcript; `_start_server_side_llm_chain` creates system node (fresh thread) → user node (transcript, `created_at = recording start`, audio moved to `data/audio/nodes/<uid>/<user_node_id>/`) → LLM placeholder with `tts_task_status='pending'`; sets `draft.llm_node_id` and `streaming_status='completed'` in one commit; enqueues `generate_llm_response(..., source_mode='voice')`.]
5. **Reply node known**: draft SSE `all_complete {content, draft_id, llm_node_id, warning?}` or, as a fallback, `GET /api/drafts/streaming/<sid>/status` polled every 5 s (first poll 5 s after entering `finalizing`, and immediately on `visibilitychange→visible`). `onComplete` → `phase=processing`, `audio.stop()` (clears stale audio from other pages), then:
   - empty transcript → `ready`; `warning` (spend cap, bad `{user_export}` placeholder) → toast, `ready`;
   - `llm_node_id` present (normal) → `setLlmNodeId(id)`; mark `llm_node_known`;
   - absent (server chain failed or skipped) → legacy `POST /api/voice {content, model?, ai_usage?, parent_id?, session_id}` → `202 {parent_id, user_node_id, llm_node_id, task_id}`.
6. **Reply generating**: `GET /api/nodes/<id>/llm-status` every 1500 ms (request timeout 10 s, cap 30 min; immediate re-poll on foreground). [LLM task: `_start_voice_tts_stream` sets the node `tts_task_status='processing'`, `tts_task_id='voice-stream'`, and starts a TTS worker thread fed by the model's streamed text (§5.2).]
7. **Early attach (#367)**: when a poll returns `tts_streaming: true` for a non-terminal node, mark `tts_attach`, set the next chapter title to the placeholder `…`, `ttsGenerating=true` → `EventSource GET /api/sse/nodes/<id>/tts-stream`.
8. **First `chunk_ready {chunk_index, audio_url, duration, section_index?, section_title?}`**: mark `chunk_ready`; `stopSilentAudio()`; `audio.loadAudioQueue([url], {title:'Voice', url, chapters}, [duration], {onPlaying: markPlaying})` → `play()`; `audio.setGeneratingTTS(true)`; `phase=playback`. On iOS `play()` rejects with `NotAllowedError` (mark `autoplay_blocked`) and the user must press the on-page or lock-screen play button.
9. **Later chunks** → `audio.appendChunkToQueue(url, duration, chapterTitle)`. If playback had drained (`waitingForChunks`), the new chunk auto-plays.
10. **Node completes** (llm-status `completed`): once per node the page callback runs (`onLLMComplete` → re-fetch llm-status for `tool_calls_meta` → `ProposalInline` card), thread parent := this node (unless it is an interim node with `continuation_node_id`), URL `parent` updated. If the TTS was attached early, only the chapter title is fixed; otherwise (flag off) this is where `POST /api/nodes/<id>/tts` fires (§5.4).
11. **`all_complete {tts_url, continuation_node_id, preview}`**: retitle the node's placeholder chapter from `preview`. If `continuation_node_id` is set → `advanceChain(nextId)`: SSE reset, `ttsGenerating=false`, keep `audio.generatingTTS=true`, `setLlmNodeId(nextId)`; the continuation is polled and attached like steps 6–9 but appends to the same queue. Otherwise `ttsGenerating=false`, `audio.setGeneratingTTS(false)`.
12. **Queue ends**: last `onended` with `generatingTTS=false` → `isPlaying=false`, position shown at the end. Phase stays `playback` (replay, seek, chapters, "Continue" button).
13. `markPlaying` (first `playing` event) → `GET /api/voice/timing/clock` ×3 → `POST /api/voice/timing {node_id, marks, offset_ms, rtt_ms}` (§4.6).

### 1.4 Timers and intervals (all of them)

| What | Value | Where |
|---|---|---|
| Recorder timeslice (chunk length) | 15 000 ms | `useStreamingTranscription` default, `useStreamingMediaRecorder` |
| Recording duration tick | 1 s | `useStreamingMediaRecorder` |
| Lock-screen title refresh while recording | 1 s | `useMediaSession` |
| Chunk upload retries | 4 retries (5 attempts), delays 2/4/8/16 s; per-attempt timeout 600 s | `uploadChunkWithRetry` |
| Finalize request timeout | 120 s | `stopStreaming` |
| Draft status poll during `finalizing` | first at 5 s, then every 5 s; immediate on foreground | `useStreamingTranscription` |
| Draft SSE stale check | no event for 45 s (checked every 10 s) → reconnect with `?last_chunk=` | `useDraftTranscriptionSSE` |
| SSE reconnect after `CLOSED` | 3 s | `useSSE` |
| llm-status poll | 1500 ms, request timeout 10 s, gives up after 30 min | `useAsyncTaskPolling` |
| TTS recovery reconcile | every 7 s + on foreground | `TTS_RECOVERY_POLL_MS` |
| `/tts` trigger watchdog | 20 s without outcome → re-fire POST | `TTS_TRIGGER_WATCHDOG_MS` |
| Catch-up grace after `/tts-status` says completed | 3 s (skipped on foreground) | `TTS_SSE_CATCH_UP_MS` (introduced, unmeasured) |
| No-audio safety net | 60 s in `processing` with SSE on and node completed → `playback` | `useVoiceSession` |
| Error dot | cleared after 3 s | `useVoiceSession` |
| Player time tracking | 100 ms | `AudioContext.startTimeTracking` |
| Chunk duration probe | 3 s timeout, fallback 300 s | `preloadChunkDurations` |
| Replay-from-end threshold | within 0.5 s of end → seek 0, play after 50 ms | `VoicePage` |
| Interruption toast | 24 h (cleared on resume/stop) | `useStreamingTranscription` |
| Text-mode long-recording chime | at 59 min (text mode only, not voice) | `StreamingMicButton` |
| Server: finalize chunk poll | 0.25 s, max 600 s | `FINALIZE_POLL_SECS` |
| Server: SSE DB poll | 1 s (tts, draft), 0.5 s (llm-stream); heartbeat 15 s | `routes/sse.py` |
| Server: SSE max lifetime | tts-stream 1800 s, llm-stream 1800 s, draft stream 7200 s, node transcription stream 600 s → `event: close` | `routes/sse.py` |
| Server: partial reply text write | every 0.5 s | `PARTIAL_WRITE_INTERVAL_SECS` |
| nginx `/api/sse/` | read/send timeout 7200 s, buffering off | `configs/nginx.txt` |
| nginx `/api/` | read timeout 60 s | `configs/nginx.txt` |

---

## 2. Recording

### 2.1 Format and MIME

- Preference order (`PREFERRED_MIMES`): `audio/webm;codecs=opus`, `audio/webm`, `audio/mp4;codecs=mp4a.40.2`, `audio/mp4`. Chrome/Firefox get WebM/Opus; iOS Safari (and any WebKit browser) gets fragmented MP4 with AAC-LC.
- `?force_mime=mp4|webm` URL param restricts the negotiation (debug knob).
- A resumed session must use the family chunk 0 used (`forceMimeFamily` from `/api/drafts/interrupted`'s `streaming_mime_type`); otherwise the server answers `400 {code:'mime_mismatch'}`.
- The server accepts only the families `audio/webm` and `audio/mp4` (form field `mime_type`, codec params stripped and lowercased). Stored as `chunk_NNNN.webm|.mp4` (+ `.enc` when KMS encryption is on, which it is in prod).

**Byte-level contract (what a native encoder must reproduce).** MediaRecorder with a timeslice emits pieces of one continuous stream:
- chunk 0 = init segment + first fragment. For fMP4: `ftyp` + `moov`, then `moof`+`mdat`. The server (`extract_mp4_init_segment`) walks top-level boxes and **requires a `moov` before the first `moof`/`mdat`, and at least one `moof`/`mdat` in chunk 0**; otherwise `400 {code:'init_parse_failed', detail}` and the client treats the session as dead.
- chunks 1..N = bare fragments (`moof`+`mdat`) whose timestamps continue from the previous chunk. The server concatenates raw bytes (init segment prepended when a batch does not start at chunk 0), then `ffmpeg -fflags +genpts -i raw -c copy out.mp4`.
- A chunk N>0 that **starts with `ftyp`** (or an EBML header) is treated as a new *subsession* (#124): its init segment is stored as `init.<N>.mp4`, and each subsession in a batch is transcribed by a separate `gpt-4o-transcribe` call, transcripts joined with a blank line. The web creates such chunks on every resume (new MediaRecorder).
- A plain `.m4a` from `AVAudioRecorder` (moov at the end, after `mdat`) would fail the chunk-0 check. Self-contained per-chunk MP4 files with `moov` first would pass but turn every 15 s chunk into its own subsession (one transcription call per 15 s piece, words cut at boundaries, one playback file per chunk).

### 2.2 Chunking and lifecycle

- `mediaRecorder.start(15000)`; each `ondataavailable` with data → `chunkIndex++` → `onChunkReady(blob, index)` → upload.
- Pause (`pauseRecording`): `requestData()` (flushes the partial timeslice as a chunk, so a long pause cannot lose audio) then `pause()`.
- Resume: **always re-acquires the mic** with a new `getUserMedia` + new `MediaRecorder` (a paused recorder's capture dies silently on iOS whenever the OS reclaims the audio session, with no `mute`/`ended` event). The new recorder's first chunk is init-bearing → server subsession split. Chunk numbering continues.
- Stop: `recorder.stop()` without a preceding `requestData()` (that raced and lost the final chunk). A promise resolves when the final `ondataavailable` has enqueued its upload, or in `onstop` if no data came.
- Lifecycle flush (#88): on `visibilitychange→hidden` and `pagehide`, `requestData()` so buffered audio is uploaded while the page can still run.
- Chunks are also kept in memory (`chunksRef`) for a local "Save audio" download (text-mode button only) via `getPartialBlob()`.

### 2.3 Upload protocol

`POST /api/drafts/streaming/<session_id>/audio-chunk`, `multipart/form-data`:
- `chunk`: file, filename `chunk_<index>.mp4|.webm`
- `chunk_index`: integer string, 0-based, continuing across resumes
- `mime_type`: e.g. `audio/mp4` (codec suffix allowed)

Responses:
- `202 {chunk_index, status:'stored', task_id?, batch_queued?}` — stored (a 20-chunk transcription batch may have been queued).
- `200 {message:'Chunk already uploaded', chunk_index, status}` — duplicate (answered before touching files; concurrent duplicates hit a unique constraint and also get 200).
- `400` missing file / bad index / unsupported mime / `mime_mismatch` / `init_parse_failed` / "Streaming session is not active" (draft not `recording` or `finalizing`).
- `404` session or its directory not found.

Ordering: uploads run concurrently (each chunk fires its own request). Order is not enforced by the server, but it matters: a 20-chunk batch that forms before chunk 0 is stored has no init segment and fails. Retries: 4 with exponential backoff; a non-fatal final failure is kept in memory and retried on the browser `online` event. `init_parse_failed` is fatal: the recorder is reset and the turn ends with a toast. When the page is hidden, each upload is also sent once by `navigator.sendBeacon` (survives page death; the server dedupes).

Finalize: `POST /api/drafts/streaming/<sid>/finalize {total_chunks, label, parent_id?, model?}` → `202 {message, task_id, draft_id, total_chunks}`. `400` if the draft is no longer in `recording` (a retried finalize after a lost response gets this). `total_chunks` is the number of chunks the recorder produced, including failed uploads; **the server then waits up to 600 s for chunks that never arrive** (see §10).

### 2.4 Long recordings

- Transcription runs every 20 chunks (5 min) during recording; the remainder (< 5 min) is transcribed at finalize, so a short voice turn's transcript is produced only after Stop (stage `a2`: ~2 s for a few seconds of audio, ~16 s for 4 min).
- Batch audio is merged per subsession, encrypted and stored as `batch_<first>-<last>.mp4|.webm(.enc)`; the individual chunk files are deleted.
- Very long transcripts are split into a serial chain of nodes (`split_node_into_chain`); the reply parents to the tip.
- Known ~60-minute degradation cliff (#127). The 59-minute warning chime exists only in text-mode dictation (`StreamingMicButton`); voice mode has none (#243 gap).
- Interrupted sessions (tab killed, app crash): `GET /api/drafts/interrupted` lists drafts still `recording` that have chunk rows → `RecoveryBanner` on the Voice page: "continue" resumes recording into the same session (`resumeStreaming(sessionId, draftId, chunkCount, mimeType)`, duration offset = chunkCount × 15 s) or "discard" (`DELETE /api/drafts/streaming/<sid>/discard`). Text mode can also `POST .../transcribe-remaining` and poll `/status` every 2 s (max 5 min).

### 2.5 Mic interruption (#88, #245)

- The mic track's `mute` and `ended` events (phone call, Siri, lock screen on some versions) → `markInterrupted()`: flush (`requestData`), `pause()`, `status=paused`, `interrupted=true`. `onstop` caused by a dead track (not a user stop) is also held as paused.
- Alert once per episode: a loud doubled descending square-wave chime (660/440/330 Hz) + a 24 h toast "Recording paused — another app took the microphone...". The chime is re-played when the page returns to the foreground while still interrupted (the OS often does not route app audio during a call).
- No automatic resume on `unmute`. The user resumes via the page or the lock-screen play button. `getUserMedia` is refused while the page is hidden, so a lock-screen resume is deferred until the page is visible (`pendingResumeRef`); the lock-screen title says "Paused m:ss — play, then unlock". A re-entrancy guard stops a second press from starting two recorders.
- Upload failures play a short descending error sound.

### 2.6 Wake lock

None. The web app uses no Screen Wake Lock. On iOS the screen may lock during a recording; recording continues because the silent keepalive (§7) keeps the audio session up.

---

## 3. Transcription

- **Streaming (draft-based) is the only path in voice mode.** No node exists until finalize; the transcript accumulates in `Draft.content`.
- Model: `gpt-4o-transcribe` (`response_format='text'`), per batch of up to 20 chunks, per subsession. Cost logged in `APICostLog` (`request_type='transcription'`).
- Partial text: `GET /api/sse/drafts/<sid>/transcription-stream[?last_chunk=N]` (session cookie). Events:
  - `chunk_complete {chunk_index, text, status:'completed'}` (the batch's text is on its first chunk; the others carry `""`)
  - `chunk_error {chunk_index, error, status:'failed'}`
  - `content_update {content, completed_chunks}` (the full draft text whenever it changes)
  - `all_complete {message, content, draft_id, warning?, llm_node_id?}` — **the server deletes the draft right before sending this when `llm_node_id` or `warning` is set**
  - `error {message}`, `heartbeat {timestamp, completed_chunks, total_chunks, status}` every 15 s, `close` at 2 h
  Voice mode does not display the transcript; text-mode dictation (`NodeForm` + `StreamingMicButton`) appends it to the editor as it arrives, i.e. in 5-minute steps.
- Status (REST fallback): `GET /api/drafts/streaming/<sid>/status` → `{session_id, draft_id, streaming_status, streaming_mime_type, total_chunks, completed_chunks, failed_chunks, chunks:[{chunk_index,status,text,error}], content, llm_node_id?, warning?}`. **Side effect:** if the draft is still `recording` and no chunk is pending, it is marked `completed` (meant for refresh recovery; issue #320: a second tab calling this ends a live recording).
- Node creation: server-side in `_start_server_side_llm_chain` (voice label only). Fresh thread: system node (the user's `voice` prompt + context artifacts; created early at finalize start when the Anthropic cache prewarm runs) → user node (transcript; `streaming_transcription=True`; transcript-chunk rows re-pointed; audio dir moved) → LLM placeholder. With a `parent_id`, the user node goes under it and `ai_usage` is inherited (`reply_ai_usage`). Chunks that failed to transcribe are noted in the text (`*Note: Chunks [..] failed to transcribe*`).
- Uploaded audio files (text mode, not voice): `< 10 MB` → `POST /api/nodes/` multipart `audio_file`; larger → `POST /api/nodes/upload/init` → `/upload/chunk` (5 MB pieces, 3 attempts, 1/2/4 s backoff, 120 s timeout) → `/upload/finalize`; then `GET /api/nodes/<id>/transcription-status` polled (`utils/chunkedUpload.js`).
- Dictation into the text editor finalizes without the `Voice` label (no LLM chain; a top-level note gets a `# <timestamp> Voice note` heading) and is saved with `POST /api/drafts/streaming/<sid>/save-as-node`.

---

## 4. Reply generation and streaming

### 4.1 How the reply starts

No auto-generate setting is involved in voice mode: `finalize` with `label='Voice'`, a model (the client sends `user.preferred_model`; the server resolves one otherwise via `pick_model_for_generation`) and a non-empty transcript always starts the LLM task server-side. The legacy `POST /api/voice` path is a fallback only.

Other entry: "Voice" from a node page → `POST /api/voice/from-node/<node_id> {model}` → `{mode:'processing', llm_node_id, parent_id, fresh?}` → `/voice?resume=<llm_node_id>&parent=<parent_id>` (the page starts in `processing` polling that node; `fresh` = a new placeholder was created and is generating).

### 4.2 Feature flags

| Flag | Default in code | Prod | Effect |
|---|---|---|---|
| `STREAMING_REPLIES` | on | on | reply text written to `Node.streaming_content` (encrypted, 0.5 s) and served by `/api/sse/nodes/<id>/llm-stream` |
| `STREAMING_VOICE_TTS` | **off** | **on** (set in prod env 2026-09-28; on in staging compose) | voice replies are spoken while written by a TTS thread inside the LLM task |

A native client must work with both values (§5.3–5.4).

### 4.3 `GET /api/nodes/<id>/llm-status`

`{node_id, status: pending|processing|completed|failed|cancelled, progress, error, task_info, continuation_node_id, tts_task_status, tts_streaming (tts_task_id=='voice-stream'), content (only completed/cancelled), tool_calls_meta?, stage?/batch_submitted_at? (reads), feed_picks_count?, warnings: [...], node?}`; `Cache-Control: no-cache`. The voice client ignores `cancelled` (would stay on "Thinking...").

### 4.4 Reply text stream (used by the node page, not by the Voice page)

`GET /api/sse/nodes/<id>/llm-stream`: `snapshot {text}` on connect and when the model call restarted, `delta {text}` appended text, `done {status, continuation_node_id, error}` when the node is no longer pending/processing (then the stream closes), `heartbeat`, `close` at 30 min. Final content comes from the node / llm-status. A native voice screen could show the reply text live with this, and could use `done` instead of polling llm-status.

### 4.5 Within-turn chain (#158)

When the model calls a tool, the current node is finalized as an *interim* node (its text or a fallback line such as "on it…"), a continuation node is created with `continuation_node_id` pointing to it, and the next call writes the continuation. Up to 5 tool rounds. Each node's audio goes into the same playback queue, one chapter per node (Roman numerals + first 44 chars of the node's text on the Voice page). With streaming TTS the continuation is created with `tts_task_status='processing'` so its stream can be attached as soon as the client moves on.

### 4.6 Timing instrumentation (#371)

Browser marks: `rec_stop`, `finalize_acked`, `llm_node_known`, `tts_attach`, `chunk_ready`, `autoplay_blocked`, `play_pressed`, `playing`. Backend marks per reply node in Redis (7-day TTL). `GET /api/voice/timing/clock → {t}`; `POST /api/voice/timing {node_id, marks:{stage: epoch ms}, offset_ms, rtt_ms}`; `GET /api/voice/timing?limit=` (user's recent turns + per-stage medians); admin `GET /api/admin/voice-timing`. Staging numbers (iPhone, 5 turns): transcription 2.4–3.3 s after Stop (9.9 s cold), model thinking 6.6–13.9 s (largest stage), TTS stages 2–5 s. The native client should send the same marks (`playing` without a tap now).

---

## 5. TTS

### 5.1 Common audio facts

- OpenAI `gpt-4o-mini-tts`, voice `alloy`, MP3 128 kbps CBR (16 000 bytes per audio second). Called with streaming response, SDK retries off.
- #382/#383 (merged 2026-09-30): a call that runs ≥ 290 s raises `TTSCutOff` (OpenAI silently cuts at 300 s since 2026-09-27); a call that after 30 s has delivered less audio than elapsed time, or sends nothing for 30 s, is cancelled and resent, up to 2 more times; if all fail the chunk file is removed and `TTSCallFailed` raised. Client-visible: a chunk can take tens of seconds (up to ~90 s across retries) to appear; a chunk that fails ends that node's TTS as `failed`.
- Text preparation: quote markers `{quote:ID}` removed, `:::share` fenced blocks never spoken, for proposal replies only the prose parts are spoken (intro, `### Note`, trailing commentary), edge timestamps dropped. h1/h2 headings start chapters; the heading is spoken as its own sentence; the last chunk of a chapter gets 900 ms of trailing silence.
- Storage: `data/audio/user/<uid>/node/<node_id>/` (profiles `.../profile/<pid>/`, saved references `.../item/<iid>/`), files `tts_chunk_<i>.mp3` and the joined `tts.mp3`, each encrypted to `*.enc` in prod.
- URLs: `/media/user/<uid>/node/<id>/tts_chunk_<i>.mp3?v=<id of the run's chunk-0 row>` and `.../tts.mp3?v=...`. A single-chunk batch reply's chunk URL *is* the `tts.mp3` URL. `?v=` busts nginx's cache after a regeneration.
- `TTSChunk` rows: `(node_id|profile_id|item_id, chunk_index, status pending|processing|completed|failed, audio_url, duration (s, from pydub), section_index, section_title, completed_at)`.

### 5.2 Streaming voice TTS (flag on) — `utils/tts_stream.py`, `utils/tts_stream_text.py`

- One `VoiceTTSStream` worker thread per voice turn inside the LLM Celery task. Each node of the turn gets a `NodeSpeech`; the model's text deltas are fed through `SpokenTextProjector` (releases text only when nothing later can scrub it) into `ChunkPlanner` (section-bounded chunks cut at confirmed sentence ends).
- Chunk sizing (`SpeechSchedule`), from calibrated rates (106 chars/s generation, 0.062 s audio per char, 7 s per-chunk overhead): while nothing is queued, cut as soon as the finished sentences reach 80 chars (`MIN_FIRST_CHUNK_CHARS`) (#371); otherwise size the next chunk by the time left before the listener's queue runs out (estimated server-side from durations; the server cannot see the player), cutting early 2.5 s before the estimated drain (`REFILL_LEAD_SECS`). Max 4096 chars. First chunk otherwise ≤ 318 chars.
- For each chunk: insert `TTSChunk(status='processing')` → synthesize → encrypt → set `audio_url`, `duration`, `status='completed'` → commit (the URL is only visible once the file is encrypted in place).
- When a node's text is complete and the node is committed (`release`), its segments are joined into `tts.mp3`, cost is logged, `tts_task_status='completed'`. So `all_complete` for a node always carries its `continuation_node_id`.
- Failure: a chunk exception marks the node's TTS `failed` and stops speaking it; a failed turn (`abort`) marks unfinished nodes failed. A node deleted mid-reply stops with a warning.

### 5.3 Batch TTS (flag off, and every non-voice listen) — `tasks/tts.py`

`generate_tts_audio(node_id, audio_root, requesting_user_id)`: spend-cap check; skips when `audio_original_url` exists; `section_aware_chunk_text`; deletes old `TTSChunk` rows, creates pending rows; synthesizes chunk by chunk (each committed `completed` with its URL as it finishes, then encrypted); joins; sets `audio_tts_url`, `tts_task_status='completed'`. In voice mode it is dispatched by the LLM task at each node's own finalization (`_dispatch_voice_tts`, status set to `pending` in the same commit as the node's completion), interim nodes included.

### 5.4 Client protocol for TTS

**Trigger** `POST /api/nodes/<id>/tts` (login + voice mode + spend headroom): `409` original recording exists; `200 {message, tts_url}` already generated; `202 {message, task_id, status, node_id}` in progress (no re-enqueue) or just enqueued; `500` no API key; `402` spend cap; `403` no voice mode. Idempotent. Profiles: `POST /api/profile/<id>/tts`; references: `POST /api/external/items/<id>/tts`.

**Status** `GET /api/nodes/<id>/tts-status` → `{node_id, status, progress, task_info, node?:{id, audio_tts_url}}` (profiles/items have their own).

**Stream** `GET /api/sse/nodes/<id>/tts-stream[?last_chunk=N]` (also `/api/sse/profiles/<id>/tts-stream`, `/api/sse/items/<id>/tts-stream`). Served as `text/event-stream` only when `tts_task_status` is `pending`, `processing` or `completed`; otherwise a **JSON** answer (`200 {status:'completed', tts_url}` if a URL exists, else `400 {error}`) that an EventSource cannot use. Events:
- `chunk_ready {chunk_index, audio_url, status:'ready', duration?, section_index?, section_title?}` — every completed chunk with index > `last_chunk`, in order
- `all_complete {message, tts_url, continuation_node_id (nodes only), preview (first 200 chars of the node text, nodes only)}` then the stream ends
- `error {message:'TTS generation failed'}` then the stream ends
- `heartbeat {timestamp, completed_chunks, total_chunks, progress}` every 15 s
- `close {message:'Connection timeout'}` after 30 min
A completed node's stream replays every chunk after `last_chunk` and then `all_complete` (#376), so a reconnect after completion is safe. The web client never sends `last_chunk` for TTS; it dedupes instead.

**How the Voice page decides**
- Flag on: attach the SSE as soon as llm-status says `tts_streaming`; no POST.
- Flag off: on llm-status `completed`, `POST /tts` → `200` = deliver the whole `tts.mp3` via REST (`deliverFullTts`); `202` = enable the SSE (only after the POST returns, so the stream does not open before `pending` is set).
- Empty or unspeakable reply (e.g. only a proposal card): no TTS; go to `playback` with nothing to play.

### 5.5 Client playback queue (`AudioContext`)

- `loadAudioQueue(urls, meta, serverDurations, {onPlaying})`: pause any current audio; start loading chunk 0 immediately (reusing the gesture-warmed element if there is one); use server durations if all present, else probe each URL's metadata (3 s timeout → 300 s fallback); `playChunkAtTime(0, 0, autoplay)`.
- `playChunkAtTime(i, t)`: new playback id (stale handlers ignored); **reuses the same `<audio>` element** by setting `src` (iOS autoplay permission is per element); applies the current rate from a ref; seeks at `canplay`; corrects the stored duration on `loadedmetadata` (> 1 s off) and on `ended` (> 0.5 s off); `onended` → next chunk, or `waitingForChunks=true` if the queue is empty but `generatingTTS`, or finished.
- `appendChunkToQueue(url, duration, chapterTitle)`: **dedupe: skip if the URL is already in the queue** (covers SSE replays and a POST-200 racing a chunk_ready); record a chapter anchored to the chunk index; auto-play the new chunk if the queue had drained.
- `useTTSStreamSSE` dedupe: a `Set` of received `chunk_index` values per URL (reset on URL change and `reset()`), because a reconnect replays from chunk 0.
- Chapters: `{title, start_time, chunk_index}`; the start time is recomputed from live chunk durations (`chapterStartTime`), falling back to `start_time` for a merged single file. `renameChapter(chunkIndex, title)` fixes a streamed node's `…` placeholder title from `all_complete.preview` (or from the content when the node completes first).

### 5.6 Stall detection and recovery (#242, #376) — why it exists and what it does

iOS Safari suspends the page when it is backgrounded or locked with no audio playing, and silently kills in-flight XHRs and EventSources (no error event). The Voice page therefore reconciles against REST while a turn is undelivered (`phase==='processing' || ttsGenerating`), every 7 s and on foreground:
1. **Lost trigger**: node completed, not delivered, SSE off, last `/tts` attempt > 20 s ago → re-arm and re-fire `POST /tts`. A POST that failed without an HTTP response re-arms instead of failing the turn.
2. **Dead or lagging SSE**: `GET /tts-status`; if `completed`, wait 3 s for the SSE to catch up (not on foreground), then: if some chunks of this node came by SSE → `reconnect()` (the stream replays; dedupe makes it safe); if none → `deliverFullTts(tts.mp3)` via REST (later SSE chunks for that node are then ignored). `failed` → error, `ready`.
3. **60 s safety net** (§1.4).
The #376 bug: the reconcile closed a healthy stream up to 1 s before it delivered the last chunk (the SSE polls the DB once a second), and the reconnect got the JSON answer; the reply stopped at 19 s of 56. Fixed by serving completed nodes as a replaying stream plus the 3 s grace.

### 5.7 Regenerate TTS (#66)

Editing the text of a node, profile or saved reference that has generated TTS opens `RegenerateTtsDialog` ("Regenerate audio" / "Keep existing audio" / Cancel). Regenerate → the update request carries `regenerate_tts: true` (node `PUT`, profile update, item update) → `clear_tts_artifacts`: `audio_tts_url=None`, `TTSChunk` rows deleted, `tts_task_id/status` cleared (files stay on disk as orphans). The next play generates fresh audio with a new `?v=`. `SpeakerIcon` drops its cached URLs when `content` changes. Original recordings are never touched.

---

## 6. Playback outside voice mode

### 6.1 Speaker icon (`SpeakerIcon`, on nodes, profiles, saved references)

Shown when `user.voice_mode_enabled` or the node is public; disabled (35 % opacity) when the node's `ai_usage === 'none'`. On tap (`warmup()` first for Safari):
1. Cached in this component: replay the chunk list or single URL with the cached chapters.
2. `GET /api/nodes/<id>/audio` → `200 {original_url, tts_url, has_audio_chunks}` (only files that exist) | `202 {status:'generating', progress, task_id}` | `404`. Public nodes: any logged-in user; private: voice mode required. Profiles `GET /api/profile/<id>/audio`, items `GET /api/external/items/<id>/audio`.
3. Preference: the **original recording** first. If `has_audio_chunks`: `GET /api/nodes/<id>/audio-chunks` → `{chunks:[{url, duration}]}` (URLs `/media/nodes/<uid>/<id>/batch_0000-0019.mp4` or `chunk_NNNN.*`, durations from ffprobe, 300 s fallback) → `loadAudioQueue`. Else `original_url` (uploaded file) or `tts_url` → `loadAudio` single file; for TTS, chapters from `GET /api/nodes/<id>/tts-chapters` → `{chapters:[{section_index, title, chunk_index, start_time}]}` (empty unless > 1 section).
4. Nothing exists and voice mode is on: `POST /tts` then the TTS SSE: first chunk → `loadAudioQueue` immediately (chapters fetched in parallel and applied when they arrive — #376 fix: waiting for chapters dropped chunks that arrived meanwhile), later chunks appended, chapters re-fetched when a chunk opens a new section, `all_complete` → cache `tts_url`, re-fetch final chapters, `onTtsGenerated()` (makes the edit dialog aware the node now has TTS).
5. A `useAsyncTaskPolling` on `tts-status` exists as a fallback, but nothing calls `setTtsTaskActive(true)`, so it never runs. The speaker icon has none of the Voice page's REST recovery: a stream that iOS kills without an error event can leave the spinner on.

### 6.2 Global player (`GlobalAudioPlayer`, in the nav bar; hidden on `/voice`)

- One player for the whole app (`AudioProvider`). Shows title (first markdown heading of the content, else "Node N"/"Profile N"/"Reference N"), a chapter `<select>` (> 1 chapter; follows the play position; selecting seeks), Play/Pause, **Stop** (pause, release the element, position 0, player stays visible and replayable, #161), skip −10 s / +10 s, playback-rate button cycling **1 → 1.25 → 1.5 → 2 → 1** (rate kept across chunks via a ref), `m:ss / m:ss` with a pulsing dot while TTS is still generating, a click-to-seek progress bar (desktop only), and ✕ **Close** (full teardown, hides).
- Mobile (< 640 px): floating card at the bottom, one row, no seek bar; publishes `--floating-player-offset` so toasts sit above it.
- Seek works across chunks (`seekToCumulativeTime`: find chunk by cumulative durations, load it, keep playing state; seeking to within 0.1 s of the end just shows the end).

### 6.3 Voice page player

Own UI in `playback`: skip −10 s, play/pause (if at the end, replay from 0), skip +10 s, progress bar with chapter ticks, time and duration with the generating dot, the chapter list (tap = seek and play), `ProposalInline` (todo/issue/feedback/share proposal card from `tool_calls_meta`), the "Continue" record button, a "Text Mode" button (→ `/node/<last reply>` or `/textmode`).

### 6.4 Download (`DownloadAudioIcon`, node page)

Same visibility rules. `GET /api/nodes/<id>/audio` then: `original_url` → fetch and save as `node-<id>-recording.<ext>`; else `has_audio_chunks` → `GET /api/nodes/<id>/audio-download?format=mp3` (server decrypts every chunk/batch, concatenates with ffmpeg — MP4 sources re-encoded to AAC — then encodes MP3; `format=original` returns the merged `.mp4`/`.webm`) → `node-<id>-recording.mp3`; else `tts_url` → `node-<id>-tts.mp3`. Text-mode "Save audio" downloads the local in-memory recording blob (always named `.webm`, even for MP4 content).

### 6.5 Media Session (lock screen)

- Only `useMediaSession`, only in voice mode, only on iOS:
  - `recording`: `play`→resume, `pause`→pause, `nexttrack`→**stop and send**; others cleared; `playbackState` follows pause; metadata title `Recording m:ss` / `Paused m:ss — play, then unlock`, artist `Loore`, artwork `/apple-touch-icon.png`, refreshed every 1 s.
  - `processing`: title `Voice…`, only `nexttrack`→**cancel**; `playbackState='playing'`.
  - `playback`: title `Voice`, all handlers cleared (the browser's default controls for the `<audio>` element apply).
  - `ready`: metadata cleared.
- iOS lock-screen buttons follow the real audio element state, not the declared `playbackState` (#245).
- Outside voice mode no metadata is set: the lock screen shows the browser's default for the playing element.

---

## 7. iOS Safari and mobile workarounds already in the code (each one is a constraint)

| # | Workaround | Code | Constraint it reveals |
|---|---|---|---|
| 1 | Silent audio keepalive during recording: a WebAudio oscillator at gain 0 → `MediaStreamDestination` → `<audio>.srcObject`, started inside the record tap. A stream (no duration) avoids a cycling progress bar on the lock screen. | `useVoiceSession.startSilentAudio` | Safari only shows lock-screen controls and keeps running JS while an audio element is playing; it must start within a user gesture. |
| 2 | The keepalive runs until the finalize request returns, then stops. | `handleStop` `.finally(stopSilentAudio)` | Without audio, iOS suspends the page within seconds, killing the last upload/finalize. **After this point nothing keeps the page alive: this is the gap the native app must close.** |
| 3 | Keepalive paused during a mic interruption only, kept playing during a user pause. | `useVoiceSession` effect | The lock screen shows Pause/Play from the element's real state; pausing the keepalive tears down the audio session, which kills capture. |
| 4 | No autoplay on iOS: playback UI with a play button; the lock-screen play button also works; timing excludes the tap. | `AudioContext.playChunkAtTime`, `voiceTiming` | Safari blocks `play()` that does not follow a gesture, even with warm-up. The reply can never start by itself on iOS web. |
| 5 | `warmup()` (silent WebAudio buffer + a silent `<audio>` played in the gesture and reused later) is used on desktop only; skipped on iOS. | `AudioContext.warmup`, `handleStop` | On iOS it does not unlock autoplay, and playing silent audio while the mic stream is active crashed Bluetooth headphones on multi-device setups. |
| 6 | One `<audio>` element reused for every chunk. | `playChunkAtTime` | iOS ties autoplay permission to a specific element; a fresh `Audio()` mid-reply was blocked, so playback stopped at chunk boundaries. |
| 7 | `stop()`/`closePlayer()` fully release the element (`removeAttribute('src')`, `load()`). | `AudioContext` | A paused element with loaded media holds Bluetooth A2DP and blocks the switch to HFP when the mic is next opened. |
| 8 | REST recovery for the TTS delivery (watchdog 20 s, reconcile 7 s, catch-up 3 s, foreground reconcile, SSE reconnect, full-file delivery, 60 s net). | `useVoiceSession` (#242, #376) | iOS kills XHRs and EventSources on background/lock without any error event. |
| 9 | Draft status polling (5 s + on foreground) alongside the draft SSE. | `useStreamingTranscription` | Same: `all_complete` can be lost while backgrounded. |
| 10 | Poll immediately on `visibilitychange→visible`; `Cache-Control: no-cache` on polls. | `useAsyncTaskPolling` | Timers are throttled in the background; Safari cached poll responses. |
| 11 | Flush the partial timeslice on `visibilitychange→hidden`/`pagehide`; duplicate upload with `sendBeacon` when hidden; server dedupes by chunk index (and a concurrent duplicate returns 200, #372). | `useStreamingMediaRecorder`, `useStreamingTranscription`, `drafts.py` | The page can die at any moment after hiding; at most one 15 s chunk is lost. |
| 12 | Detect mic interruptions via track `mute`/`ended`; auto-pause; loud chime + 24 h toast; chime again on foreground. | `useStreamingMediaRecorder`, `useStreamingTranscription` | A call or Siri takes the mic silently; MediaRecorder otherwise records silence while the timer runs. App audio is often not routed during a call. |
| 13 | Every resume re-acquires the mic (new recorder, new init segment, server subsession). | `doResume` | A paused recorder's capture dies silently on iOS and can report the old track as live. |
| 14 | Lock-screen resume deferred to unlock; title says "play, then unlock". | `pendingResumeRef` | `getUserMedia` is refused while the page is hidden. |
| 15 | MP4/AAC fallback for WebKit; mime family locked per session; resume refuses another family. | `PREFERRED_MIMES`, server `mime_mismatch` | iOS Safari MediaRecorder has no WebM; the server cannot mix containers. |
| 16 | Toast for WebM/Opus unplayable on older iOS Safari (< 17.4). | `playbackErrorMessage` | Recordings made in desktop Chrome are WebM/Opus. |
| 17 | Server-side ffprobe durations; client duration probe with 3 s timeout; correction on `loadedmetadata`/`ended`. | `/audio-chunks`, `AudioContext` | MediaRecorder files lack reliable duration metadata. |
| 18 | 59-min chime uses an AudioContext created in the record gesture. | `StreamingMicButton` | iOS may refuse to start a fresh AudioContext while backgrounded. |
| 19 | iPadOS detection (`MacIntel` + touch points). | `useVoiceSession`, `useMediaSession` | iPadOS reports a Mac user agent. |
| 20 | `beforeunload` warning while recording; URL `?resume=&parent=` kept in sync; `/drafts/interrupted` recovery banner. | `useVoiceSession`, `VoicePage` | Tabs get killed; SPA navigation cannot be blocked with `BrowserRouter`. |
| 21 | Record buttons disabled offline; failed chunks retried on `online`. | `VoicePage`, `useStreamingTranscription` | Mobile connectivity drops mid-recording. |
| 22 | Mobile player as a floating card without a seek bar. | `GlobalAudioPlayer` | Narrow screens. |

---

## 8. Native iOS mapping

### 8.1 Piece-by-piece

| Web piece | Native equivalent |
|---|---|
| Silent keepalive (#1–3) | Not needed as a trick: an active `AVAudioSession` + running audio I/O with `UIBackgroundModes: audio` keeps the process alive. The gaps with no real audio are covered by an audible cue (§8.3). |
| `MediaRecorder` 15 s timeslice, fMP4 | `AVAudioEngine` input tap (or `AVCaptureSession` audio data output) → convert to a fixed PCM format with `AVAudioConverter` (e.g. 48 kHz mono) → `CMSampleBuffer` → `AVAssetWriter` created with `init(contentType: .mpeg4Movie)` (no URL), `outputFileTypeProfile = .mpeg4AppleHLS` or `.mpeg4CMAFCompliant`, one `AVAssetWriterInput(mediaType: .audio, outputSettings: [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 64000])`, `expectsMediaDataInRealTime = true`, and `AVAssetWriterDelegate.assetWriter(_:didOutputSegmentData:segmentType:segmentReport:)`. Either `preferredOutputSegmentInterval = 15 s` or `.indefinite` + `flushSegment()` every 15 s and on pause/interruption/stop (`flushSegment` mirrors `requestData()`). |
| Chunk 0 contract | Upload chunk 0 = the `.initialization` segment bytes **concatenated with the first `.separable` segment**. Chunks 1..N = each following separable segment as-is. Keep sample timestamps continuous across pauses (compute PTS from the sample count, not host time) so a pause does not need a new init segment; a fresh writer after a crash can send an `ftyp`-first chunk N>0 and the server will open a subsession. **Verify against staging before building on it**: that separable segments never start with `ftyp`, whether they start with `styp` (the server's box walker only needs `ftyp`/`moov` in chunk 0; ffmpeg should accept `styp`), and that `gpt-4o-transcribe` accepts the remuxed file. |
| Upload with retry + `sendBeacon` | Write each chunk's multipart body to a file in Application Support first; upload with a foreground `URLSession` (lowest latency), and on failure or app backgrounding also enqueue a background `URLSession` `uploadTask(with:fromFile:)` (survives suspension and termination; the server dedupes by index). Persist the pending-upload list so a relaunch can finish it. Upload chunk 0 before any other chunk; then any order. |
| Finalize | Only after every chunk is acknowledged (or given up; then send `total_chunks` = count actually stored, otherwise the server waits 10 min). Treat `400 "not in recording state"` on a retry as success. |
| Draft SSE / `/status` fallback | Poll `GET /api/drafts/streaming/<sid>/status` every ~0.5–1 s after finalize until `streaming_status == 'completed'` (take `llm_node_id`, `warning`, `content`) or `failed`. Avoid the draft SSE in voice mode: it deletes the draft at `all_complete`, so a missed event leaves `/status` answering 404. Never call `/status` while the session is still recording (it can complete it, #320). |
| llm-status polling 1.5 s | Same endpoint, or `llm-stream` SSE `done` event + one llm-status fetch for `content`, `tool_calls_meta`, `warnings`. |
| `EventSource` | `URLSession.bytes(for:)` (or a data-task delegate) parsing `event:`/`data:` lines; session cookie from `HTTPCookieStorage.shared` (all voice endpoints are `@login_required`, cookie session). Check the response `Content-Type`: the TTS stream answers JSON when the node's TTS is not pending/processing/completed. Reconnect with `?last_chunk=<highest index received>` (server supports it; the web does not use it). Treat no event for 45 s (3 missed heartbeats) as stalled. Handle `event: error` and `event: close` explicitly. |
| TTS attach rule | Attach `/api/sse/nodes/<id>/tts-stream` as soon as the reply node id is known and `tts_task_status` is `pending`/`processing` (the server chain sets `pending` at node creation, so this works with the flag on or off). If the stream answers JSON 400 (status still null: the legacy `/api/voice` path, and `/api/voice/from-node` placeholders, whose TTS status is only set when the LLM task starts), retry the attach when llm-status shows `tts_streaming: true`, or when the node is `completed` after a `POST /tts`. Also close the stream when llm-status reports `failed`: with the flag off, a failed reply leaves the TTS status at `pending` and the stream would only send heartbeats until its 30-min timeout. Also `POST /tts` once when the node completes if no `all_complete` has arrived (idempotent). On `all_complete` with `continuation_node_id`, attach the continuation's stream and append to the same queue. |
| `AudioContext` queue | See §8.4: an `AVQueuePlayer`-based queue with cumulative-time mapping, or `AVAudioEngine` + `AVAudioPlayerNode`. Dedupe by `(node_id, chunk_index)` and by URL. |
| Playback rate 1/1.25/1.5/2 | `AVPlayer.rate` with `AVPlayerItem.audioTimePitchAlgorithm = .timeDomain` (or `.spectral`); or `AVAudioUnitTimePitch.rate` on the engine path. `MPRemoteCommandCenter.changePlaybackRateCommand.supportedPlaybackRates = [1, 1.25, 1.5, 2]`. |
| Seek / skip ±10 s | Cumulative seek across items (rebuild the queue from the target item, `seek(to:)` inside it). `skipForwardCommand`/`skipBackwardCommand` with `preferredIntervals = [10]`, `changePlaybackPositionCommand`. |
| Media Session | `MPNowPlayingInfoCenter.default().nowPlayingInfo` (`MPMediaItemPropertyTitle`, `MPMediaItemPropertyArtist = "Loore"`, `MPMediaItemPropertyArtwork` = app icon, `MPMediaItemPropertyPlaybackDuration`, `MPNowPlayingInfoPropertyElapsedPlaybackTime`, `MPNowPlayingInfoPropertyPlaybackRate`, `MPNowPlayingInfoPropertyIsLiveStream = true` while recording/thinking, optionally `MPNowPlayingInfoPropertyChapterNumber/ChapterCount`). Remote commands per phase exactly as §6.5 (recording: play = resume, pause = pause, next track = stop and send; thinking: next track = cancel; playback: play/pause/toggle/skip/seek/rate). The app must use a non-mixable category (no `.mixWithOthers`) to be the Now Playing app. |
| Track `mute`/`ended` | `AVAudioSession.interruptionNotification` (`.began` → flush segment, mark interrupted, pause writer; `.ended` + `.shouldResume` → may resume), `routeChangeNotification` (`.oldDeviceUnavailable` → pause playback per HIG; during recording the engine's `AVAudioEngineConfigurationChange` fires → restart the engine and keep the converter's fixed output format), `mediaServicesWereResetNotification` (rebuild engine, writer, player). |
| Interruption alert | Chime on `.ended` or on foreground, plus a **local notification** (`UNUserNotificationCenter`, no backend needed) "Recording paused — tap to resume", which reaches the user when app audio cannot. |
| getUserMedia refused while hidden | Native can restart capture in the background only while its session is still active; after a phone call the session was interrupted. Try `setActive(true)` + engine start in the remote-command handler; if it fails (`AVAudioSession.ErrorCode.cannotStartRecording` / `cannotInterruptOthers`), fall back to the web's behaviour (resume at unlock). **Verify on device.** |
| Mic permission | `NSMicrophoneUsageDescription`; `AVAudioApplication.requestRecordPermission` (iOS 17+). |
| System alerts during recording | `setPrefersNoInterruptionsFromSystemAlerts(true)` (iOS 14.5+). |
| 59-min warning (#127/#243) | Local timer + chime + local notification, in voice mode too (the web lacks it there). |
| Download | `URLSession.download` of the same URLs (`/api/nodes/<id>/audio-download?format=mp3` for chunked recordings) + share sheet / `fileExporter`. |
| Timing marks | Same `POST /api/voice/timing`; marks `rec_stop`, `finalize_acked`, `llm_node_known`, `tts_attach`, `chunk_ready`, `playing` (no `autoplay_blocked`). |

### 8.2 Audio session configuration

- Info.plist: `UIBackgroundModes = [audio]`, `NSMicrophoneUsageDescription`.
- Voice turn: category `.playAndRecord`, mode `.default` or `.spokenAudio` (avoid `.voiceChat`: it applies voice processing and lowers playback volume), options `[.allowBluetooth (HFP mic, e.g. AirPods), .allowBluetoothA2DP, .defaultToSpeaker]`. Activate at the record tap (foreground). Do **not** deactivate until the turn's audio is finished (a non-mixable session cannot be re-activated from the background: `cannotInterruptOthers`).
- Bluetooth quality: while the category is `.playAndRecord` and the input is a Bluetooth HFP mic, output also goes over HFP (narrowband). Option: at Stop, switch the still-active session to `.playback`/`.spokenAudio` so the reply plays over A2DP, and back to `.playAndRecord` at the next record tap (foreground). Changing category on an active session triggers a route change and an engine configuration change. **Verify on device** that this works while locked; check whether the iOS 26 option for high-quality Bluetooth recording (`bluetoothHighQualityRecording`) is available and helps.
- Listen-aloud outside voice mode: `.playback`, mode `.spokenAudio`.
- After the turn ends: `setActive(false, options: .notifyOthersOnDeactivation)` so other apps' audio can resume.

### 8.3 The gap between Stop and the first TTS chunk (the reason for the native app)

**Problem.** A backgrounded or locked iOS app is suspended a few seconds after its audio I/O stops. The gap from Stop to the first chunk is typically 5–20 s (transcription 2–3 s, model thinking 6–14 s, first TTS chunk 2–5 s) and can be minutes (long recordings, tool rounds, slow OpenAI TTS under #382 with up to ~90 s per chunk). The same gap reappears mid-reply whenever the queue drains while the reply is still being generated, and between an interim node's audio and its continuation's. A suspended app loses its SSE/polling and, once the session was deactivated, cannot start playback on its own.

**Recommended design.** Never let audio I/O stop between the record tap and the end of the reply:
1. At Stop, start a **looping audible "thinking" cue** (soft, low-level, a few seconds long; e.g. `AVAudioPlayer` with `numberOfLoops = -1`, or an `AVAudioPlayerNode` loop) *before* stopping the recording engine. The session stays active and the app keeps running in the background under the `audio` mode, so the foreground `URLSession` requests, the status polling and the SSE keep working while the phone is locked.
2. Stop the cue when the first chunk starts playing. Start it again whenever the queue drains while TTS is still generating (the web's `waitingForChunks`), and during a chain's interim → continuation wait.
3. Update Now Playing to "Thinking…" (live stream, no progress bar) with *next track* = cancel, like the web.
4. The cue doubles as feedback for a user with the phone in a pocket: they hear that the reply is coming. Give it a volume setting; a user who turns it off should get a clear warning that replies may then need a tap when the phone is locked.
5. Safety nets for when the app was suspended or killed anyway (system memory pressure, the user force-quits): `beginBackgroundTask` around finalize and the first polls (about 30 s of extra time, not enough on its own); on the next foreground, reconcile from REST (`/status`, `llm-status`, `tts-status`, `tts-stream?last_chunk=`), like the web's #242 loop; the persisted upload queue finishes uploads through the background `URLSession`.

**Options and App Store review risk** (Guideline 2.5.4: background modes only for their intended purpose):

| Option | Keeps the app alive in the gap | Review risk | Notes |
|---|---|---|---|
| Audible thinking cue (recommended) | Yes | Low: real audio the user hears, part of a continuous audio conversation | Must stay audible; it is a product feature, not a hidden trick. |
| Silent audio / engine rendering zeros | Yes | Medium to high: "silent audio to stay awake" is a known rejection reason | Works technically; reviewers who notice will reject. Avoid, or use only for sub-second transitions. |
| Keep the mic running through the gap (discard samples) | Yes (as recording) | Medium: orange mic indicator while not recording; looks like covert listening | Also battery. Only defensible with a real feature (voice "stop"/barge-in). |
| `beginBackgroundTask` only | ~30 s | None | Not enough for long gaps; the session may still be deactivated/suspended afterwards. |
| Push notification when audio is ready | Wakes the user, not the audio | None for alert pushes; silent pushes are throttled | Needs backend work (APNs), which the "backend unchanged" goal excludes; a good later fallback ("Reply ready — tap to listen"). |
| CallKit / PushKit VoIP | Yes | High: rejected for non-VoIP use | Do not use. |
| Background `URLSession` for SSE | No | — | Background sessions cannot stream SSE; only useful for uploads/downloads. |
| Live Activity | No (display only) | None | Optional lock-screen status ("Thinking…", "Speaking 0:42"); updates need the app running or pushes. |

### 8.4 Playback engine choice

Two workable designs; either satisfies the locked-phone case if §8.3 is followed.

- **A. `AVQueuePlayer` + separate cue player (recommended for the first version).** One `ChunkQueuePlayer` type serves the voice turn and the global listen-aloud player (parity with the single `AudioContext`). Append `AVPlayerItem(url:)` per chunk (MP3 over HTTP, range requests supported by `/media`), keep a parallel array of durations (server `duration`, corrected by `item.duration` once loaded) for cumulative time, chapters, seek (rebuild the queue from item k), rate (`audioTimePitchAlgorithm`), and a time observer (`addPeriodicTimeObserver`, ~0.1–0.25 s) for UI and Now Playing. Small gaps between items are acceptable (the web has them too). The looping cue is an `AVAudioPlayer` on the same session.
- **B. `AVAudioEngine` + `AVAudioPlayerNode` + `AVAudioUnitTimePitch`.** Download each chunk to a file and `scheduleFile`; gapless; one engine can hold the cue and the TTS. More code for seek, rate, time and Now Playing, and the mic-indicator issue if the same engine also had the input node in use (use a separate engine for recording).

### 8.5 Recommended native turn state machine

| State | Audio running | Network | Exit |
|---|---|---|---|
| `idle` | none | — | record tap → `starting` |
| `starting` | session activated | `POST init` (402 → idle with message) | engine started → `recording` |
| `recording` | mic → writer | segment uploads (chunk 0 first) | pause → `paused`; interruption → `interrupted`; stop → `stopping` |
| `paused` (user) | engine may keep running (no append) to keep the session; PTS continuity kept | uploads continue | resume → `recording`; stop → `stopping` |
| `interrupted` (system) | none (OS holds the mic) | uploads continue if the app runs | `.ended` → resume if allowed, else wait for the user (local notification); stop → `stopping` |
| `stopping` | **cue starts first**, then mic engine stops | flush last segment, finish uploads, `POST finalize` | 202 → `transcribing` |
| `transcribing` | cue | poll `/status` ~0.5–1 s | `completed` + `llm_node_id` → `awaitingAudio`; `warning` / empty → `idle` + message; `failed` → `idle` + error |
| `awaitingAudio` | cue | TTS SSE on the reply node; llm-status (or llm-stream) for completion, failure, continuation | first `chunk_ready` → `playing`; node failed → `idle` + error; completed with no speakable text → `done` |
| `playing` | TTS queue | SSE continues | queue empty & generating → `draining`; queue empty & done → `done` |
| `draining` | cue | SSE | next chunk → `playing`; `all_complete` with continuation → attach next node, stay |
| `done` | none; deactivate session | — | Continue → `starting` (parent = last reply node); replay/seek allowed |
| any | — | — | cancel → stop audio, close streams, `idle` (server keeps generating; the next turn parents to the thread parent as in the web) |

Deep-link equivalent of `?resume=<llm_node_id>&parent=<id>`: open straight into `awaitingAudio` for that node (use `tts-stream`, which replays a completed node).

---

## 9. Backend protocol reference (what a native client must speak)

All under the session cookie. JSON unless noted.

Recording and transcription
- `POST /api/drafts/streaming/init` `{parent_id?, privacy_level?, ai_usage?, label: 'Voice'}` → `201 {session_id, draft_id, sse_url}`; `402` spend cap.
- `POST /api/drafts/streaming/<sid>/audio-chunk` multipart `chunk`, `chunk_index`, `mime_type` → `202`/`200`/`400`/`404` (§2.3).
- `POST /api/drafts/streaming/<sid>/finalize` `{total_chunks, label, parent_id?, model?}` → `202 {message, task_id, draft_id, total_chunks}`.
- `GET /api/drafts/streaming/<sid>/status` (§3).
- `GET /api/sse/drafts/<sid>/transcription-stream[?last_chunk=N]` (§3).
- `GET /api/drafts/interrupted` → `[{id, session_id, parent_id, label, content, chunk_count, has_stored_chunks, streaming_mime_type, created_at, updated_at, parent_deleted?, warning?}]`.
- `DELETE /api/drafts/streaming/<sid>/discard`; `POST /api/drafts/streaming/<sid>/transcribe-remaining`; `POST /api/drafts/streaming/<sid>/save-as-node` (dictation).

Reply
- `POST /api/voice` `{content, model?, parent_id?, session_id?, ai_usage?}` → `202 {parent_id, user_node_id, llm_node_id, task_id}` (fallback only).
- `POST /api/voice/from-node/<id>` `{model?}` → `{mode, llm_node_id, parent_id, fresh?}`.
- `GET /api/nodes/<id>/llm-status` (§4.3); `GET /api/sse/nodes/<id>/llm-stream` (§4.4).

TTS and audio
- `POST /api/nodes/<id>/tts`, `GET /api/nodes/<id>/tts-status`, `GET /api/sse/nodes/<id>/tts-stream[?last_chunk=N]`, `GET /api/nodes/<id>/tts-chapters` (§5.4, §6.1); profile and item equivalents.
- `GET /api/nodes/<id>/audio`, `GET /api/nodes/<id>/audio-chunks`, `GET /api/nodes/<id>/audio-download?format=original|mp3` (§6.1, §6.4).
- `GET /media/<path>` — send the session cookies (design §9.3); decrypts `.enc` on the fly per request (whole file, DEK cached per gunicorn worker); supports `Range` (206). Content types for encrypted files: `.mp3` audio/mpeg, `.webm` audio/webm, `.wav`, `.m4a` audio/mp4, `.ogg`, `.flac`; **`.mp4` is not in the map → `application/octet-stream`**.
- Timing: `GET /api/voice/timing/clock`, `POST /api/voice/timing`, `GET /api/voice/timing`.

Server infrastructure notes: gunicorn 4 gevent workers (SSE connections are cheap); nginx `/api/sse/` unbuffered with 2 h timeouts; `/api/` 60 s read timeout.

---

## 10. Risks, bugs found, open questions

1. **Keeping the app alive through the gap** depends on continuous audio. The audible cue is the defensible route; a silent keepalive risks rejection under 2.5.4. If the system kills the app anyway, the reply needs a tap after reconciliation, as on the web today.
2. **fMP4 from `AVAssetWriter` must match the server's parser** (chunk 0 = init + first media segment; later chunks without `ftyp`; `styp` tolerance; timestamps continuous). A mismatch fails immediately with `init_parse_failed` on chunk 0, or silently later (a batch of 20 needing an init segment). Prototype this first, against staging.
3. **Draft deleted by the draft SSE at `all_complete`**: a client that misses that event then gets `404` from `/status` forever (the web's fallback would loop). Native: poll `/status` instead of using the draft SSE in voice mode.
4. **Finalize waits up to 10 min** for chunks counted in `total_chunks` that never arrived; a retried finalize gets `400`.
5. **Cross-device kill (#320)**: `GET /status` completes a draft that is still recording; a web tab's `NodeForm` recovery (`GET /api/drafts?parent_id=…` → `transcribe-remaining` → `/status`) under the same parent (or a new top-level entry, for a fresh voice turn) can end a native recording in progress.
6. **Media requests**: send the session cookies with every `/media` request (design §9.3).
7. **Original recordings in `.mp4.enc` are served as `application/octet-stream`**; AVPlayer may refuse them. Use `AVURLAssetOverrideMIMETypeKey: "audio/mp4"` (iOS 17+) or download to a local `.mp4` file first. **WebM/Opus originals (desktop Chrome recordings) are not playable by AVFoundation**: use `/api/nodes/<id>/audio-download?format=mp3` (ffmpeg per request; nginx's 60 s `/api/` timeout may cut long recordings) or a bundled Opus/WebM decoder.
8. **TTS slowness (#382)** gives chunk gaps of up to ~90 s and occasional chunk failures (`event: error`, node TTS `failed`, rest of the reply unspoken). The client must keep the cue running through such gaps and handle the error event.
9. **Cancel does not stop server work** (LLM, TTS, cost) and the web's next-turn parenting after a cancel is inconsistent (§1.2). Decide whether native keeps parity.
10. **Bluetooth**: HFP (narrowband) playback while `.playAndRecord` with a BT mic; the category switch to `.playback` at Stop needs device testing while locked. The web documented BT headset crashes and A2DP/HFP switching problems.
11. **Background mic restart after an interruption** (lock-screen resume) is unverified natively; worst case equals the web ("resume at unlock").
12. **Voice mode has no long-recording warning** (#127/#243); add one natively.
13. **`llm-status` `cancelled`** is unhandled in the web voice client (stuck on "Thinking..."); handle it natively as a terminal state.
14. **Flag drift**: `STREAMING_VOICE_TTS` defaults to off in code but is on in prod and staging; the native attach rule in §8.1 works for both.
15. Minor: AVPlayer's range requests on encrypted media each decrypt the whole file and may trigger a KMS unwrap per gunicorn worker (DEK cache is per process); chunks are small, so acceptable, but long original recordings streamed by range are heavier.
