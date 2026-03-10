import '@livekit/components-styles';
import {
  ConnectionStateToast,
  ControlBar,
  GridLayout,
  LiveKitRoom,
  ParticipantTile,
  PreJoin,
  RoomAudioRenderer,
  VideoTrack,
  VideoConference,
  isTrackReference,
  useParticipantContext,
  useParticipants,
  useRoomContext,
  useTrackRefContext,
  useTracks,
  useVoiceAssistant,
} from '@livekit/components-react';
import { RoomEvent, Track } from 'livekit-client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiClient } from './api';
import { AgentAudioVisualizerAura } from './components/agents-ui/agent-audio-visualizer-aura';
import { AgentAudioVisualizerRadial } from './components/agents-ui/agent-audio-visualizer-radial';
import { AgentAudioVisualizerWave } from './components/agents-ui/agent-audio-visualizer-wave';
import { ApplicationsPage } from './components/ApplicationsPage';
import { CandidatesPage } from './components/CandidatesPage';
import { InterviewsPage } from './components/InterviewsPage';
import { PositionsPage } from './components/PositionsPage';
import authBackground from './assets/Background.png';
import authCam from './assets/Cam.png';
import authLogo from './assets/logo.png';
import './styles.css';

let globalDirectPc = null;
let globalDirectAudio = null;
let globalDirectMic = null;

function closeGlobalDirectConnection() {
  if (globalDirectPc) {
    globalDirectPc.getSenders().forEach((sender) => sender.track?.stop());
    globalDirectPc.close();
    globalDirectPc = null;
  }
  if (globalDirectMic) {
    globalDirectMic.getTracks().forEach((track) => track.stop());
    globalDirectMic = null;
  }
  if (globalDirectAudio) {
    globalDirectAudio.pause();
    globalDirectAudio.srcObject = null;
    globalDirectAudio = null;
  }
}

function LoginCard({ onLoggedIn }) {
  const [username, setUsername] = useState('demo');
  const [password, setPassword] = useState('demo-pass');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      await apiClient.login(username, password);
      onLoggedIn(username);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-page" style={{ backgroundImage: `url(${authBackground})` }}>
      <header className="auth-brand">
        <img src={authLogo} alt="Bristlecone" />
      </header>
      <div className="auth-hero-circle" aria-hidden="true">
        <img src={authCam} alt="" />
      </div>
      <section className="auth-form-shell">
        <div className="auth-card">
          <form onSubmit={submit}>
            <label htmlFor="username">Username</label>
            <input id="username" value={username} onChange={(e) => setUsername(e.target.value)} />
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <button disabled={loading}>{loading ? 'Signing in...' : 'Sign in'}</button>
          </form>
          {error && <p className="error">{error}</p>}
        </div>
      </section>
    </div>
  );
}

function ParticipantsSidebar() {
  const participants = useParticipants();
  const rows = useMemo(
    () =>
      participants.map((p) => ({
        identity: p.identity,
        name: p.name || p.identity,
        isLocal: p.isLocal,
        isAi:
          p.identity.startsWith('agent-') ||
          p.identity.includes('assistant') ||
          p.identity.includes('interviewer') ||
          p.identity.includes('support') ||
          p.identity.includes('realtime') ||
          p.identity.includes('observer'),
      })),
    [participants],
  );

  return (
    <aside className="sidebar">
      <h3>Participants ({rows.length})</h3>
      <ul>
        {rows.map((row) => (
          <li key={row.identity}>
            <span>{row.name}</span>
            {row.isLocal && <small>you</small>}
            {row.isAi && <small className="ai-pill">AI</small>}
          </li>
        ))}
      </ul>
    </aside>
  );
}

