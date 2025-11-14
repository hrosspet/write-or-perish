import React, { useState, useRef } from 'react';
import { useEffect } from 'react';
import { FaVolumeUp, FaSpinner } from 'react-icons/fa';
import api from '../api';
import { useUser } from '../contexts/UserContext';

/**
 * SpeakerIcon component fetches and plays audio for a node.
 * Shows loading spinner, play/pause state.
 */
const SpeakerIcon = ({ nodeId }) => {
  const { user } = useUser();
  const [loading, setLoading] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [audioSrc, setAudioSrc] = useState(null);
  const audioRef = useRef(null);

  // Reset audio state when the node changes
  useEffect(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current = null;
    }
    setAudioSrc(null);
    setPlaying(false);
    setLoading(false);
  }, [nodeId]);

  // Show only if voice mode enabled for current user
  if (!user || !user.voice_mode_enabled) {
    return null;
  }

  const handleClick = async () => {
    if (loading) return;
    setLoading(true);
    try {
      let srcUrl;
      if (!audioSrc) {
        // Attempt to fetch existing audio URLs
        let original_url = null;
        let tts_url = null;
        try {
          const res = await api.get(`/nodes/${nodeId}/audio`);
          original_url = res.data.original_url;
          tts_url = res.data.tts_url;
        } catch (getErr) {
          // 404 means no audio yet; other errors rethrow
          if (!(getErr.response && getErr.response.status === 404)) {
            throw getErr;
          }
        }
        // If no original or TTS, trigger TTS generation
        let urlPath = original_url || tts_url;
        if (!urlPath) {
          const ttsRes = await api.post(`/nodes/${nodeId}/tts`);
          urlPath = ttsRes.data.tts_url;
        }
        // Build absolute URL
        srcUrl = urlPath.startsWith('http')
          ? urlPath
          : `${process.env.REACT_APP_BACKEND_URL}${urlPath}`;
        setAudioSrc(srcUrl);
        // Create and play audio
        const audio = new Audio(srcUrl);
        audioRef.current = audio;
        audio.onended = () => setPlaying(false);
        audio.onpause = () => setPlaying(false);
        audio.onplay = () => setPlaying(true);
        await audio.play();
      } else {
        // Toggle playback on existing audio
        const audio = audioRef.current;
        if (playing) {
          audio.pause();
        } else {
          await audio.play();
        }
      }
    } catch (err) {
      console.error('Error playing audio:', err);
    } finally {
      setLoading(false);
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