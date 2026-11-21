import React, { useState } from 'react';
import { FaDownload, FaSpinner } from 'react-icons/fa';
import api from '../api';
import { useUser } from '../contexts/UserContext';
import { useToast } from '../contexts/ToastContext';

export const DOWNLOAD_FAILED_TEXT = 'The audio could not be downloaded.';

const DownloadAudioIcon = ({ nodeId, isPublic, aiUsage, ownRecording }) => {
  const { user } = useUser();
  const { addToast } = useToast();
  const [loading, setLoading] = useState(false);

  // Same visibility rules as SpeakerIcon
  if (!user || (!user.voice_mode_enabled && !isPublic && !ownRecording)) {
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

  // The file's bytes; a non-2xx answer is thrown, never saved as the file.
  // The thrown error carries the server's words and code when its body is
  // the usual JSON error.
  const fetchAsBlob = async (url) => {
    const fullUrl = url.startsWith('http')
      ? url
      : `${process.env.REACT_APP_BACKEND_URL}${url}`;
    const response = await fetch(fullUrl, { credentials: 'include' });
    if (!response.ok) {
      let body = null;
      try {
        body = await response.json();
      } catch (e) {
        // Not JSON (an HTML error page).
      }
      const err = new Error(`Download failed (${response.status})`);
      err.status = response.status;
      err.serverMessage = (body && (body.error || body.message)) || null;
      err.code = (body && body.code) || null;
      throw err;
    }
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
      let mergeError = null;
      if (has_audio_chunks) {
        try {
          const blob = await fetchAsBlob(`/api/nodes/${nodeId}/audio-download?format=mp3`);
          downloadBlob(blob, `node-${nodeId}-recording.mp3`);
          setLoading(false);
          return;
        } catch (chunksErr) {
          console.error('Error downloading merged audio:', chunksErr);
          // A refusal with its own words (download_busy): say it, rather
          // than save the spoken version instead of the recording.
          if (chunksErr.code && chunksErr.serverMessage) {
            addToast(chunksErr.serverMessage, 6000);
            setLoading(false);
            return;
          }
          mergeError = chunksErr;
        }
      }

      // Fall back to TTS audio
      if (tts_url) {
        const blob = await fetchAsBlob(tts_url);
        downloadBlob(blob, `node-${nodeId}-tts.mp3`);
        setLoading(false);
        return;
      }

      // No audio available (say so when the merge failed)
      if (mergeError) {
        addToast(mergeError.serverMessage || DOWNLOAD_FAILED_TEXT, 6000);
      }
      setLoading(false);
    } catch (err) {
      console.error('Error downloading audio:', err);
      addToast(err.serverMessage || DOWNLOAD_FAILED_TEXT, 6000);
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