function ObserverWaveVisualizer({ audioElement, micStream, activity, micEnabled }) {
  const canvasRef = useRef(null);
  const activityRef = useRef(activity);

  useEffect(() => {
    activityRef.current = activity;
  }, [activity]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const context = canvas.getContext('2d');
    if (!context) return undefined;

    let width = 1;
    let height = 1;
    let rafId = 0;
    let audioContext = null;
    let remoteAnalyser = null;
    let remoteSourceNode = null;
    let localAnalyser = null;
    let localSourceNode = null;
    let phase = 0;
    let remoteStreamRef = null;
    let localStreamRef = null;
    let resizeObserver = null;
    let smoothedAmplitude = 0.03;

    const resizeCanvas = () => {
      const rect = canvas.getBoundingClientRect();
      const nextWidth = Math.max(1, Math.floor(rect.width));
      const nextHeight = Math.max(1, Math.floor(rect.height));
      if (canvas.width !== nextWidth || canvas.height !== nextHeight) {
        canvas.width = nextWidth;
        canvas.height = nextHeight;
      }
      width = nextWidth;
      height = nextHeight;
    };

    const getLevel = (analyser, buffer) => {
      if (!analyser) return 0;
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        const sample = (buffer[i] - 128) / 128;
        sum += sample * sample;
      }
      return Math.sqrt(sum / buffer.length);
    };

    const ensureAudioContext = () => {
      if (audioContext) return true;
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      audioContext = new Ctx();
      audioContext.resume().catch(() => {});
      return true;
    };

    const ensureRemotePipeline = () => {
      if (!audioElement || !audioElement.srcObject) return;
      const remoteStream = audioElement.srcObject;
      if (!ensureAudioContext() || remoteStreamRef === remoteStream) return;
      remoteStreamRef = remoteStream;
      remoteSourceNode?.disconnect();
      remoteAnalyser?.disconnect();
      remoteSourceNode = audioContext.createMediaStreamSource(remoteStream);
      remoteAnalyser = audioContext.createAnalyser();
      remoteAnalyser.fftSize = 1024;
      remoteSourceNode.connect(remoteAnalyser);
    };

    const ensureLocalPipeline = () => {
      if (!micStream) return;
      if (!ensureAudioContext() || localStreamRef === micStream) return;
      localStreamRef = micStream;
      localSourceNode?.disconnect();
      localAnalyser?.disconnect();
      localSourceNode = audioContext.createMediaStreamSource(micStream);
      localAnalyser = audioContext.createAnalyser();
      localAnalyser.fftSize = 1024;
      localSourceNode.connect(localAnalyser);
    };

    const baseHue = 189; // #21a6bd
    const hueShift = 2;

    const drawWave = (amplitude, t) => {
      context.clearRect(0, 0, width, height);
      context.beginPath();
      for (let x = 0; x < width; x += 1) {
        const progress = x / width;
        const y =
          height * 0.5 +
          Math.sin(progress * 12 + t) * (5 + amplitude * 120) +
          Math.sin(progress * 30 + t * 1.2) * (2.2 + amplitude * 44);
        if (x === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
      const animatedHue = baseHue + Math.sin(t * 0.8) * hueShift;
      context.lineWidth = 2.6;
      context.strokeStyle = `hsla(${animatedHue}, 70%, 46%, 0.95)`;
      context.stroke();

      // subtle second pass to make hue shift visible without changing layout
      context.beginPath();
      for (let x = 0; x < width; x += 1) {
        const progress = x / width;
        const y =
          height * 0.5 +
          Math.sin(progress * 12 + t + 0.25) * (4 + amplitude * 90) +
          Math.sin(progress * 30 + t * 1.12) * (1.8 + amplitude * 30);
        if (x === 0) context.moveTo(x, y);
        else context.lineTo(x, y);
      }
      context.lineWidth = 1.4;
      context.strokeStyle = 'rgba(75, 201, 132, 0.55)';
      context.stroke();
    };

    const remoteBuffer = new Uint8Array(512);
    const localBuffer = new Uint8Array(512);
    const render = () => {
      resizeCanvas();
      ensureRemotePipeline();
      ensureLocalPipeline();
      const remoteLevel = getLevel(remoteAnalyser, remoteBuffer);
      const localLevel = getLevel(localAnalyser, localBuffer);
      const status = activityRef.current || '';
      let statusAmplitude = 0.03;
      if (status === 'Assistant speaking') statusAmplitude = 0.24;
      else if (status === 'You are speaking') statusAmplitude = 0.2;
      else if (status === 'Assistant listening') statusAmplitude = 0.05;
      else if (status === 'Connecting OpenAI realtime...') statusAmplitude = 0.02;

      let audioAmplitude = remoteLevel * 24 + localLevel * 18;
      if (!micEnabled && status !== 'Assistant speaking') {
        statusAmplitude = Math.min(statusAmplitude, 0.008);
        audioAmplitude *= 0.2;
      }

      const targetAmplitude = Math.min(0.36, Math.max(0.004, statusAmplitude, audioAmplitude));
      smoothedAmplitude = smoothedAmplitude * 0.82 + targetAmplitude * 0.18;
      phase += 0.006 + smoothedAmplitude * 0.05;
      drawWave(smoothedAmplitude, phase);
      rafId = window.requestAnimationFrame(render);
    };
    render();

    resizeObserver = new ResizeObserver(() => resizeCanvas());
    resizeObserver.observe(canvas);

    return () => {
      window.cancelAnimationFrame(rafId);
      resizeObserver?.disconnect();
      if (remoteSourceNode) remoteSourceNode.disconnect();
      if (remoteAnalyser) remoteAnalyser.disconnect();
      if (localSourceNode) localSourceNode.disconnect();
      if (localAnalyser) localAnalyser.disconnect();
      if (audioContext) audioContext.close().catch(() => {});
    };
  }, [audioElement, micEnabled, micStream]);

  return <canvas ref={canvasRef} className="observer-wave" />;
}

function mapObserverStatusToAuraState(status) {
  if (!status) return 'connecting';
  const normalized = status.toLowerCase();
  if (normalized.includes('connecting')) return 'connecting';
  if (normalized.includes('disconnected')) return 'disconnected';
  if (normalized.includes('assistant speaking')) return 'speaking';
  if (normalized.includes('you are speaking')) return 'listening';
  if (normalized.includes('listening') || normalized.includes('ready') || normalized.includes('connected')) {
    return 'listening';
  }
  return 'thinking';
}

