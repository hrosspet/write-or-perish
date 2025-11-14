import React, { useState, useRef } from 'react';
import { useUser } from '../contexts/UserContext';
import { FaMicrophone, FaStop, FaPlay, FaRedo } from 'react-icons/fa';
import AudioPlayer from './AudioPlayer';

/**
 * MicButton component renders audio recording controls: record, stop, playback, re-record.
 * Props:
 *  - status: 'idle' | 'recording' | 'recorded'
 *  - mediaUrl: URL of recorded audio
 *  - duration: recording duration in seconds
 *  - startRecording: function to start
 *  - stopRecording: function to stop
 *  - resetRecording: function to reset
 */
const MicButton = ({ status, mediaUrl, duration, startRecording, stopRecording, resetRecording }) => {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef(null);

  const handlePlayPause = () => {
    if (!audioRef.current) return;
    if (playing) {
      audioRef.current.pause();
    } else {
      audioRef.current.play();
    }
  };

  const onAudioPlay = () => setPlaying(true);
  const onAudioPause = () => setPlaying(false);
  const onAudioEnded = () => setPlaying(false);

  const { user } = useUser();
  // Show only if voice mode enabled for current user
  if (!user || !user.voice_mode_enabled) {
    return null;
  }
  switch (status) {
    case 'idle':
      return <button type="button" onClick={startRecording} title="Record audio"><FaMicrophone /></button>;
    case 'recording':
      return <button type="button" onClick={stopRecording} title="Stop recording"><FaStop color="red" /></button>;
    case 'recorded':
      return (
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <button type="button" onClick={handlePlayPause} title={playing ? 'Pause playback' : 'Play recording'}>
            {playing ? <FaStop /> : <FaPlay />}
          </button>
          <span>{duration.toFixed(1)}s</span>
          <button type="button" onClick={resetRecording} title="Re-record"><FaRedo /></button>
          {/* Audio element for playback */}
          <AudioPlayer
            ref={audioRef}
            src={mediaUrl}
            controls={false}
            autoPlay={false}
            onPlay={onAudioPlay}
            onPause={onAudioPause}
            onEnded={onAudioEnded}
            style={{ display: 'none' }}
          />
        </div>
      );
    default:
      return null;
  }
};

export default MicButton;