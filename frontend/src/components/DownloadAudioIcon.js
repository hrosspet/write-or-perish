import React, { useState } from 'react';
import { FaDownload, FaSpinner } from 'react-icons/fa';
import api from '../api';
import { useUser } from '../contexts/UserContext';

const DownloadAudioIcon = ({ nodeId, isPublic, aiUsage }) => {
  const { user } = useUser();
  const [loading, setLoading] = useState(false);

  // Same visibility rules as SpeakerIcon
  if (!user || (!user.voice_mode_enabled && !isPublic)) {
    return null;
  }

  const noAiAccess = aiUsage === 'none';

  const downloadBlob = (blob, filename) => {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const fetchAsBlob = async (url) => {
    const fullUrl = url.startsWith('http')
      ? url
      : `${process.env.REACT_APP_BACKEND_URL}${url}`;
    const response = await fetch(fullUrl, { credentials: 'include' });
    return response.blob();
  };

  const handleDownload = async () => {
    if (noAiAccess || loading) return;
    setLoading(true);

    try {
      const res = await api.get(`/nodes/${nodeId}/audio`, {
        validateStatus: (status) => status === 200 || status === 404
      });

      if (res.status === 404) {
        setLoading(false);
        return;
      }

      const { original_url, tts_url, has_audio_chunks } = res.data;

      // Prefer original recording over TTS
      if (original_url) {
        const blob = await fetchAsBlob(original_url);
        const ext = original_url.split('.').pop().replace(/\.enc$/, '') || 'webm';
        downloadBlob(blob, `node-${nodeId}-recording.${ext}`);
        setLoading(false);
        return;
      }

      // Try audio chunks (streaming recordings) — server merges with ffmpeg
      if (has_audio_chunks) {
        try {
          const blob = await fetchAsBlob(`/api/nodes/${nodeId}/audio-download?format=mp3`);
          downloadBlob(blob, `node-${nodeId}-recording.mp3`);
          setLoading(false);
          return;
        } catch (chunksErr) {
          console.error('Error downloading merged audio:', chunksErr);
        }
      }

      // Fall back to TTS audio
      if (tts_url) {
        const blob = await fetchAsBlob(tts_url);
        downloadBlob(blob, `node-${nodeId}-tts.mp3`);
        setLoading(false);
        return;
      }

      // No audio available
      setLoading(false);
    } catch (err) {
      console.error('Error downloading audio:', err);
      setLoading(false);
    }
  };

  return (
    <button onClick={handleDownload} title={noAiAccess ? 'Download disabled — No AI access' : 'Download audio'}
      disabled={noAiAccess}
      style={{ background: 'none', border: 'none', cursor: noAiAccess ? 'not-allowed' : 'pointer', padding: 0, marginLeft: '4px', opacity: noAiAccess ? 0.35 : 1 }}>
      {loading ? <FaSpinner className="spin" /> : <FaDownload />}
    </button>
  );
};

export default DownloadAudioIcon;