function AgentWaveTileContent({
  targetAgent,
  status,
  audioElement,
  micStream,
  micEnabled,
  assistantSpeaking,
}) {
  const participant = useParticipantContext();
  const trackRef = useTrackRefContext();
  const participants = useParticipants();
  const { agent, state: voiceState, audioTrack: voiceAudioTrack } = useVoiceAssistant();
  const normalizedIdentity = (participant.identity || '').toLowerCase();
  const isRemoteAgent = participant.identity?.startsWith('agent-') && !participant.isLocal;
  const explicitMatch = normalizedIdentity.includes(targetAgent);
  const allowGenericAgentMatch =
    targetAgent === 'observer' || targetAgent === 'realtime' || targetAgent === 'assistant';
  const isVoiceAssistantAgent = participant.identity === agent?.identity;
  const isTargetAgent =
    isVoiceAssistantAgent || explicitMatch || (isRemoteAgent && allowGenericAgentMatch);
  const localParticipant = participants.find((p) => p.isLocal);
  const userSpeaking = localParticipant?.isSpeaking ?? false;
  const observerAssistantSpeaking = assistantSpeaking || status === 'Assistant speaking';
  const localMicTrack = localParticipant?.getTrackPublication?.(Track.Source.Microphone)?.track;

  let derivedStatus = status || '';
  if (targetAgent === 'realtime' || targetAgent === 'assistant') {
    if (!micEnabled) derivedStatus = 'Muted';
    else if (participant.isSpeaking) derivedStatus = 'Assistant speaking';
    else if (userSpeaking) derivedStatus = 'You are speaking';
    else derivedStatus = 'Assistant listening';
  }

  if (isTargetAgent) {
    if (targetAgent === 'realtime') {
      return (
        <div className="observer-orb-tile">
          <AgentAudioVisualizerWave
            state={voiceState}
            audioTrack={voiceAudioTrack}
            color="#21a6bd"
            colorShift={2}
            className="realtime-wave"
          />
          <div className="standard-participant-meta">{participant.name || participant.identity}</div>
        </div>
      );
    }
    if (targetAgent === 'interviewer') {
      let interviewerState = voiceState || 'connecting';
      // Explicit silence behavior requested: if no one speaks, show connecting behavior.
      if (participant.isSpeaking) interviewerState = 'speaking';
      else if (userSpeaking) interviewerState = 'listening';
      else interviewerState = 'connecting';

      return (
        <div className="observer-orb-tile">
          <div className="interviewer-radial-shell">
            <AgentAudioVisualizerRadial
              state={interviewerState}
              audioTrack={voiceAudioTrack}
              color="#4BC984"
              barCount={8}
              radius={50}
              className="interviewer-radial"
            />
          </div>
          <div className="standard-participant-meta">{participant.name || participant.identity}</div>
        </div>
      );
    }
    if (targetAgent === 'observer') {
      let observerAuraState = mapObserverStatusToAuraState(status);
      // Observer speaking detection is sourced from direct remote audio activity.
      if (observerAuraState !== 'connecting' && observerAuraState !== 'disconnected') {
        if (observerAssistantSpeaking) observerAuraState = 'speaking';
        else if (userSpeaking) observerAuraState = 'listening';
        else observerAuraState = 'thinking';
      }
      return (
        <div className={`observer-orb-tile observer-state-${observerAuraState}`}>
          <div className="observer-aura-shell">
            <AgentAudioVisualizerAura
              state={observerAuraState}
              audioTrack={voiceAudioTrack}
              color="#21A6BD"
              colorShift={2}
              themeMode="dark"
              className="observer-aura"
            />
          </div>
          <div className="standard-participant-meta">{participant.name || participant.identity}</div>
        </div>
      );
    }
    if (targetAgent === 'assistant') {
      const assistantWaveState = participant.isSpeaking ? 'speaking' : userSpeaking ? 'listening' : 'thinking';
      const assistantWaveColor = assistantWaveState === 'listening' ? '#4BC984' : '#21A6BD';
      const assistantWaveAudioTrack =
        assistantWaveState === 'listening' ? localMicTrack || voiceAudioTrack : voiceAudioTrack;
      return (
        <div className={`observer-orb-tile assistant-state-${assistantWaveState}`}>
          <div className="assistant-wave-shell">
            <AgentAudioVisualizerWave
              state={assistantWaveState}
              audioTrack={assistantWaveAudioTrack}
              color={assistantWaveColor}
              colorShift={assistantWaveState === 'listening' ? 0.2 : 0.06}
              lineWidth={assistantWaveState === 'listening' ? 3 : 2.2}
              blur={assistantWaveState === 'listening' ? 0.5 : 0.7}
              className="assistant-wave"
            />
          </div>
          <div className="standard-participant-meta">{participant.name || participant.identity}</div>
        </div>
      );
    }
    return (
      <div className="observer-orb-tile">
        <ObserverWaveVisualizer
          audioElement={audioElement}
          micStream={micStream}
          activity={derivedStatus}
          micEnabled={micEnabled}
        />
        <div className="standard-participant-meta">{participant.name || participant.identity}</div>
      </div>
    );
  }

  const hasVideo =
    isTrackReference(trackRef) &&
    (trackRef.publication.kind === 'video' ||
      trackRef.source === Track.Source.Camera ||
      trackRef.source === Track.Source.ScreenShare);

  return (
    <div className="standard-participant-tile">
      {hasVideo ? <VideoTrack trackRef={trackRef} /> : <div className="tile-fallback" />}
      <div className="standard-participant-meta">{participant.name || participant.identity}</div>
    </div>
  );
}

function AgentWaveConference({
  targetAgent,
  status,
  audioElement,
  micStream,
  micEnabled,
  assistantSpeaking,
}) {
  const participants = useParticipants();
  const tracks = useTracks(
    [
      { source: Track.Source.Camera, withPlaceholder: true },
      { source: Track.Source.ScreenShare, withPlaceholder: false },
    ],
    { updateOnlyOn: [RoomEvent.ActiveSpeakersChanged], onlySubscribed: false },
  );
  const hasObserverAgentTile = participants.some((p) => {
    if (p.isLocal) return false;
    const id = (p.identity || '').toLowerCase();
    return id.startsWith('agent-') || id.includes('observer');
  });
  const showObserverFallbackTile = targetAgent === 'observer' && !hasObserverAgentTile;
  const userSpeaking = participants.some((p) => p.isLocal && p.isSpeaking);
  const observerAssistantSpeaking = assistantSpeaking || status === 'Assistant speaking';
  const observerFallbackState = (() => {
    const mapped = mapObserverStatusToAuraState(status);
    if (mapped === 'connecting' || mapped === 'disconnected') return mapped;
    if (observerAssistantSpeaking) return 'speaking';
    if (userSpeaking) return 'listening';
    // Idle visual when neither user nor assistant is actively speaking.
    return 'thinking';
  })();
  const localTrackRef = tracks.find((t) => isTrackReference(t) && t.participant?.isLocal) || null;
  const localParticipant = participants.find((p) => p.isLocal) || null;

  return (
    <div className="lk-video-conference">
      <div className="lk-video-conference-inner">
        {showObserverFallbackTile ? (
          <div className="observer-dual-layout">
            {localTrackRef ? (
              <ParticipantTile trackRef={localTrackRef} className="observer-local-participant-tile">
                <AgentWaveTileContent
                  targetAgent={targetAgent}
                  status={status}
                  audioElement={audioElement}
                  micStream={micStream}
                  micEnabled={micEnabled}
                  assistantSpeaking={assistantSpeaking}
                />
              </ParticipantTile>
            ) : (
              <div className="standard-participant-tile">
                <div className="tile-fallback" />
                <div className="standard-participant-meta">
                  {localParticipant?.name || localParticipant?.identity || 'You'}
                </div>
              </div>
            )}
            <div className={`observer-orb-tile observer-state-${observerFallbackState}`}>
              <div className="observer-aura-shell">
                <AgentAudioVisualizerAura
                  state={observerFallbackState}
                  color="#21A6BD"
                  colorShift={2}
                  themeMode="dark"
                  className="observer-aura"
                />
              </div>
              <div className="standard-participant-meta">Observer</div>
            </div>
          </div>
        ) : (
          <div className="lk-grid-layout-wrapper">
            <GridLayout tracks={tracks}>
              <ParticipantTile>
                <AgentWaveTileContent
                  targetAgent={targetAgent}
                  status={status}
                  audioElement={audioElement}
                  micStream={micStream}
                  micEnabled={micEnabled}
                  assistantSpeaking={assistantSpeaking}
                />
              </ParticipantTile>
            </GridLayout>
          </div>
        )}
        <ControlBar controls={{ chat: false, settings: false }} />
      </div>
      <RoomAudioRenderer />
      <ConnectionStateToast />
    </div>
  );
}

