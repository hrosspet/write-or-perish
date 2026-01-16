import React, { useState } from 'react';
import { useEffect } from 'react';
import { FaVolumeUp, FaSpinner } from 'react-icons/fa';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import { useAudio } from '../contexts/AudioContext';
import { useAsyncTaskPolling } from '../hooks/useAsyncTaskPolling';

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
const SpeakerIcon = ({ nodeId, profileId, content }) => {
  const { user } = useUser();
  const { loadAudio, currentAudio, isPlaying } = useAudio();
  const [loading, setLoading] = useState(false);
  const [audioSrc, setAudioSrc] = useState(null);
  const [ttsTaskActive, setTtsTaskActive] = useState(false);

  const isNode = nodeId != null;
  const id = isNode ? nodeId : profileId;
  const baseUrl = isNode ? `/nodes/${id}` : `/profile/${id}`;

  // Extract header from content if available
  const header = extractMarkdownHeader(content);
  const baseTitle = isNode ? `Node ${id}` : `Profile ${id}`;
  const fullTitle = header ? `${baseTitle}: ${header}` : baseTitle;

  // TTS generation polling - enabled automatically when ttsTaskActive is true
  const {
    status: ttsStatus,
    data: ttsData,
    error: ttsError
  } = useAsyncTaskPolling(
    ttsTaskActive ? `${baseUrl}/tts-status` : null,
    { enabled: ttsTaskActive }  // Auto-start when ttsTaskActive is true
  );

  // Reset audio state when the node/profile changes
  useEffect(() => {
    setAudioSrc(null);
    setLoading(false);
    setTtsTaskActive(false);
  }, [nodeId, profileId]);

  // Handle TTS completion
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

  // Show only if voice mode enabled for current user
  if (!user || !user.voice_mode_enabled) {
    return null;
  }

  const handleClick = async () => {
    if (loading || ttsTaskActive) return;

    try {
      if (!audioSrc) {
        setLoading(true);
        // Attempt to fetch existing audio URLs
        let original_url = null;
        let tts_url = null;
        try {
          const res = await api.get(`${baseUrl}/audio`, {
            validateStatus: (status) => status === 200 || status === 404
          });
          if (res.status === 200) {
            original_url = res.data.original_url;
            tts_url = res.data.tts_url;
          }
          // If status is 404, both remain null (no audio exists yet)
        } catch (getErr) {
          // Handle any other errors
          console.error('Error checking for audio:', getErr);
        }

        // Prefer original recording over TTS
        let urlPath = original_url || tts_url;
        if (!urlPath) {
          // No audio exists - start async TTS generation
          await api.post(`${baseUrl}/tts`);
          setTtsTaskActive(true);
          return;
        }

        const srcUrl = urlPath.startsWith('http')
          ? urlPath
          : `${process.env.REACT_APP_BACKEND_URL}${urlPath}`;
        setAudioSrc(srcUrl);

        // Load audio into global player
        await loadAudio({ url: srcUrl, title: fullTitle, id, type: isNode ? 'node' : 'profile' });
        setLoading(false);
      } else {
        // Audio already loaded - trigger play in global player
        await loadAudio({ url: audioSrc, title: fullTitle, id, type: isNode ? 'node' : 'profile' });
      }
    } catch (err) {
      console.error('Error playing audio:', err);
      setLoading(false);
      setTtsTaskActive(false);
    }
  };

  // Check if this is the currently playing audio
  const isCurrentlyPlaying = currentAudio &&
    currentAudio.id === id &&
    currentAudio.type === (isNode ? 'node' : 'profile') &&
    isPlaying;

  return (
    <button onClick={handleClick} title={loading ? 'Loading audio...' : 'Play audio'}
        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, marginLeft: '8px' }}>
      {loading ? <FaSpinner className="spin" /> : <FaVolumeUp color={isCurrentlyPlaying ? '#61dafb' : 'inherit'} />}
    </button>
  );
};

export default SpeakerIcon;