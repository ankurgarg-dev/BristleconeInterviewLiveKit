import { useCallback, useEffect, useState } from 'react';
import { apiClient } from '../api';

function scoreText(screening) {
  if (screening && typeof screening.overall_match_score === 'number') {
    return `${Math.round(Math.max(0, Math.min(100, screening.overall_match_score)))}%`;
  }
  const score = screening?.score;
  if (typeof score !== 'number' || Number.isNaN(score)) return '-';
  return `${Math.round(Math.max(0, Math.min(1, score)) * 100)}%`;
}

export function InterviewsPage({ onJoinInterview, onOpenApplication }) {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [downloadingRoom, setDownloadingRoom] = useState('');

  const loadInterviews = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await apiClient.listInterviews();
      setRows(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadInterviews();
  }, [loadInterviews]);

  async function downloadTranscriptForRoom(room) {
    const targetRoom = String(room || '').trim();
    if (!targetRoom) return;
    setDownloadingRoom(targetRoom);
    setError('');
    try {
      const { blob, filename } = await apiClient.downloadTranscript(targetRoom);
      const url = window.URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = filename || `${targetRoom}-transcript.txt`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setError(err.message);
    } finally {
      setDownloadingRoom('');
    }
  }

  return (
    <section className="portal-page applications-page">
      <div className="portal-hero positions-hero">
        <div>
          <h1>Interviews</h1>
          <p>Interview schedule and actions (join, transcript, upcoming video/AI details).</p>
        </div>
        <div className="positions-actions">
          <button type="button" onClick={loadInterviews} disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      <article className="portal-card positions-list-card">
        <h3>Scheduled Interviews</h3>
        {loading && <p>Loading interviews...</p>}
        {!loading && rows.length === 0 && <p>No interviews scheduled yet.</p>}
        {!loading && rows.length > 0 && (
          <div className="positions-table-wrap">
            <table className="positions-table">
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Position</th>
                  <th>Interview</th>
                  <th>Score</th>
                  <th>Actions</th>
                  <th>Application</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.application_id}:${row.interview?.room || 'none'}`}>
                    <td>{row.candidate_snapshot?.fullName || '-'}</td>
                    <td>{row.position_snapshot?.role_title || '-'}</td>
                    <td>
                      <div>{row.interview?.room || '-'}</div>
                      <small>
                        {row.interview?.scheduled_for ? new Date(row.interview.scheduled_for).toLocaleString() : 'Not scheduled'} |{' '}
                        {row.interview?.agent || '-'} | {row.interview?.duration_minutes || 30} min
                        {Array.isArray(row.interviews) ? ` | Attempts: ${row.interviews.length}` : ''}
                      </small>
                    </td>
                    <td>{scoreText(row.screening)}</td>
                    <td>
                      <div className="interview-actions">
                        <button
                          type="button"
                          className="icon-action-btn"
                          title="Join interview"
                          aria-label="Join interview"
                          onClick={() => onJoinInterview?.(row.interview?.room, row.interview?.agent || 'interviewer')}
                          disabled={!row.interview?.room}
                        >
                          ▶
                        </button>
                        <button
                          type="button"
                          className="secondary-button icon-action-btn"
                          title="Download transcript"
                          aria-label="Download transcript"
                          onClick={() => downloadTranscriptForRoom(row.interview?.room)}
                          disabled={!row.interview?.transcript_available || downloadingRoom === row.interview?.room}
                        >
                          {downloadingRoom === row.interview?.room ? '…' : '📝'}
                        </button>
                        <button
                          type="button"
                          className="secondary-button icon-action-btn"
                          disabled={true}
                          title="Download video (coming soon)"
                          aria-label="Download video (coming soon)"
                        >
                          🎥
                        </button>
                        <button
                          type="button"
                          className="secondary-button icon-action-btn"
                          disabled={true}
                          title="Interview AI details (coming soon)"
                          aria-label="Interview AI details (coming soon)"
                        >
                          ✨
                        </button>
                      </div>
                    </td>
                    <td>
                      <button type="button" className="secondary-button" onClick={() => onOpenApplication?.(row.application_id)}>
                        Open Application
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {error && <p className="error">{error}</p>}
      </article>
    </section>
  );
}