function ObserverAgentAudioBlocker({ enabled }) {
  const room = useRoomContext();

  useEffect(() => {
    if (!enabled) return;

    const muteAgentAudio = (participant) => {
      if (!participant?.identity?.startsWith('agent-')) return;
      participant.trackPublications.forEach((pub) => {
        if (pub.kind === Track.Kind.Audio && pub.isSubscribed) {
          pub.setSubscribed(false);
        }
      });
    };

    const muteAll = () => {
      room.remoteParticipants.forEach((participant) => muteAgentAudio(participant));
    };

    const onParticipantConnected = (participant) => muteAgentAudio(participant);
    const onTrackPublished = (publication, participant) => {
      if (participant?.identity?.startsWith('agent-') && publication.kind === Track.Kind.Audio) {
        publication.setSubscribed(false);
      }
    };
    const onTrackSubscribed = (track, publication, participant) => {
      if (participant?.identity?.startsWith('agent-') && publication.kind === Track.Kind.Audio) {
        publication.setSubscribed(false);
      }
    };

    muteAll();
    room.on(RoomEvent.ParticipantConnected, onParticipantConnected);
    room.on(RoomEvent.TrackPublished, onTrackPublished);
    room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);

    return () => {
      room.off(RoomEvent.ParticipantConnected, onParticipantConnected);
      room.off(RoomEvent.TrackPublished, onTrackPublished);
      room.off(RoomEvent.TrackSubscribed, onTrackSubscribed);
    };
  }, [enabled, room]);

  return null;
}

function ObserverMicRelayGate({ enabled, directMicRef, onMicEnabledChange }) {
  const room = useRoomContext();

  useEffect(() => {
    if (!enabled) return;

    const applyMicState = () => {
      const stream = directMicRef.current;
      const micEnabled = room.localParticipant?.isMicrophoneEnabled ?? true;
      onMicEnabledChange?.(micEnabled);
      if (!stream) return;
      stream.getAudioTracks().forEach((track) => {
        track.enabled = micEnabled;
      });
    };

    const onLocalTrackMuted = (publication) => {
      if (publication?.source === Track.Source.Microphone) applyMicState();
    };
    const onLocalTrackUnmuted = (publication) => {
      if (publication?.source === Track.Source.Microphone) applyMicState();
    };

    applyMicState();
    const timer = window.setInterval(applyMicState, 200);
    room.on(RoomEvent.LocalTrackMuted, onLocalTrackMuted);
    room.on(RoomEvent.LocalTrackUnmuted, onLocalTrackUnmuted);

    return () => {
      window.clearInterval(timer);
      room.off(RoomEvent.LocalTrackMuted, onLocalTrackMuted);
      room.off(RoomEvent.LocalTrackUnmuted, onLocalTrackUnmuted);
    };
  }, [directMicRef, enabled, onMicEnabledChange, room]);

  return null;
}

