import { useState, useRef, useCallback } from 'react';

/**
 * useMediaRecorder hook for recording audio via MediaRecorder API.
 * Returns status ('idle' | 'recording' | 'recorded'), mediaBlob, mediaUrl, duration (sec),
 * and control functions: startRecording, stopRecording, resetRecording.
 */
export function useMediaRecorder() {
  const [status, setStatus] = useState('idle');
  const [mediaBlob, setMediaBlob] = useState(null);
  const [mediaUrl, setMediaUrl] = useState('');
  const [duration, setDuration] = useState(0);
  const recorderRef = useRef(null);
  const startTimeRef = useRef(null);
  const chunksRef = useRef([]);

  const resetRecording = useCallback(() => {
    // Clean up previous recording
    if (mediaUrl) {
      URL.revokeObjectURL(mediaUrl);
    }
    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.stop();
    }
    recorderRef.current = null;
    chunksRef.current = [];
    setMediaBlob(null);
    setMediaUrl('');
    setDuration(0);
    setStatus('idle');
  }, [mediaUrl]);

  const startRecording = useCallback(async () => {
    resetRecording();
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('MediaDevices API not supported');
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const options = { mimeType: 'audio/webm' };
      const mediaRecorder = new MediaRecorder(stream, options);
      recorderRef.current = mediaRecorder;
      chunksRef.current = [];
      mediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) {
          chunksRef.current.push(e.data);
        }
      };
      mediaRecorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mediaRecorder.mimeType });
        const url = URL.createObjectURL(blob);
        setMediaBlob(blob);
        setMediaUrl(url);
        const ms = Date.now() - startTimeRef.current;
        setDuration(ms / 1000);
        setStatus('recorded');
        // Stop all tracks
        stream.getTracks().forEach((t) => t.stop());
      };
      startTimeRef.current = Date.now();
      mediaRecorder.start();
      setStatus('recording');
    } catch (err) {
      console.error('Error accessing microphone:', err);
      throw err;
    }
  }, [resetRecording]);

  const stopRecording = useCallback(() => {
    if (recorderRef.current && recorderRef.current.state === 'recording') {
      recorderRef.current.stop();
      setStatus('recorded');
    }
  }, []);

  return {
    status,
    mediaBlob,
    mediaUrl,
    duration,
    startRecording,
    stopRecording,
    resetRecording,
  };
}