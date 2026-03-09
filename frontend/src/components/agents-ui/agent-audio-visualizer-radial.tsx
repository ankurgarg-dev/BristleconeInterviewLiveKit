import { type ComponentProps, type CSSProperties, useMemo } from 'react';
import { type LocalAudioTrack, type RemoteAudioTrack } from 'livekit-client';
import {
  type AgentState,
  type TrackReferenceOrPlaceholder,
  useMultibandTrackVolume,
} from '@livekit/components-react';
import { useAgentAudioVisualizerRadialAnimator } from '../../hooks/agents-ui/use-agent-audio-visualizer-radial';

export interface AgentAudioVisualizerRadialProps {
  state?: AgentState;
  color?: `#${string}`;
  radius?: number;
  barCount?: number;
  audioTrack?: LocalAudioTrack | RemoteAudioTrack | TrackReferenceOrPlaceholder;
  className?: string;
}

export function AgentAudioVisualizerRadial({
  state = 'connecting',
  color,
  radius = 40,
  barCount = 16,
  audioTrack,
  className,
  style,
  ...props
}: AgentAudioVisualizerRadialProps & ComponentProps<'div'>) {
  const volumeBands = useMultibandTrackVolume(audioTrack, {
    bands: barCount,
    loPass: 100,
    hiPass: 200,
  });

  const sequencerInterval = useMemo(() => {
    switch (state) {
      case 'connecting':
      case 'listening':
        return 500;
      case 'initializing':
        return 250;
      case 'thinking':
        return Infinity;
      default:
        return 1000;
    }
  }, [state]);

  const highlightedIndices = useAgentAudioVisualizerRadialAnimator(state, barCount, sequencerInterval);
  const bands = useMemo(
    () => (audioTrack ? volumeBands : new Array(barCount).fill(0)),
    [audioTrack, volumeBands, barCount],
  );

  const dotSize = useMemo(() => (radius * Math.PI) / barCount, [radius, barCount]);
  const barThickness = useMemo(() => {
    // Keep bars visually separated at low counts (e.g. 8).
    return Math.max(2, Math.min(dotSize * 0.38, 10));
  }, [dotSize]);
  const minBarHeight = useMemo(() => Math.max(2, Math.min(radius * 0.12, 8)), [radius]);
  const maxBarHeight = useMemo(
    () => Math.max(minBarHeight + 12, radius * 1.55),
    [minBarHeight, radius],
  );

  return (
    <div
      data-lk-state={state}
      className={className}
      style={{ ...style, color } as CSSProperties}
      {...props}
    >
      {bands.map((band, idx) => {
        const angle = (idx / barCount) * Math.PI * 2;
        const highlighted = highlightedIndices.includes(idx);
        const isSpeaking = state === 'speaking';
        const speakingHeight = minBarHeight + Math.pow(Math.max(0, band), 0.8) * (maxBarHeight - minBarHeight);
        const barHeight = isSpeaking ? speakingHeight : minBarHeight;
        const opacity = highlighted ? 1 : state === 'speaking' ? 0.34 : 0.2;

        return (
          <div
            key={`${barCount}-${idx}`}
            style={{
              position: 'absolute',
              top: '50%',
              left: '50%',
              width: 1,
              height: 1,
              transformOrigin: 'center',
              transform: `rotate(${angle}rad) translateY(${radius}px)`,
            }}
          >
            <div
              data-lk-index={idx}
              data-lk-highlighted={highlighted}
              style={{
                width: `${barThickness}px`,
                minHeight: `${minBarHeight}px`,
                height: `${barHeight}px`,
                borderRadius: '999px',
                background: 'currentColor',
                opacity,
                transform: 'translate(-50%, 0)',
                transformOrigin: 'top center',
                transition: 'height 120ms linear, opacity 150ms linear',
              }}
            />
          </div>
        );
      })}
    </div>
  );
}