function MeetingView({ tokenInfo, onLeave, userName }) {
  const [error, setError] = useState('');
  const [directStatus, setDirectStatus] = useState('');
  const [directError, setDirectError] = useState('');
  const [directAudioElement, setDirectAudioElement] = useState(null);
  const [directMicStream, setDirectMicStream] = useState(null);
  const [directMicEnabled, setDirectMicEnabled] = useState(true);
  const [assistantSpeaking, setAssistantSpeaking] = useState(false);
  const directPcRef = useRef(null);
  const directAudioRef = useRef(null);
  const directMicRef = useRef(null);
  const directConnectInFlightRef = useRef(false);
  const directConnectionKeyRef = useRef('');
  const directStartTimerRef = useRef(null);
  const transcriptSeenRef = useRef(new Set());
  const observerAssistantBuffersRef = useRef(new Map());
  const observerSeenUserItemsRef = useRef(new Set());
  const observerSeenAssistantItemsRef = useRef(new Set());
  const useWaveAgentUi =
    tokenInfo.selectedAgent === 'observer' ||
    tokenInfo.selectedAgent === 'realtime' ||
    tokenInfo.selectedAgent === 'assistant' ||
    tokenInfo.selectedAgent === 'interviewer';
  const useDirectObserver = tokenInfo.selectedAgent === 'observer' && tokenInfo.aiEnabled;
  const livekitAudioEnabled = tokenInfo.audioEnabled;

  const appendTranscriptLine = useCallback(async ({ room, speaker, text, source, uniqueKey }) => {
    const trimmed = (text || '').trim();
    if (!trimmed || !room) return;
    const key = uniqueKey || `${source || 'livekit'}:${speaker || 'Unknown'}:${trimmed}`;
    if (transcriptSeenRef.current.has(key)) return;
    transcriptSeenRef.current.add(key);
    try {
      await apiClient.appendTranscript({
        room,
        speaker: speaker || 'Unknown',
        text: trimmed,
        source: source || 'livekit',
        unique_key: key,
      });
    } catch (_) {}
  }, []);

  useEffect(() => {
    let cancelled = false;
    const connectionKey = `${tokenInfo.room || ''}:${tokenInfo.identity || ''}:${tokenInfo.selectedAgent || ''}`;

    if (useDirectObserver) {
      if (directConnectInFlightRef.current) return;
      if (directConnectionKeyRef.current === connectionKey && directPcRef.current) return;
      directConnectInFlightRef.current = true;
    }

    async function connectDirectRealtime() {
      if (!useDirectObserver) return;
      setDirectError('');
      setDirectStatus('Connecting OpenAI realtime...');

      try {
        const { client_secret } = await apiClient.createOpenAIRealtimeToken({
          instructions: tokenInfo.instructions || null,
        });
        if (cancelled) return;

        const pc = new RTCPeerConnection();
        directPcRef.current = pc;
        closeGlobalDirectConnection();
        globalDirectPc = pc;

        const audioEl = new Audio();
        audioEl.autoplay = true;
        directAudioRef.current = audioEl;
        setDirectAudioElement(audioEl);
        globalDirectAudio = audioEl;

        pc.ontrack = (event) => {
          audioEl.srcObject = event.streams[0];
          audioEl.play().catch(() => {});
        };

        const dc = pc.createDataChannel('oai-events');
        dc.onopen = () => setDirectStatus('OpenAI realtime connected');
        dc.onclose = () => setDirectStatus('OpenAI realtime disconnected');
        dc.onerror = () => setDirectError('OpenAI realtime data channel error');
        dc.onmessage = (event) => {
          try {
            const payload = JSON.parse(event.data);
            const eventType = payload?.type || '';
            const isAssistantAudioEvent =
              eventType.startsWith('response.audio.') ||
              eventType.startsWith('response.output_audio.');
            if (isAssistantAudioEvent) {
              setDirectStatus('Assistant speaking');
            } else if (eventType === 'input_audio_buffer.speech_started') {
              setDirectStatus('You are speaking');
            } else if (eventType === 'input_audio_buffer.speech_stopped') {
              setDirectStatus('Assistant listening');
            } else if (
              eventType === 'response.done' ||
              eventType === 'response.completed' ||
              eventType === 'output_audio_buffer.stopped'
            ) {
              setDirectStatus('Assistant listening');
            }

            const itemKey =
              payload?.item_id || payload?.output_item_id || payload?.response_id || payload?.id || '';
            if (
              eventType === 'response.audio_transcript.delta' ||
              eventType === 'response.output_audio_transcript.delta'
            ) {
              const next = `${observerAssistantBuffersRef.current.get(itemKey) || ''}${payload?.delta || ''}`;
              observerAssistantBuffersRef.current.set(itemKey, next);
            }
            if (eventType === 'response.output_audio_transcript.done') {
              if (itemKey && observerSeenAssistantItemsRef.current.has(itemKey)) {
                return;
              }
              const finalText = (payload?.transcript || observerAssistantBuffersRef.current.get(itemKey) || '').trim();
              if (finalText) {
                if (itemKey) observerSeenAssistantItemsRef.current.add(itemKey);
                appendTranscriptLine({
                  room: tokenInfo.room,
                  speaker: 'AI Assistant',
                  text: finalText,
                  source: 'observer-assistant',
                  uniqueKey: `observer:assistant:${itemKey || finalText}`,
                });
              }
              observerAssistantBuffersRef.current.delete(itemKey);
            }
            if (eventType === 'conversation.item.input_audio_transcription.completed') {
              if (itemKey && observerSeenUserItemsRef.current.has(itemKey)) {
                return;
              }
              const userText = (payload?.transcript || '').trim();
              if (userText) {
                if (itemKey) observerSeenUserItemsRef.current.add(itemKey);
                appendTranscriptLine({
                  room: tokenInfo.room,
                  speaker: userName || 'User',
                  text: userText,
                  source: 'observer-user',
                  uniqueKey: `observer:user:${itemKey || userText}`,
                });
              }
            }
          } catch (_) {}
        };

        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        directMicRef.current = stream;
        setDirectMicStream(stream);
        globalDirectMic = stream;
        stream.getTracks().forEach((track) => pc.addTrack(track, stream));

        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);

        const model = tokenInfo.realtimeModel || 'gpt-realtime-mini';
        const response = await fetch(
          `https://api.openai.com/v1/realtime/calls?model=${encodeURIComponent(model)}`,
          {
            method: 'POST',
            headers: {
              Authorization: `Bearer ${client_secret}`,
              'Content-Type': 'application/sdp',
            },
            body: offer.sdp,
          },
        );
        if (!response.ok) {
          const text = await response.text();
          throw new Error(`OpenAI realtime SDP failed: ${response.status} ${text.slice(0, 240)}`);
        }

        const answerSdp = await response.text();
        if (cancelled) return;
        await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp });
        directConnectionKeyRef.current = connectionKey;
        setDirectStatus('OpenAI realtime ready');
      } catch (err) {
        setDirectError(err.message || 'Failed to connect OpenAI realtime');
        setDirectStatus('');
      } finally {
        directConnectInFlightRef.current = false;
      }
    }

    directStartTimerRef.current = window.setTimeout(() => {
      connectDirectRealtime();
    }, 300);

    return () => {
      cancelled = true;
      if (directStartTimerRef.current) {
        window.clearTimeout(directStartTimerRef.current);
      }
      directStartTimerRef.current = null;
      const pc = directPcRef.current;
      if (pc) {
        pc.getSenders().forEach((sender) => sender.track?.stop());
        pc.close();
      }
      directPcRef.current = null;

      const stream = directMicRef.current;
      if (stream) {
        stream.getTracks().forEach((track) => track.stop());
      }
      directMicRef.current = null;
      setDirectMicStream(null);

      const audioEl = directAudioRef.current;
      if (audioEl) {
        audioEl.pause();
        audioEl.srcObject = null;
      }
      directAudioRef.current = null;
      setDirectAudioElement(null);
      closeGlobalDirectConnection();
      directConnectInFlightRef.current = false;
      directConnectionKeyRef.current = '';
      setDirectStatus('');
      setDirectError('');
      observerAssistantBuffersRef.current.clear();
      observerSeenUserItemsRef.current.clear();
      observerSeenAssistantItemsRef.current.clear();
    };
  }, [appendTranscriptLine, tokenInfo.instructions, tokenInfo.realtimeModel, tokenInfo.room, useDirectObserver, userName]);

  useEffect(() => {
    if (!useDirectObserver || !directAudioElement) {
      setAssistantSpeaking(false);
      return;
    }

    let cancelled = false;
    let audioContext = null;
    let sourceNode = null;
    let analyser = null;
    let rafId = 0;
    let streamRef = null;
    const buffer = new Uint8Array(512);
    let activeUntil = 0;
    let lastSpeaking = false;

    const ensurePipeline = () => {
      const stream = directAudioElement.srcObject;
      if (!stream || streamRef === stream) return;
      streamRef = stream;

      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audioContext) {
        audioContext = new Ctx();
        audioContext.resume().catch(() => {});
      }

      sourceNode?.disconnect();
      analyser?.disconnect();
      sourceNode = audioContext.createMediaStreamSource(stream);
      analyser = audioContext.createAnalyser();
      analyser.fftSize = 1024;
      analyser.smoothingTimeConstant = 0.6;
      sourceNode.connect(analyser);
    };

    const rms = () => {
      if (!analyser) return 0;
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        const sample = (buffer[i] - 128) / 128;
        sum += sample * sample;
      }
      return Math.sqrt(sum / buffer.length);
    };

    const loop = () => {
      if (cancelled) return;
      ensurePipeline();
      const level = rms();
      const now = performance.now();
      if (level > 0.02) activeUntil = now + 220;
      const speakingNow = now < activeUntil;
      if (speakingNow !== lastSpeaking) {
        lastSpeaking = speakingNow;
        setAssistantSpeaking(speakingNow);
      }
      rafId = window.requestAnimationFrame(loop);
    };
    loop();

    return () => {
      cancelled = true;
      window.cancelAnimationFrame(rafId);
      sourceNode?.disconnect();
      analyser?.disconnect();
      audioContext?.close().catch(() => {});
    };
  }, [directAudioElement, useDirectObserver]);

  return (
    <div className="meeting-layout">
      <LiveKitRoom
        token={tokenInfo.token}
        serverUrl={tokenInfo.server_url}
        connect={true}
        audio={livekitAudioEnabled}
        video={tokenInfo.videoEnabled}
        onDisconnected={() => onLeave()}
        onError={(err) => {
          setError(err.message);
          apiClient.clientEvent('join_failure', err.message).catch(() => {});
        }}
        onMediaDeviceFailure={(failure, kind) => {
          const detail = `${kind || 'unknown'}:${failure || 'failure'}`;
          apiClient.clientEvent('media_permission_failure', detail).catch(() => {});
        }}
      >
        <ObserverAgentAudioBlocker enabled={useDirectObserver} />
        <ObserverMicRelayGate
          enabled={useDirectObserver}
          directMicRef={directMicRef}
          onMicEnabledChange={setDirectMicEnabled}
        />
        <div className="meeting-shell">
          <div className="meeting-main">
            {error && <div className="banner error">{error}</div>}
            {useDirectObserver && directError && <div className="banner error">{directError}</div>}
            {useWaveAgentUi ? (
              <AgentWaveConference
                targetAgent={tokenInfo.selectedAgent}
                status={directStatus}
                audioElement={directAudioElement}
                micStream={directMicStream}
                micEnabled={directMicEnabled}
                assistantSpeaking={assistantSpeaking}
              />
            ) : (
              <VideoConference />
            )}
          </div>
          <ParticipantsSidebar />
        </div>
      </LiveKitRoom>
    </div>
  );
}

