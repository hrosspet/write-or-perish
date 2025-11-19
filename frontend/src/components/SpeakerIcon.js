import React, { useState, useRef } from 'react';
import { useEffect } from 'react';
import { FaVolumeUp, FaSpinner } from 'react-icons/fa';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import { useAsyncTaskPolling } from '../hooks/useAsyncTaskPolling';

/**
 * SpeakerIcon component fetches and plays audio for a node or profile.
 * Shows loading spinner, play/pause state.
 */
const SpeakerIcon = ({ nodeId, profileId }) => {
  const { user } = useUser();
  const [loading, setLoading] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [audioSrc, setAudioSrc] = useState(null);
  const [ttsTaskActive, setTtsTaskActive] = useState(false);
  const audioRef = useRef(null);

  const isNode = nodeId != null;
  const id = isNode ? nodeId : profileId;
  const baseUrl = isNode ? `/nodes/${id}` : `/profile/${id}`;

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
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    setAudioSrc(null);
    setPlaying(false);
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
        // Create and play audio
        const audio = new Audio(srcUrl);
        audioRef.current = audio;
        audio.onended = () => setPlaying(false);
        audio.onpause = () => setPlaying(false);
        audio.onplay = () => setPlaying(true);
        audio.play().catch(err => console.error('Error playing audio:', err));
      }
      setTtsTaskActive(false);
      setLoading(false);
    } else if (ttsStatus === 'failed') {
      console.error('TTS generation failed:', ttsError);
      setTtsTaskActive(false);
      setLoading(false);
    }
  }, [ttsStatus, ttsData, ttsError, isNode]);

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
        let tts_url = null;
        try {
          const res = await api.get(`${baseUrl}/audio`);
          tts_url = res.data.tts_url;
        } catch (getErr) {
          // 404 means no audio yet; other errors rethrow
          if (!(getErr.response && getErr.response.status === 404)) {
            throw getErr;
          }
        }
        
        let urlPath = tts_url;
        if (!urlPath) {
          // Start async TTS generation
          await api.post(`${baseUrl}/tts`);
          setTtsTaskActive(true);
          return;
        }

        const srcUrl = urlPath.startsWith('http')
          ? urlPath
          : `${process.env.REACT_APP_BACKEND_URL}${urlPath}`;
        setAudioSrc(srcUrl);
        
        const audio = new Audio(srcUrl);
        audioRef.current = audio;
        audio.onended = () => setPlaying(false);
        audio.onpause = () => setPlaying(false);
        audio.onplay = () => setPlaying(true);
        await audio.play();
        setLoading(false);
      } else {
        const audio = audioRef.current;
        if (playing) {
          audio.pause();
        } else {
          await audio.play();
        }
      }
    } catch (err) {
      console.error('Error playing audio:', err);
      setLoading(false);
      setTtsTaskActive(false);
    }
  };

  return (
    <button onClick={handleClick} title={loading ? 'Loading audio...' : 'Play audio'}
        style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, marginLeft: '8px' }}>
      {loading ? <FaSpinner className="spin" /> : <FaVolumeUp color={playing ? '#61dafb' : 'inherit'} />}
    </button>
  );
};

export default SpeakerIcon;