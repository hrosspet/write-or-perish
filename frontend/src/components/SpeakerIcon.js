import React, { useState, useRef, useCallback } from 'react';
import { useEffect } from 'react';
import { FaVolumeUp, FaSpinner } from 'react-icons/fa';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import { useAudio } from '../contexts/AudioContext';
import { useAsyncTaskPolling } from '../hooks/useAsyncTaskPolling';
import { useTTSStreamSSE } from '../hooks/useSSE';

/**
 * Extracts the first markdown header from content
 */
const extractMarkdownHeader = (content) => {
  if (!content) return null;
  const lines = content.split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith('#')) {
      // Remove all # characters and trim
      return trimmed.replace(/^#+\s*/, '').trim();
    }
  }
  return null;
};

/**
 * SpeakerIcon component fetches and plays audio for a node or profile.
 * Shows loading spinner, play/pause state.
 */
const SpeakerIcon = ({ nodeId, profileId, content, isPublic }) => {
  const { user } = useUser();
  const { loadAudio, loadAudioQueue, appendChunkToQueue, setGeneratingTTS, currentAudio, isPlaying } = useAudio();
  const [loading, setLoading] = useState(false);
  const [audioSrc, setAudioSrc] = useState(null);
  const [audioChunks, setAudioChunks] = useState(null); // For chunked playback
  const [audioChunkDurations, setAudioChunkDurations] = useState(null); // Server-provided durations
  const [ttsTaskActive, setTtsTaskActive] = useState(false);
  const [sseActive, setSseActive] = useState(false);
  const sseChunkCountRef = useRef(0);

  const isNode = nodeId != null;
  const id = isNode ? nodeId : profileId;
  const baseUrl = isNode ? `/nodes/${id}` : `/profile/${id}`;

  // Extract header from content if available
  const header = extractMarkdownHeader(content);
  const baseTitle = isNode ? `Node ${id}` : `Profile ${id}`;
  const fullTitle = header ? `${baseTitle}: ${header}` : baseTitle;

  // SSE streaming for TTS chunks - only for nodes
  const handleChunkReady = useCallback((data) => {
    const chunkUrl = data.audio_url.startsWith('http')
      ? data.audio_url
      : `${process.env.REACT_APP_BACKEND_URL}${data.audio_url}`;
    const chunkDuration = data.duration != null ? data.duration : null;

    sseChunkCountRef.current += 1;

    if (sseChunkCountRef.current === 1) {
      // First chunk: start playback immediately via loadAudioQueue
      setLoading(false);
      loadAudioQueue(
        [chunkUrl],
        { title: fullTitle, id, type: 'node' },
        chunkDuration != null ? [chunkDuration] : null
      );
    } else {
      // Subsequent chunks: append to the active queue
      appendChunkToQueue(chunkUrl, chunkDuration);
    }
  }, [fullTitle, id, loadAudioQueue, appendChunkToQueue]);

  const handleAllComplete = useCallback((data) => {
    setSseActive(false);
    setGeneratingTTS(false);
    sseChunkCountRef.current = 0;

    // Cache the final TTS URL for future replay
    if (data.tts_url) {
      const finalUrl = data.tts_url.startsWith('http')
        ? data.tts_url
        : `${process.env.REACT_APP_BACKEND_URL}${data.tts_url}`;
      setAudioSrc(finalUrl);
    }
  }, [setGeneratingTTS]);

  const { disconnect: disconnectSSE } = useTTSStreamSSE(isNode ? nodeId : null, {
    enabled: sseActive,
    onChunkReady: handleChunkReady,
    onAllComplete: handleAllComplete,
  });

  // TTS generation polling - fallback when SSE isn't used (profiles, or SSE failure)
  const {
    status: ttsStatus,
    data: ttsData,
    error: ttsError
  } = useAsyncTaskPolling(
    ttsTaskActive ? `${baseUrl}/tts-status` : null,
    { enabled: ttsTaskActive }
  );

  // Reset audio state when the node/profile changes
  useEffect(() => {
    setAudioSrc(null);
    setAudioChunks(null);
    setAudioChunkDurations(null);
    setLoading(false);
    setTtsTaskActive(false);
    setSseActive(false);
    sseChunkCountRef.current = 0;
  }, [nodeId, profileId]);

  // Clean up SSE on unmount
  useEffect(() => {
    return () => {
      if (sseActive) {
        disconnectSSE();
        setGeneratingTTS(false);
      }
    };
  }, [sseActive, disconnectSSE, setGeneratingTTS]);

  // Handle TTS completion (polling fallback)
  useEffect(() => {
    if (ttsStatus === 'completed' && ttsData) {
      const ttsUrl = isNode ? ttsData.node?.audio_tts_url : ttsData.profile?.audio_tts_url;
      if (ttsUrl) {
        // Build absolute URL
        const srcUrl = ttsUrl.startsWith('http')
          ? ttsUrl
          : `${process.env.REACT_APP_BACKEND_URL}${ttsUrl}`;
        setAudioSrc(srcUrl);
        // Add a small delay to ensure file is available on server before loading
        setTimeout(() => {
          loadAudio({ url: srcUrl, title: fullTitle, id, type: isNode ? 'node' : 'profile' });
        }, 500);
      }
      setTtsTaskActive(false);
      setLoading(false);
    } else if (ttsStatus === 'failed') {
      console.error('TTS generation failed:', ttsError);
      setTtsTaskActive(false);
      setLoading(false);
    }
  }, [ttsStatus, ttsData, ttsError, isNode, id, loadAudio, fullTitle]);

  // Show for voice-mode users, or for any authenticated user on public posts
  if (!user || (!user.voice_mode_enabled && !isPublic)) {
    return null;
  }

  const handleClick = async () => {
    if (loading || ttsTaskActive || sseActive) return;

    try {
      // If we already have audio chunks cached, play them
      if (audioChunks && audioChunks.length > 0) {
        await loadAudioQueue(audioChunks, { title: fullTitle, id, type: isNode ? 'node' : 'profile' }, audioChunkDurations);
        return;
      }

      // If we already have a single audio source, play it
      if (audioSrc) {
        await loadAudio({ url: audioSrc, title: fullTitle, id, type: isNode ? 'node' : 'profile' });
        return;
      }

      setLoading(true);

      // Attempt to fetch existing audio URLs
      let original_url = null;
      let tts_url = null;
      let has_audio_chunks = false;
      try {
        const res = await api.get(`${baseUrl}/audio`, {
          validateStatus: (status) => status === 200 || status === 404
        });
        if (res.status === 200) {
          original_url = res.data.original_url;
          tts_url = res.data.tts_url;
          has_audio_chunks = res.data.has_audio_chunks || false;
        }
        // If status is 404, both remain null (no audio exists yet)
      } catch (getErr) {
        // Handle any other errors
        console.error('Error checking for audio:', getErr);
      }

      // Prefer original recording (including audio chunks) over TTS
      // If audio chunks exist, try loading them before falling back to TTS
      if (has_audio_chunks && isNode) {
        try {
          const chunksRes = await api.get(`${baseUrl}/audio-chunks`, {
            validateStatus: (status) => status === 200 || status === 404
          });
          if (chunksRes.status === 200 && chunksRes.data.chunks?.length > 0) {
            const chunksData = chunksRes.data.chunks;
            const chunkUrls = chunksData.map(chunk => {
              const url = typeof chunk === 'string' ? chunk : chunk.url;
              return url.startsWith('http') ? url : `${process.env.REACT_APP_BACKEND_URL}${url}`;
            });
            const chunkDurations = chunksData.map(chunk =>
              typeof chunk === 'object' && chunk.duration != null ? chunk.duration : null
            );
            setAudioChunks(chunkUrls);
            setAudioChunkDurations(chunkDurations);
            setLoading(false);
            await loadAudioQueue(chunkUrls, { title: fullTitle, id, type: 'node' }, chunkDurations);
            return;
          }
        } catch (chunksErr) {
          console.error('Error loading audio chunks:', chunksErr);
        }
      }

      let urlPath = original_url || tts_url;

      if (!urlPath && isNode) {
        // No single audio file - check for audio chunks (streaming transcription nodes)
        try {
          const chunksRes = await api.get(`${baseUrl}/audio-chunks`, {
            validateStatus: (status) => status === 200 || status === 404
          });
          if (chunksRes.status === 200 && chunksRes.data.chunks?.length > 0) {
            // Backend returns [{url, duration}, ...] with accurate durations from ffprobe
            const chunksData = chunksRes.data.chunks;
            const chunkUrls = chunksData.map(chunk => {
              const url = typeof chunk === 'string' ? chunk : chunk.url;
              return url.startsWith('http') ? url : `${process.env.REACT_APP_BACKEND_URL}${url}`;
            });
            // Extract server-provided durations (accurate via ffprobe)
            const chunkDurations = chunksData.map(chunk =>
              typeof chunk === 'object' && chunk.duration != null ? chunk.duration : null
            );
            setAudioChunks(chunkUrls);
            setAudioChunkDurations(chunkDurations);
            setLoading(false);
            await loadAudioQueue(chunkUrls, { title: fullTitle, id, type: 'node' }, chunkDurations);
            return;
          }
        } catch (chunksErr) {
          console.error('Error checking for audio chunks:', chunksErr);
        }
      }

      if (!urlPath) {
        // No audio exists — only voice-mode users may generate TTS
        if (!user.voice_mode_enabled) {
          setLoading(false);
          return;
        }
        // Start async TTS generation
        await api.post(`${baseUrl}/tts`);

        if (isNode) {
          // Use SSE for streaming playback (nodes only)
          sseChunkCountRef.current = 0;
          setSseActive(true);
          setGeneratingTTS(true);
          // loading stays true until first chunk arrives
        } else {
          // Fall back to polling for profiles
          setTtsTaskActive(true);
        }
        return;
      }

      const srcUrl = urlPath.startsWith('http')
        ? urlPath
        : `${process.env.REACT_APP_BACKEND_URL}${urlPath}`;
      setAudioSrc(srcUrl);

      // Load audio into global player
      await loadAudio({ url: srcUrl, title: fullTitle, id, type: isNode ? 'node' : 'profile' });
      setLoading(false);
    } catch (err) {
      console.error('Error playing audio:', err);
      setLoading(false);
      setTtsTaskActive(false);
      setSseActive(false);
      setGeneratingTTS(false);
    }
  };

  // Check if this is the currently playing audio
  const isCurrentlyPlaying = currentAudio &&
    currentAudio.id === id &&
    currentAudio.type === (isNode ? 'node' : 'profile') &&
    isPlaying;

  const isActive = loading || ttsTaskActive || sseActive;

  return (
    <button onClick={handleClick} title={isActive ? 'Generating audio...' : 'Play audio'}
        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, marginLeft: '8px' }}>
      {isActive ? <FaSpinner className="spin" /> : <FaVolumeUp color={isCurrentlyPlaying ? '#61dafb' : 'inherit'} />}
    </button>
  );
};

export default SpeakerIcon;