function JoinCard({ user, onJoin, defaultRoom = 'demo-room' }) {
  const [room, setRoom] = useState(defaultRoom || 'demo-room');
  const [aiEnabled, setAiEnabled] = useState(true);
  const [agent, setAgent] = useState('assistant');
  const [instructions, setInstructions] = useState('');
  const [pendingChoices, setPendingChoices] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [transcriptAvailable, setTranscriptAvailable] = useState(false);
  const [downloadLoading, setDownloadLoading] = useState(false);
  const joinInFlightRef = useRef(false);
  const fallbackChoices = useMemo(() => ({ audioEnabled: true, videoEnabled: true }), []);
  const observerSuffix = '-observer';

  const effectiveRoom = useMemo(() => {
    const base = room.trim();
    if (!base) return '';
    if (agent !== 'observer') return base;
    return base.endsWith(observerSuffix) ? base : `${base}${observerSuffix}`;
  }, [agent, room]);

  useEffect(() => {
    setRoom(defaultRoom || 'demo-room');
  }, [defaultRoom]);

  useEffect(() => {
    const roomName = effectiveRoom;
    if (!roomName) {
      setTranscriptAvailable(false);
      return undefined;
    }

    let cancelled = false;
    const refreshStatus = async () => {
      try {
        const status = await apiClient.transcriptStatus(roomName);
        if (!cancelled) {
          setTranscriptAvailable(Boolean(status?.exists));
        }
      } catch (_) {
        if (!cancelled) {
          setTranscriptAvailable(false);
        }
      }
    };

    const timer = window.setTimeout(refreshStatus, 250);
    const poll = window.setInterval(refreshStatus, 3000);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.clearInterval(poll);
    };
  }, [effectiveRoom]);

  async function startMeeting(choices) {
    if (joinInFlightRef.current) return;
    joinInFlightRef.current = true;
    const resolvedChoices = choices || pendingChoices || fallbackChoices;
    setLoading(true);
    setError('');
    try {
      const tokenInfo = await apiClient.issueToken({
        room: effectiveRoom,
        display_name: user,
        ai_enabled: aiEnabled,
        agent,
        instructions: instructions || null,
        capabilities: {
          can_publish: true,
          can_subscribe: true,
          can_publish_data: true,
          can_publish_sources: ['microphone', 'camera', 'screen_share'],
        },
      });
      onJoin({
        ...tokenInfo,
        selectedAgent: agent,
        aiEnabled,
        instructions: tokenInfo.instructions || instructions || '',
        realtimeModel: 'gpt-realtime-mini',
        audioEnabled: resolvedChoices.audioEnabled,
        videoEnabled: resolvedChoices.videoEnabled,
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
      joinInFlightRef.current = false;
    }
  }

  async function downloadTranscript() {
    const roomName = effectiveRoom;
    if (!roomName) return;
    setDownloadLoading(true);
    setError('');
    try {
      const { blob, filename } = await apiClient.downloadTranscript(roomName);
      const url = window.URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename || `${roomName}-transcript.txt`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    } finally {
      setDownloadLoading(false);
    }
  }

  return (
    <div className="join-page">
      <div className="join-grid">
        <section className="join-config">
          <label htmlFor="room">Room</label>
          <input id="room" value={room} onChange={(e) => setRoom(e.target.value)} />
          {agent === 'observer' && effectiveRoom && (
            <p className="helper-note">Observer room: {effectiveRoom}</p>
          )}

          <label htmlFor="agent">AI agent</label>
          <select id="agent" value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="assistant">assistant</option>
            <option value="support">support</option>
            <option value="interviewer">interviewer</option>
            <option value="realtime">realtime</option>
            <option value="observer">observer</option>
          </select>

          <label className="toggle-row" htmlFor="ai-enabled">
            <input
              id="ai-enabled"
              type="checkbox"
              checked={aiEnabled}
              onChange={(e) => setAiEnabled(e.target.checked)}
            />
            Enable AI participant in room
          </label>

          <label htmlFor="instructions">AI instructions (optional)</label>
          <textarea
            id="instructions"
            rows={4}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            placeholder="Answer in concise points"
          />

          <button
            disabled={loading}
            onClick={() => startMeeting(pendingChoices || fallbackChoices)}
          >
            {loading ? 'Joining...' : 'Join meeting'}
          </button>
          <button
            type="button"
            disabled={!transcriptAvailable || downloadLoading}
            onClick={downloadTranscript}
          >
            {downloadLoading ? 'Downloading...' : 'Download transcript'}
          </button>
          {error && <p className="error">{error}</p>}
        </section>

        <section className="join-preview">
          <PreJoin
            defaults={{
              username: user,
              audioEnabled: true,
              videoEnabled: true,
            }}
            onSubmit={(choices) => {
              setPendingChoices(choices);
              startMeeting(choices);
            }}
            onError={(err) => setError(err.message)}
            persistUserChoices={true}
          />
        </section>
      </div>
    </div>
  );
}

function PlaceholderPage({ title, description }) {
  return (
    <section className="portal-page">
      <div className="portal-hero">
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <div className="portal-grid">
        <article className="portal-card">
          <h3>Overview</h3>
          <p>Dashboard widgets and lists for this section can be added here.</p>
        </article>
        <article className="portal-card">
          <h3>Recent activity</h3>
          <p>Recent updates, ownership, and status indicators can be shown in this panel.</p>
        </article>
      </div>
    </section>
  );
}

function AgentPromptSettings() {
  const agentOptions = ['assistant', 'support', 'interviewer', 'realtime', 'observer'];
  const [records, setRecords] = useState([]);
  const [selectedAgent, setSelectedAgent] = useState('interviewer');
  const [draftPrompt, setDraftPrompt] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');

  const selectedRecord = useMemo(
    () => records.find((item) => item.agent === selectedAgent) || null,
    [records, selectedAgent],
  );

  const loadPrompts = useCallback(async () => {
    setLoading(true);
    setError('');
    setMessage('');
    try {
      const response = await apiClient.listAgentPrompts();
      const nextRecords = response?.prompts || [];
      setRecords(nextRecords);
      if (!nextRecords.some((item) => item.agent === selectedAgent) && nextRecords[0]?.agent) {
        setSelectedAgent(nextRecords[0].agent);
      }
    } catch (err) {
      setError(err.message || 'Failed to load prompts');
    } finally {
      setLoading(false);
    }
  }, [selectedAgent]);

  useEffect(() => {
    loadPrompts();
  }, [loadPrompts]);

  useEffect(() => {
    setDraftPrompt(selectedRecord?.prompt || '');
  }, [selectedRecord]);

  async function savePrompt() {
    if (!selectedAgent) return;
    setSaving(true);
    setError('');
    setMessage('');
    try {
      const updated = await apiClient.updateAgentPrompt(selectedAgent, draftPrompt);
      setRecords((prev) =>
        prev.map((item) =>
          item.agent === selectedAgent
            ? {
                ...item,
                prompt: updated.prompt,
                default_prompt: updated.default_prompt,
                is_default: updated.is_default,
              }
            : item,
        ),
      );
      setMessage(`Saved prompt for ${selectedAgent}.`);
    } catch (err) {
      setError(err.message || 'Failed to save prompt');
    } finally {
      setSaving(false);
    }
  }

  async function restoreDefault() {
    if (!selectedAgent) return;
    setSaving(true);
    setError('');
    setMessage('');
    try {
      const reset = await apiClient.resetAgentPrompt(selectedAgent);
      setRecords((prev) =>
        prev.map((item) =>
          item.agent === selectedAgent
            ? {
                ...item,
                prompt: reset.prompt,
                default_prompt: reset.default_prompt,
                is_default: reset.is_default,
              }
            : item,
        ),
      );
      setDraftPrompt(reset.prompt);
      setMessage(`Restored default prompt for ${selectedAgent}.`);
    } catch (err) {
      setError(err.message || 'Failed to restore default prompt');
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="portal-page">
      <div className="portal-hero">
        <h1>Settings</h1>
        <p>Manage AI agent prompts used during interviews and meetings.</p>
      </div>
      <div className="portal-grid settings-grid">
        <article className="portal-card prompt-settings-card">
          <h3>Agent Prompt</h3>
          <p>Select an agent, edit its instruction prompt, and save.</p>

          <label htmlFor="agent-prompt-selector">AI agent</label>
          <select
            id="agent-prompt-selector"
            value={selectedAgent}
            onChange={(e) => setSelectedAgent(e.target.value)}
            disabled={loading || saving}
          >
            {agentOptions.map((agent) => (
              <option key={agent} value={agent}>
                {agent}
              </option>
            ))}
          </select>

          <label htmlFor="agent-prompt-editor">Prompt / instructions</label>
          <textarea
            id="agent-prompt-editor"
            rows={20}
            value={draftPrompt}
            onChange={(e) => setDraftPrompt(e.target.value)}
            disabled={loading || saving}
          />
          <div className="settings-meta-row">
            <small>
              {selectedRecord
                ? selectedRecord.is_default
                  ? 'Using default prompt'
                  : 'Using custom prompt override'
                : 'Prompt record unavailable. Click Refresh after backend is reachable.'}
            </small>
          </div>
          <div className="settings-actions">
            <button type="button" onClick={savePrompt} disabled={loading || saving || !draftPrompt.trim()}>
              {saving ? 'Saving...' : 'Save prompt'}
            </button>
            <button
              type="button"
              className="secondary-btn"
              onClick={restoreDefault}
              disabled={loading || saving || !selectedRecord || selectedRecord.is_default}
            >
              Restore default
            </button>
            <button type="button" className="secondary-btn" onClick={loadPrompts} disabled={loading || saving}>
              Refresh
            </button>
          </div>
          {loading && <p>Loading prompts...</p>}
          {error && <p className="error">{error}</p>}
          {message && <p className="success">{message}</p>}
        </article>
      </div>
    </section>
  );
}

function HomePage({ user, tokenInfo, defaultRoom, onJoin, onLeaveMeeting, onLogout }) {
  const [activeMenu, setActiveMenu] = useState('meeting');
  const [applicationsPrefill, setApplicationsPrefill] = useState(null);
  const [applicationFocusId, setApplicationFocusId] = useState(null);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const userMenuRef = useRef(null);
  const menuItems = [
    { key: 'meeting', label: 'Meeting Demo' },
    { key: 'positions', label: 'Positions' },
    { key: 'candidates', label: 'Candidates' },
    { key: 'applications', label: 'Applications' },
    { key: 'interviews', label: 'Interviews' },
    { key: 'settings', label: 'Settings' },
  ];
  const userInitials = user
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join('');

  useEffect(() => {
    function handleDocumentClick(event) {
      const menuRoot = userMenuRef.current;
      if (!menuRoot) return;
      if (!menuRoot.contains(event.target)) {
        setUserMenuOpen(false);
      }
    }
    document.addEventListener('mousedown', handleDocumentClick);
    return () => {
      document.removeEventListener('mousedown', handleDocumentClick);
    };
  }, []);

  async function joinInterviewRoom(roomName, selectedAgent = 'interviewer') {
    const room = String(roomName || '').trim();
    if (!room) return;
    const agent = String(selectedAgent || 'interviewer').trim();
    const token = await apiClient.issueToken({
      room,
      display_name: user,
      ai_enabled: true,
      agent,
      instructions: null,
      capabilities: {
        can_publish: true,
        can_subscribe: true,
        can_publish_data: true,
        can_publish_sources: ['microphone', 'camera', 'screen_share'],
      },
    });
    onJoin({
      ...token,
      selectedAgent: agent,
      aiEnabled: true,
      instructions: token.instructions || '',
      realtimeModel: 'gpt-realtime-mini',
      audioEnabled: true,
      videoEnabled: true,
    });
    setActiveMenu('meeting');
  }

  function openApplicationsFromEntity(prefill) {
    setApplicationsPrefill(prefill || null);
    setApplicationFocusId(null);
    setActiveMenu('applications');
  }

  function openApplicationDetails(applicationId) {
    setApplicationFocusId(applicationId || null);
    setApplicationsPrefill(null);
    setActiveMenu('applications');
  }

  return (
    <div className="portal-shell">
      <header className="portal-topbar">
        <img src={authLogo} alt="Bristlecone" className="portal-logo" />
        <div className="portal-user-menu" ref={userMenuRef}>
          <button
            type="button"
            className="portal-user"
            onClick={() => setUserMenuOpen((open) => !open)}
            aria-haspopup="menu"
            aria-expanded={userMenuOpen}
          >
            <span className="portal-avatar">{userInitials || 'U'}</span>
            <span>{user}</span>
          </button>
          {userMenuOpen && (
            <div className="portal-user-dropdown" role="menu">
              <button
                type="button"
                className="portal-user-dropdown-item"
                onClick={() => {
                  setUserMenuOpen(false);
                  onLogout();
                }}
              >
                Logout
              </button>
            </div>
          )}
        </div>
      </header>

      <div className="portal-body">
        <aside className="portal-nav">
          {menuItems.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`portal-nav-item ${activeMenu === item.key ? 'active' : ''}`}
              onClick={() => setActiveMenu(item.key)}
            >
              {item.label}
            </button>
          ))}
        </aside>

        <main className="portal-content">
          {activeMenu === 'meeting' &&
            (tokenInfo ? (
              <MeetingView tokenInfo={tokenInfo} onLeave={onLeaveMeeting} userName={user} />
            ) : (
              <JoinCard user={user} onJoin={onJoin} defaultRoom={defaultRoom} />
            ))}
          {activeMenu === 'positions' && (
            <PositionsPage onCreateApplication={openApplicationsFromEntity} />
          )}
          {activeMenu === 'candidates' && (
            <CandidatesPage onCreateApplication={openApplicationsFromEntity} />
          )}
          {activeMenu === 'applications' && (
            <ApplicationsPage
              prefill={applicationsPrefill}
              selectedApplicationId={applicationFocusId}
              onPrefillHandled={() => setApplicationsPrefill(null)}
              onJoinInterview={joinInterviewRoom}
              onOpenInterviews={() => setActiveMenu('interviews')}
            />
          )}
          {activeMenu === 'interviews' && (
            <InterviewsPage
              onJoinInterview={joinInterviewRoom}
              onOpenApplication={openApplicationDetails}
            />
          )}
          {activeMenu === 'settings' && (
            <AgentPromptSettings />
          )}
        </main>
      </div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState('');
  const [loading, setLoading] = useState(true);
  const [tokenInfo, setTokenInfo] = useState(null);
  const [lastRoom, setLastRoom] = useState('demo-room');

  useEffect(() => {
    let mounted = true;
    apiClient
      .me()
      .then((me) => {
        if (mounted) {
          setUser(me.username);
        }
      })
      .catch(() => {})
      .finally(() => {
        if (mounted) {
          setLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  async function logout() {
    await apiClient.logout().catch(() => {});
    setTokenInfo(null);
    setUser('');
    setLastRoom('demo-room');
  }

  if (loading) return <main className="centered">Loading...</main>;
  if (!user) return <LoginCard onLoggedIn={setUser} />;
  return (
    <HomePage
      user={user}
      tokenInfo={tokenInfo}
      defaultRoom={lastRoom}
      onJoin={(nextTokenInfo) => {
        setTokenInfo(nextTokenInfo);
        if (nextTokenInfo?.room) setLastRoom(nextTokenInfo.room);
      }}
      onLeaveMeeting={() => setTokenInfo(null)}
      onLogout={logout}
    />
  );
}
