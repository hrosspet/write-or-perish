# Voice Mode – Design & Implementation Plan

This document outlines the work required to add **Voice Mode** to the product.  In Voice Mode users can:  
1. Record a voice clip (up to **a couple of hours / 100 MB**).  
2. Replay the recording before submitting.  
3. Submit; the server **transcribes** it asynchronously using OpenAI `gpt‑4o‑transcribe` and stores the transcript as a normal text node.  
4. Play audio for any node using a **speaker icon** located next to the node‑action buttons.  • If an original recording exists it is streamed; otherwise a server‑side TTS clip (OpenAI `gpt‑4o‑mini‑tts`) is generated, cached, and played.

---

## 1  User‑Experience Changes

### 1.1  Recording Text (inside **Add Text / Write Text** modal)
The existing “Add Text / Write Text” modal is expanded to include voice recording controls.

• **Microphone button** placed inside the modal header next to the **Send**/submit control.  
• States: _idle_, _recording_ (pulse animation), _recorded_ (play / re‑record buttons).  
• While recording, the textarea is disabled (grayed) to prevent accidental typing.  
• After recording finishes the user sees:  
    – Waveform preview + duration.  
    – **Play**, **Re‑record**, **Send**.  
• If the user resumes typing, the current recording is discarded automatically.

### 1.2  Speaker Icon on Nodes
• Displayed **next to the node‑action buttons** (Add Text, LLM Response, etc.).  
• Hover tooltip: “Play audio”.  
• Click flow:  
    1. Front‑end requests `/nodes/:id/audio` to obtain URLs.  
    2. If neither original nor TTS exists, front‑end POSTs to `/nodes/:id/tts` to trigger generation (Admin‑only check happens server‑side), shows loading spinner, then auto‑plays when ready.  
• While playing, icon animates (equalizer bars) and can be paused/stopped.

---

## 2  Data‑Model & Storage

### 2.1  Database (PostgreSQL)
Add columns to `nodes` table (or create side‑table `node_audio`):
```
audio_original_url   TEXT   -- NULL unless user recorded
audio_tts_url        TEXT   -- NULL until TTS is generated
audio_duration_sec   REAL   -- pre‑filled by front‑end or ffprobe
audio_mime_type      TEXT   -- e.g. audio/webm; codecs=opus
```

### 2.2  Binary Storage
• **Local file storage** at `/data/audio/` (can be swapped for S3 later).  
• Folder structure: `user/{user_id}/node/{node_id}/{original|tts}.{ext}` where `{ext}` is `webm`, `wav`, or `m4a`.  
• Files served through `/media` proxy route with HTTP range support; no direct public directory listing.

### 2.3  Naming & Size Limits
• Accept `webm` (Opus), `wav`, and `m4a` up to **100 MB** each (≈ 2 hours @ 48 kbps Opus).  
• Enforce server‑side validation to reject larger or unsupported uploads.  
• `audio_duration_sec` is populated asynchronously (via `ffprobe`) after upload so the UI can display length.

---

## 3  Backend API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/nodes` | POST | Accept text **or** multipart form with `audio_file`. Returns `node_id` immediately; transcription processed async. |
| `/nodes/:id/audio` | GET | Returns JSON `{ original_url, tts_url }` if present or 404. |
| `/nodes/:id/tts` | POST | Trigger background TTS generation. Returns **202 Accepted**. |

Implementation Notes:
1. Use **Flask** `BackgroundTasks` / Celery / RQ worker to handle transcription & TTS so that the HTTP response is not blocked.  
2. After transcription completes:  
  • Update `nodes.content` with transcript.  
  • Re‑compute token counts.  
  • Persist `audio_original_url`.

### 3.1  Feature Gating
Voice Mode endpoints are **protected** – only the following user groups may access them:
1. **Admin** (current phase).  
2. **Paid users** (future).  

Implementation: add `User.is_admin` (already exists) and `User.plan = 'free' | 'pro'`.  Decorate the new endpoints with a `@voice_mode_required` wrapper that  
• returns **403** if the feature is disabled for the user.  
• The front‑end hides Mic & Speaker controls entirely when the flag is absent.

---

## 4  AI Pipelines

### 4.1  Speech‑to‑Text (OpenAI gpt‑4o‑transcribe)
1. Worker streams the uploaded audio (≤ 100 MB).  
2. Calls `/v1/audio/transcriptions` with model `gpt‑4o-transcribe`.  
3. Stores transcript & updates token counts.  
4. On error ➜ flag node for manual review.

### 4.2  Text‑to‑Speech (OpenAI gpt‑4o‑mini‑tts)
Triggered only when `audio_original_url` IS NULL and first playback is requested.
1. Fetch `nodes.content`.  
2. Chunk text to ≤ 2k chars.  
3. Call `/v1/audio/speech` with model `gpt‑4o-mini-tts`.  
4. Concatenate (ffmpeg) into single file, store `audio_tts_url`, return URL.

---

## 5  Front‑End Changes (React)

1. **Recording Hook** – wrapper around `MediaRecorder` for WebM/Opus & m4a.  
2. **AudioPlayer component** – shared between the recording modal and node playback.  
3. **MicButton component** embedded inside the **Add Text / Write Text modal** (hidden when `!voiceModeEnabled`).  
4. **SpeakerIcon component** rendered next to action buttons in each `NodeCard`.  
5. **State Management** – maintain recording blob until upload; optimistic UI while transcription pending.  
6. **Feature flag** – `voiceModeEnabled` supplied in bootstrap payload so UI can hide controls if the current user is not eligible.

---

## 6  Security & Privacy

• Request microphone permission only on user action.  
• Sanitize filenames, store MIME, run antivirus if required.  
• HTTPS mandatory for all audio endpoints.  
• Rate‑limit TTS generation (cache results, only first request triggers expensive call).  
• Update Terms of Service to cover voice data usage.

---

## 7  Roll‑out Plan

1. **Branch `voice-mode`**  ← _this document_.  
2. Phase 1 – Front‑end recording & local playback (no backend).  
3. Phase 2 – Backend upload & transcription → text nodes.  
4. Phase 3 – Speaker icon + original playback.  
5. Phase 4 – On‑demand TTS & caching.  
6. QA across mobile / desktop / Safari (no Opus?).  
7. Production deploy behind a feature flag `VOICE_MODE`.  
8. Gradually enable for 5%, then 100% of traffic.

---

## 8  Open Questions

1. Do we allow **audio‑only nodes** (no transcript in UI) while transcription is pending?  
2. Which storage provider is preferred in production?  
3. Localisation of TTS voice (multiple languages / genders)?  
4. Should we expose an **RSS/podcast** feed of nodes with audio?

---

Prepared on branch **voice‑mode** – v0.2
