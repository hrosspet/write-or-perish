import React, { forwardRef } from 'react';

/**
 * AudioPlayer component
 * Props:
 *  - src: URL of audio resource
 *  - autoPlay: boolean to auto-play
 *  - controls: boolean to show default controls
 *  - onEnded: callback when playback ends
 *  - ...props: passed to <audio>
 */
const AudioPlayer = forwardRef(({ src, autoPlay = false, controls = true, onEnded, style, ...props }, ref) => {
  return (
    <audio
      ref={ref}
      src={src}
      controls={controls}
      autoPlay={autoPlay}
      onEnded={onEnded}
      style={style}
      {...props}
    />
  );
});

export default AudioPlayer;