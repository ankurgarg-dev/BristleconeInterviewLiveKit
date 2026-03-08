import '@livekit/components-styles';
import {
  LiveKitRoom,
  PreJoin,
  VideoConference,
  useParticipants,
} from '@livekit/components-react';
import { useEffect, useMemo, useState } from 'react';
import { apiClient } from './api';
import './styles.css';

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
    <div className="auth-card">
      <h1>Sign in to meeting app</h1>
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
        isAi: p.identity.startsWith('agent-') || p.identity.includes('assistant'),
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

function MeetingView({ tokenInfo, onLeave }) {
  const [error, setError] = useState('');

  return (
    <div className="meeting-layout">
      <LiveKitRoom
        token={tokenInfo.token}
        serverUrl={tokenInfo.server_url}
        connect={true}
        audio={tokenInfo.audioEnabled}
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
        <div className="meeting-shell">
          <div className="meeting-main">
            {error && <div className="banner error">{error}</div>}
            <VideoConference />
          </div>
          <ParticipantsSidebar />
        </div>
      </LiveKitRoom>
    </div>
  );
}

function JoinCard({ user, onJoin, onLogout }) {
  const [room, setRoom] = useState('demo-room');
  const [aiEnabled, setAiEnabled] = useState(true);
  const [agent, setAgent] = useState('assistant');
  const [instructions, setInstructions] = useState('');
  const [pendingChoices, setPendingChoices] = useState(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function startMeeting(choices) {
    setLoading(true);
    setError('');
    try {
      const tokenInfo = await apiClient.issueToken({
        room,
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
        audioEnabled: choices.audioEnabled,
        videoEnabled: choices.videoEnabled,
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="join-page">
      <header>
        <h1>LiveKit Meeting</h1>
        <div className="user-actions">
          <span>{user}</span>
          <button onClick={onLogout}>Logout</button>
        </div>
      </header>

      <div className="join-grid">
        <section className="join-config">
          <label htmlFor="room">Room</label>
          <input id="room" value={room} onChange={(e) => setRoom(e.target.value)} />

          <label htmlFor="agent">AI agent</label>
          <select id="agent" value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="assistant">assistant</option>
            <option value="support">support</option>
            <option value="interviewer">interviewer</option>
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
            disabled={!pendingChoices || loading}
            onClick={() => pendingChoices && startMeeting(pendingChoices)}
          >
            {loading ? 'Joining...' : 'Join meeting'}
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
            onSubmit={(choices) => setPendingChoices(choices)}
            onError={(err) => setError(err.message)}
            persistUserChoices={true}
          />
        </section>
      </div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState('');
  const [loading, setLoading] = useState(true);
  const [tokenInfo, setTokenInfo] = useState(null);

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
  }

  if (loading) return <main className="centered">Loading...</main>;
  if (!user) return <LoginCard onLoggedIn={setUser} />;
  if (!tokenInfo) return <JoinCard user={user} onJoin={setTokenInfo} onLogout={logout} />;

  return <MeetingView tokenInfo={tokenInfo} onLeave={() => setTokenInfo(null)} />;
}
