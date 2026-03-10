import { useCallback, useEffect, useMemo, useState } from 'react';
import { apiClient } from '../api';

const EMPTY_FORM = {
  position_id: '',
  candidate_id: '',
  status: 'applied',
  source: 'manual',
  notes: '',
  screening: null,
  interview: null,
  interviews: [],
  interview_happened: false,
  delete_allowed: true,
};

function toPayload(form) {
  return {
    position_id: String(form.position_id || '').trim(),
    candidate_id: String(form.candidate_id || '').trim(),
    status: String(form.status || 'applied').trim() || 'applied',
    source: String(form.source || 'manual').trim() || 'manual',
    notes: String(form.notes || '').trim(),
    screening: form.screening || null,
    interview: form.interview || null,
    interviews: Array.isArray(form.interviews) ? form.interviews : [],
  };
}

function fromApplication(app) {
  return {
    position_id: app.position_id || '',
    candidate_id: app.candidate_id || '',
    status: app.status || 'applied',
    source: app.source || 'manual',
    notes: app.notes || '',
    screening: app.screening || null,
    interview: app.interview || null,
    interviews: Array.isArray(app.interviews) ? app.interviews : [],
    interview_happened: Boolean(app.interview_happened),
    delete_allowed: app.delete_allowed !== false,
  };
}

function scoreText(screening) {
  if (screening && typeof screening.overall_match_score === 'number') {
    const pct = Math.max(0, Math.min(100, screening.overall_match_score));
    return `${Math.round(pct)}%`;
  }
  const score = screening?.score;
  if (typeof score !== 'number' || Number.isNaN(score)) return '-';
  return `${Math.round(Math.max(0, Math.min(1, score)) * 100)}%`;
}

function isoToLocalInput(isoText) {
  const text = String(isoText || '').trim();
  if (!text) return '';
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (value) => String(value).padStart(2, '0');
  const yyyy = date.getFullYear();
  const mm = pad(date.getMonth() + 1);
  const dd = pad(date.getDate());
  const hh = pad(date.getHours());
  const min = pad(date.getMinutes());
  return `${yyyy}-${mm}-${dd}T${hh}:${min}`;
}

function localInputToIso(value) {
  const text = String(value || '').trim();
  if (!text) return null;
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString();
}

