import React, { useState } from 'react';
import { FaDownload, FaSpinner } from 'react-icons/fa';
import api from '../api';
import { useUser } from '../contexts/UserContext';

const DownloadAudioIcon = ({ nodeId, isPublic }) => {
  const { user } = useUser();
  const [loading, setLoading] = useState(false);

  // Same visibility rules as SpeakerIcon
  if (!user || (!user.voice_mode_enabled && !isPublic)) {
    return null;
  }

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
    if (loading) return;
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

      // Try audio chunks (streaming recordings)
      if (has_audio_chunks) {
        try {
          const chunksRes = await api.get(`/nodes/${nodeId}/audio-chunks`);
          if (chunksRes.data.chunks?.length > 0) {
            const chunkUrls = chunksRes.data.chunks.map(c => {
              const url = typeof c === 'string' ? c : c.url;
              return url.startsWith('http') ? url : `${process.env.REACT_APP_BACKEND_URL}${url}`;
            });

            if (chunkUrls.length === 1) {
              // Single chunk - download directly
              const blob = await fetchAsBlob(chunkUrls[0]);
              downloadBlob(blob, `node-${nodeId}-recording.webm`);
            } else {
              // Multiple chunks - concatenate into single blob
              const blobs = await Promise.all(
                chunkUrls.map(url => fetch(url, { credentials: 'include' }).then(r => r.blob()))
              );
              const combined = new Blob(blobs, { type: 'audio/webm' });
              downloadBlob(combined, `node-${nodeId}-recording.webm`);
            }
            setLoading(false);
            return;
          }
        } catch (chunksErr) {
          console.error('Error fetching audio chunks for download:', chunksErr);
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
    <button onClick={handleDownload} title="Download audio"
      style={{ background: 'none', border: 'none', cursor: 'pointer', padding: 0, marginLeft: '4px' }}>
      {loading ? <FaSpinner className="spin" /> : <FaDownload />}
    </button>
  );
};

export default DownloadAudioIcon;