export function ApplicationsPage({
  prefill = null,
  selectedApplicationId = null,
  onPrefillHandled,
  onJoinInterview,
  onOpenInterviews,
}) {
  const [applications, setApplications] = useState([]);
  const [positions, setPositions] = useState([]);
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [screening, setScreening] = useState(false);
  const [scheduling, setScheduling] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [screeningMessage, setScreeningMessage] = useState('');
  const [editingId, setEditingId] = useState(null);
  const [openingApplication, setOpeningApplication] = useState(false);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [positionSearch, setPositionSearch] = useState('');
  const [candidateSearch, setCandidateSearch] = useState('');
  const [scheduleFor, setScheduleFor] = useState('');
  const [scheduleNotes, setScheduleNotes] = useState('');
  const [interviewAgent, setInterviewAgent] = useState('interviewer');

  const isEditMode = Boolean(editingId);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError('');
    const [applicationsResult, positionsResult, candidatesResult] = await Promise.allSettled([
      apiClient.listApplications(),
      apiClient.listPositions(),
      apiClient.listCandidates(),
    ]);

    if (applicationsResult.status === 'fulfilled') {
      setApplications(applicationsResult.value);
    } else {
      setApplications([]);
      setError((prev) => {
        const next = String(applicationsResult.reason?.message || 'Failed to load applications');
        if (prev) return `${prev} | ${next}`;
        return `${next}. If you just pulled frontend changes, restart backend API server.`;
      });
    }

    if (positionsResult.status === 'fulfilled') {
      setPositions(positionsResult.value);
    } else {
      setPositions([]);
      setError((prev) => {
        const next = String(positionsResult.reason?.message || 'Failed to load positions');
        if (prev) return `${prev} | ${next}`;
        return next;
      });
    }

    if (candidatesResult.status === 'fulfilled') {
      setCandidates(candidatesResult.value);
    } else {
      setCandidates([]);
      setError((prev) => {
        const next = String(candidatesResult.reason?.message || 'Failed to load candidates');
        if (prev) return `${prev} | ${next}`;
        return next;
      });
    }

    setLoading(false);
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const positionById = useMemo(() => {
    const map = new Map();
    positions.forEach((row) => map.set(row.position_id, row));
    return map;
  }, [positions]);

  const candidateById = useMemo(() => {
    const map = new Map();
    candidates.forEach((row) => map.set(row.id, row));
    return map;
  }, [candidates]);

  const filteredPositions = useMemo(() => {
    const needle = positionSearch.trim().toLowerCase();
    if (!needle) return positions;
    return positions.filter((row) => {
      const text = [
        row.role_title || '',
        row.level || '',
        row.jd_text || '',
        ...(Array.isArray(row.must_haves) ? row.must_haves : []),
        ...(Array.isArray(row.tech_stack) ? row.tech_stack : []),
        ...(Array.isArray(row.nice_to_haves) ? row.nice_to_haves : []),
        ...(Array.isArray(row.focus_areas) ? row.focus_areas : []),
      ]
        .join(' ')
        .toLowerCase();
      return text.includes(needle);
    });
  }, [positions, positionSearch]);

  const filteredCandidates = useMemo(() => {
    const needle = candidateSearch.trim().toLowerCase();
    if (!needle) return candidates;
    return candidates.filter((row) => {
      const text = `${row.fullName || ''} ${row.currentTitle || ''} ${row.email || ''}`.toLowerCase();
      return text.includes(needle);
    });
  }, [candidates, candidateSearch]);

  function resetToCreate() {
    setEditingId(null);
    setForm({ ...EMPTY_FORM });
    setScheduleFor('');
    setScheduleNotes('');
    setInterviewAgent('interviewer');
    setPositionSearch('');
    setCandidateSearch('');
    setError('');
    setMessage('');
    setScreeningMessage('');
  }

  async function openApplication(applicationId) {
    setOpeningApplication(true);
    setError('');
    setMessage('');
    setScreeningMessage('');
    try {
      const fresh = await apiClient.getApplication(applicationId);
      setEditingId(fresh.application_id);
      setForm(fromApplication(fresh));
      setScheduleFor(isoToLocalInput(fresh.interview?.scheduled_for));
      setScheduleNotes(fresh.interview?.notes || '');
      setInterviewAgent(fresh.interview?.agent || 'interviewer');
    } catch (err) {
      setError(err.message);
    } finally {
      setOpeningApplication(false);
    }
  }

  useEffect(() => {
    if (!selectedApplicationId) return;
    openApplication(selectedApplicationId);
  }, [selectedApplicationId]);

  useEffect(() => {
    if (!prefill || isEditMode) return;
    const next = { ...EMPTY_FORM, ...form };
    if (prefill.positionId) {
      next.position_id = prefill.positionId;
    }
    if (prefill.candidateId) {
      next.candidate_id = prefill.candidateId;
    }
    setForm(next);
    setMessage('Prefilled selection from another tab. Complete the remaining fields and save.');
    if (onPrefillHandled) onPrefillHandled();
  }, [prefill, isEditMode]);

  async function saveApplication(event) {
    event.preventDefault();
    setSaving(true);
    setError('');
    setMessage('');
    setScreeningMessage('');
    try {
      const payload = toPayload(form);
      if (!payload.position_id || !payload.candidate_id) {
        throw new Error('Select both a position and candidate.');
      }
      if (!isEditMode && !payload.screening) {
        throw new Error('Run AI screening first, then create the application.');
      }
      if (isEditMode) {
        const updated = await apiClient.updateApplication(editingId, payload);
        setForm(fromApplication(updated));
        setMessage('Application updated.');
      } else {
        const created = await apiClient.createApplication(payload);
        setEditingId(created.application_id);
        setForm(fromApplication(created));
        setMessage('Application created with screening snapshot. You can now schedule interview.');
      }
      await loadData();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteSelectedApplication() {
    if (!editingId || deleting) return;
    if (form.delete_allowed === false || form.interview_happened) {
      setError('Delete is not allowed after interview has happened.');
      return;
    }
    const ok =
      typeof window === 'undefined' || typeof window.confirm !== 'function'
        ? true
        : window.confirm('Delete this application? This cannot be undone.');
    if (!ok) return;

    setDeleting(true);
    setError('');
    setMessage('');
    setScreeningMessage('');
    try {
      await apiClient.deleteApplication(editingId);
      resetToCreate();
      await loadData();
      setMessage('Application deleted.');
    } catch (err) {
      setError(err.message);
    } finally {
      setDeleting(false);
    }
  }

  async function runScreening() {
    if (screening) return;
    if (!form.position_id || !form.candidate_id) {
      setError('Select both position and candidate before screening.');
      return;
    }
    setScreening(true);
    setError('');
    setScreeningMessage('');
    try {
      if (editingId) {
        const result = await apiClient.screenApplication(editingId);
        setForm(fromApplication(result.application));
        const warnings = result.warnings?.length ? ` (${result.warnings.join('; ')})` : '';
        setScreeningMessage(`Screening updated via ${result.used_llm ? 'LLM' : 'heuristics'}${warnings}`);
      } else {
        const result = await apiClient.screenApplicationPreview({
          position_id: form.position_id,
          candidate_id: form.candidate_id,
        });
        setForm((prev) => ({ ...prev, screening: result.screening, status: 'screened' }));
        const warnings = result.warnings?.length ? ` (${result.warnings.join('; ')})` : '';
        setScreeningMessage(`Preview screening generated via ${result.used_llm ? 'LLM' : 'heuristics'}${warnings}`);
      }
      await loadData();
    } catch (err) {
      setError(err.message);
    } finally {
      setScreening(false);
    }
  }

  async function scheduleInterviewNow() {
    if (!editingId || scheduling) return;
    setScheduling(true);
    setError('');
    setMessage('');
    try {
      const updated = await apiClient.scheduleInterview(editingId, {
        scheduled_for: localInputToIso(scheduleFor),
        stage: null,
        agent: interviewAgent,
        notes: scheduleNotes,
      });
      setForm(fromApplication(updated));
      setMessage(`Interview scheduled in room "${updated.interview?.room || 'n/a'}".`);
      await loadData();
    } catch (err) {
      setError(err.message);
    } finally {
      setScheduling(false);
    }
  }

  const selectedPosition = positionById.get(form.position_id);
  const selectedCandidate = candidateById.get(form.candidate_id);

  return (
    <section className="portal-page applications-page">
      <div className="portal-hero positions-hero">
        <div>
          <h1>Applications</h1>
          <p>
            Match a candidate with a position, run AI screening score + justification, then schedule and run the
            interview.
          </p>
        </div>
        <div className="positions-actions">
          <button type="button" onClick={resetToCreate}>
            Add New Application
          </button>
          <button type="button" onClick={loadData} disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="positions-grid">
        <article className="portal-card positions-list-card">
          <h3>Application List</h3>
          {loading && <p>Loading applications...</p>}
          {!loading && applications.length === 0 && <p>No applications yet.</p>}
          {!loading && applications.length > 0 && (
            <div className="positions-table-wrap">
              <table className="positions-table">
                <thead>
                  <tr>
                    <th>Candidate</th>
                    <th>Position</th>
                    <th>Status</th>
                    <th>Score</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {applications.map((row) => (
                    <tr
                      key={row.application_id}
                      className={row.application_id === editingId ? 'selected' : ''}
                      onClick={() => openApplication(row.application_id)}
                    >
                      <td>{row.candidate_snapshot?.fullName || candidateById.get(row.candidate_id)?.fullName || '-'}</td>
                      <td>
                        {row.position_snapshot?.role_title || positionById.get(row.position_id)?.role_title || 'Untitled role'}
                      </td>
                      <td>{row.status || '-'}</td>
                      <td>{scoreText(row.screening)}</td>
                      <td>{new Date(row.updated_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </article>

        <article className="portal-card positions-form-card">
          <h3>{isEditMode ? 'Edit Application' : 'Create Application'}</h3>
          {openingApplication && <p className="positions-meta">Loading selected application...</p>}

          <form className="positions-form" id="application-form" onSubmit={saveApplication}>
            <label htmlFor="application-position-search">Search Position</label>
            <input
              id="application-position-search"
              value={positionSearch}
              onChange={(event) => setPositionSearch(event.target.value)}
              placeholder="Type role name or level"
              disabled={loading}
            />
            {loading && <p className="positions-meta">Loading positions...</p>}

            <label htmlFor="application-position">Position</label>
            <select
              id="application-position"
              value={form.position_id}
              onChange={(event) => setForm((prev) => ({ ...prev, position_id: event.target.value }))}
              disabled={loading}
            >
              <option value="">{loading ? 'Loading positions...' : 'Select a position'}</option>
              {filteredPositions.map((position) => (
                <option key={position.position_id} value={position.position_id}>
                  {position.role_title || 'Untitled role'} {position.level ? `(${position.level})` : ''}
                </option>
              ))}
            </select>

            <label htmlFor="application-candidate-search">Search Candidate</label>
            <input
              id="application-candidate-search"
              value={candidateSearch}
              onChange={(event) => setCandidateSearch(event.target.value)}
              placeholder="Type candidate name, title, or email"
              disabled={loading}
            />
            {loading && <p className="positions-meta">Loading candidates...</p>}

            <label htmlFor="application-candidate">Candidate</label>
            <select
              id="application-candidate"
              value={form.candidate_id}
              onChange={(event) => setForm((prev) => ({ ...prev, candidate_id: event.target.value }))}
              disabled={loading}
            >
              <option value="">{loading ? 'Loading candidates...' : 'Select a candidate'}</option>
              {filteredCandidates.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.fullName || 'Unnamed'} {candidate.currentTitle ? `(${candidate.currentTitle})` : ''}
                </option>
              ))}
            </select>

            <label htmlFor="application-source">Source</label>
            <input
              id="application-source"
              value={form.source}
              onChange={(event) => setForm((prev) => ({ ...prev, source: event.target.value }))}
              placeholder="manual, referral, career-site..."
            />

            <label htmlFor="application-notes">Notes</label>
            <textarea
              id="application-notes"
              rows={3}
              value={form.notes}
              onChange={(event) => setForm((prev) => ({ ...prev, notes: event.target.value }))}
              placeholder="Capture recruiter notes, next steps, or feedback."
            />

            {(selectedPosition || selectedCandidate) && (
              <p className="positions-meta">
                Matching preview: <strong>{selectedCandidate?.fullName || 'Candidate'}</strong> vs{' '}
                <strong>{selectedPosition?.role_title || 'Position'}</strong>
              </p>
            )}

          </form>

          <div className="application-section">
            <h4>AI Screening</h4>
            {!isEditMode && <p className="positions-meta">Run screening before creating the application.</p>}
            {form.screening && (
              <div className="application-screening">
                <p className="positions-meta">
                  Score: <strong>{scoreText(form.screening)}</strong> | Last updated:{' '}
                  {form.screening.updated_at ? new Date(form.screening.updated_at).toLocaleString() : '-'}
                </p>
                {form.screening.hiring_recommendation && (
                  <p className="positions-meta">
                    Recommendation: <strong>{form.screening.hiring_recommendation}</strong>
                  </p>
                )}
                {!!Object.keys(form.screening.score_breakdown || {}).length && (
                  <p className="positions-meta">
                    Breakdown: Tech {form.screening.score_breakdown.technical_skills_match ?? '-'} / Experience{' '}
                    {form.screening.score_breakdown.relevant_experience ?? '-'} / Domain{' '}
                    {form.screening.score_breakdown.domain_knowledge ?? '-'} / Tools{' '}
                    {form.screening.score_breakdown.tools_technologies ?? '-'} / Education{' '}
                    {form.screening.score_breakdown.education_certifications ?? '-'} / Overall Fit{' '}
                    {form.screening.score_breakdown.overall_fit ?? '-'}
                  </p>
                )}
                {form.screening.justification && <p>{form.screening.justification}</p>}
                {!!form.screening.matched_skills?.length && (
                  <p className="positions-meta">Matched skills: {form.screening.matched_skills.join(', ')}</p>
                )}
                {!!form.screening.missing_skills?.length && (
                  <p className="positions-meta">Missing skills: {form.screening.missing_skills.join(', ')}</p>
                )}
                {!!form.screening.hiring_reasoning?.length && (
                  <p className="positions-meta">Reasoning: {form.screening.hiring_reasoning.join(' | ')}</p>
                )}
                {!!form.screening.interview_questions?.length && (
                  <p className="positions-meta">
                    Validation Questions: {form.screening.interview_questions.slice(0, 5).join(' | ')}
                  </p>
                )}
              </div>
            )}
            <button
              type="button"
              onClick={runScreening}
              disabled={screening || !form.position_id || !form.candidate_id}
            >
              {screening ? 'Running Screening...' : 'Run AI Screening'}
            </button>
            {screeningMessage && <p className="helper-note">{screeningMessage}</p>}
          </div>

          <div className="application-section">
            <h4>Interview Scheduling</h4>
            {!isEditMode && <p className="positions-meta">Create the application first, then schedule interview.</p>}

            <label htmlFor="application-scheduled-for">Scheduled For</label>
            <input
              id="application-scheduled-for"
              type="datetime-local"
              value={scheduleFor}
              onChange={(event) => setScheduleFor(event.target.value)}
            />

            <label htmlFor="application-agent">Interview Agent</label>
            <select
              id="application-agent"
              value={interviewAgent}
              onChange={(event) => setInterviewAgent(event.target.value)}
            >
              <option value="interviewer">interviewer</option>
              <option value="assistant">assistant</option>
              <option value="support">support</option>
              <option value="realtime">realtime</option>
              <option value="observer">observer</option>
            </select>

            <label htmlFor="application-interview-notes">Interview Notes</label>
            <textarea
              id="application-interview-notes"
              rows={2}
              value={scheduleNotes}
              onChange={(event) => setScheduleNotes(event.target.value)}
            />

            <div className="form-actions-row">
              <button type="button" onClick={scheduleInterviewNow} disabled={!isEditMode || scheduling}>
                {scheduling ? 'Scheduling...' : 'Schedule / Re-schedule Interview'}
              </button>
              <button type="button" className="secondary-button" onClick={() => onOpenInterviews?.()}>
                Open Interviews Tab
              </button>
            </div>

            {form.interview?.room && (
              <div className="application-interview-card">
                <p className="positions-meta">
                  Interview room: <strong>{form.interview.room}</strong>
                </p>
                <p className="positions-meta">
                  Agent: <strong>{form.interview.agent || interviewAgent}</strong> | Scheduled:{' '}
                  {form.interview.scheduled_for ? new Date(form.interview.scheduled_for).toLocaleString() : 'Not set'}
                </p>
                <p className="positions-meta">
                  Transcript: <strong>{form.interview.transcript_available ? 'Available' : 'Not available yet'}</strong>
                </p>
                <button type="button" className="secondary-button" onClick={() => onOpenInterviews?.()}>
                  Manage In Interviews Tab
                </button>
              </div>
            )}
          </div>

          <div className="application-section">
            <h4>Save Application</h4>
            {!isEditMode && !form.screening && (
              <p className="positions-meta">Run AI screening to enable application creation.</p>
            )}
            <div className="form-actions-row">
              <button
                type="submit"
                form="application-form"
                disabled={saving || deleting || (!isEditMode && !form.screening)}
              >
                {saving ? 'Saving...' : isEditMode ? 'Update Application' : 'Create Application'}
              </button>
              {isEditMode && (
                <button
                  type="button"
                  className="danger-button"
                  onClick={deleteSelectedApplication}
                  disabled={saving || deleting || form.delete_allowed === false}
                  title={form.delete_allowed === false ? 'Delete disabled after interview has happened' : ''}
                >
                  {deleting ? 'Deleting...' : 'Delete'}
                </button>
              )}
            </div>
          </div>

          {message && <p className="helper-note">{message}</p>}
          {error && <p className="error">{error}</p>}
        </article>
      </div>
    </section>
  );
}
