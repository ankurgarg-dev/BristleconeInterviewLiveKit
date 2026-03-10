import { useCallback, useEffect, useState } from 'react';
import { apiClient } from '../api';

const EMPTY_FORM = {
  fullName: '',
  email: '',
  currentTitle: '',
  yearsExperience: '',
  keySkills: '',
  keyProjectHighlights: '',
  candidateContext: '',
  cvTextSummary: '',
  screeningCache: '',
  cvMetadata: null,
};

function listToText(list) {
  return (Array.isArray(list) ? list : []).join(', ');
}

function textToList(value) {
  return String(value || '')
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function toPayload(form) {
  let screeningCache = null;
  const rawCache = String(form.screeningCache || '').trim();
  if (rawCache) {
    try {
      const parsed = JSON.parse(rawCache);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        screeningCache = parsed;
      }
    } catch {
      screeningCache = null;
    }
  }

  return {
    fullName: form.fullName.trim(),
    email: form.email.trim(),
    currentTitle: form.currentTitle.trim(),
    yearsExperience: form.yearsExperience === '' ? null : Number(form.yearsExperience),
    keySkills: textToList(form.keySkills),
    keyProjectHighlights: textToList(form.keyProjectHighlights),
    candidateContext: form.candidateContext.trim(),
    cvTextSummary: form.cvTextSummary.trim(),
    cvMetadata: form.cvMetadata,
    screeningCache,
  };
}

function fromCandidate(candidate) {
  return {
    fullName: candidate.fullName || '',
    email: candidate.email || '',
    currentTitle: candidate.currentTitle || '',
    yearsExperience:
      candidate.yearsExperience === null || candidate.yearsExperience === undefined
        ? ''
        : String(candidate.yearsExperience),
    keySkills: listToText(candidate.keySkills),
    keyProjectHighlights: listToText(candidate.keyProjectHighlights),
    candidateContext: candidate.candidateContext || '',
    cvTextSummary: candidate.cvTextSummary || '',
    screeningCache: candidate.screeningCache ? JSON.stringify(candidate.screeningCache, null, 2) : '',
    cvMetadata: candidate.cvMetadata || null,
  };
}

export function CandidatesPage({ onCreateApplication }) {
  const [candidates, setCandidates] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [openingCandidate, setOpeningCandidate] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [extractMessage, setExtractMessage] = useState('');
  const [extractError, setExtractError] = useState('');
  const [selectedFile, setSelectedFile] = useState(null);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [cvText, setCvText] = useState('');

  const isEditMode = Boolean(editingId);

  const loadCandidates = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const rows = await apiClient.listCandidates();
      setCandidates(rows);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadCandidates();
  }, [loadCandidates]);

  function resetToCreate() {
    setEditingId(null);
    setSelectedFile(null);
    setCvText('');
    setForm({ ...EMPTY_FORM });
    setMessage('');
    setError('');
    setExtractMessage('');
    setExtractError('');
  }

  async function openCandidate(candidateId) {
    setOpeningCandidate(true);
    setError('');
    setMessage('');
    try {
      const fresh = await apiClient.getCandidate(candidateId);
      setEditingId(fresh.id);
      setSelectedFile(null);
      setCvText('');
      setForm(fromCandidate(fresh));
    } catch (err) {
      setError(err.message);
    } finally {
      setOpeningCandidate(false);
    }
  }

  async function runExtraction(fileOverride = null) {
    if (isEditMode) return;
    const file = fileOverride || selectedFile;
    if (!file && !cvText.trim()) {
      setExtractError('Add CV text or upload a CV file before extraction.');
      return;
    }

    setExtracting(true);
    setExtractError('');
    setExtractMessage(`Extracting candidate profile from ${file ? file.name : 'CV text'}...`);
    try {
      const extracted = await apiClient.extractCandidate({
        cvText,
        file,
      });
      setForm((prev) => ({ ...prev, ...fromCandidate(extracted) }));
      const warningText = extracted.warnings?.length ? ` (${extracted.warnings.join('; ')})` : '';
      setExtractMessage(
        `Candidate extracted using ${extracted.used_llm ? 'LLM' : 'heuristic parser'}${warningText}`,
      );
    } catch (err) {
      setExtractError(`Extraction failed: ${err.message}`);
      setExtractMessage('');
    } finally {
      setExtracting(false);
    }
  }

  async function onFileSelected(event) {
    const file = event.target.files?.[0] || null;
    setSelectedFile(file);
    if (!file || isEditMode) return;
    setExtractMessage(`File selected (${file.name}). Auto extraction started...`);
    setExtractError('');
    await runExtraction(file);
  }

  async function saveCandidate(event) {
    event.preventDefault();
    setSaving(true);
    setError('');
    setMessage('');

    try {
      const payload = toPayload(form);
      if (isEditMode) {
        const updated = await apiClient.updateCandidate(editingId, payload);
        setForm(fromCandidate(updated));
        setMessage('Candidate updated.');
      } else {
        await apiClient.createCandidate(payload);
        setMessage('Candidate created.');
        resetToCreate();
      }
      await loadCandidates();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteSelectedCandidate() {
    if (!editingId || deleting) return;
    const ok =
      typeof window === 'undefined' || typeof window.confirm !== 'function'
        ? true
        : window.confirm('Delete this candidate? This cannot be undone.');
    if (!ok) return;

    setDeleting(true);
    setError('');
    setMessage('');
    try {
      await apiClient.deleteCandidate(editingId);
      resetToCreate();
      await loadCandidates();
      setMessage('Candidate deleted.');
    } catch (err) {
      setError(err.message);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <section className="portal-page candidates-page">
      <div className="portal-hero positions-hero">
        <div>
          <h1>Candidates</h1>
          <p>Create candidate profiles from CV upload/text with prefill, then edit manually.</p>
        </div>
        <div className="positions-actions">
          <button type="button" onClick={resetToCreate}>
            Add New Candidate
          </button>
          <button type="button" onClick={loadCandidates} disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="positions-grid">
        <article className="portal-card positions-list-card">
          <h3>Candidate List</h3>
          {loading && <p>Loading candidates...</p>}
          {!loading && candidates.length === 0 && <p>No candidates yet.</p>}
          {!loading && candidates.length > 0 && (
            <div className="positions-table-wrap">
              <table className="positions-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Title</th>
                    <th>Skills</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {candidates.map((candidate) => (
                    <tr
                      key={candidate.id}
                      className={candidate.id === editingId ? 'selected' : ''}
                      onClick={() => openCandidate(candidate.id)}
                    >
                      <td>{candidate.fullName || 'Unnamed'}</td>
                      <td>{candidate.email || '-'}</td>
                      <td>{candidate.currentTitle || '-'}</td>
                      <td>{candidate.keySkills?.length || 0}</td>
                      <td>{new Date(candidate.updatedAt).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </article>

        <article className="portal-card positions-form-card">
          <h3>{isEditMode ? 'Edit Candidate' : 'Create Candidate'}</h3>
          {openingCandidate && <p className="positions-meta">Loading selected candidate...</p>}
          {!isEditMode && (
            <>
              <label htmlFor="cv-file">CV File (.txt, .pdf, .docx)</label>
              <input id="cv-file" type="file" accept=".txt,.pdf,.docx" onChange={onFileSelected} />
              {selectedFile && (
                <p className="positions-meta">
                  Selected file: <strong>{selectedFile.name}</strong>
                </p>
              )}
              {extracting && <p className="positions-meta">Auto extraction in progress...</p>}
              {extractMessage && <p className="helper-note">{extractMessage}</p>}
              {extractError && <p className="error">{extractError}</p>}

              <label htmlFor="cv-text">CV Text</label>
              <textarea
                id="cv-text"
                rows={6}
                value={cvText}
                onChange={(event) => setCvText(event.target.value)}
                placeholder="Paste candidate CV text here"
              />

              <button type="button" onClick={() => runExtraction()} disabled={extracting}>
                {extracting ? 'Extracting...' : 'Extract Candidate'}
              </button>
            </>
          )}

          <form className="positions-form" onSubmit={saveCandidate}>
            <label htmlFor="candidate-fullname">Full Name</label>
            <input
              id="candidate-fullname"
              value={form.fullName}
              onChange={(event) => setForm((prev) => ({ ...prev, fullName: event.target.value }))}
            />

            <label htmlFor="candidate-email">Email</label>
            <input
              id="candidate-email"
              value={form.email}
              onChange={(event) => setForm((prev) => ({ ...prev, email: event.target.value }))}
            />

            <label htmlFor="candidate-title">Current Title</label>
            <input
              id="candidate-title"
              value={form.currentTitle}
              onChange={(event) => setForm((prev) => ({ ...prev, currentTitle: event.target.value }))}
            />

            <label htmlFor="candidate-years">Years Experience</label>
            <input
              id="candidate-years"
              type="number"
              step="0.1"
              min="0"
              value={form.yearsExperience}
              onChange={(event) => setForm((prev) => ({ ...prev, yearsExperience: event.target.value }))}
            />

            <label htmlFor="candidate-keyskills">Key Skills (comma or newline separated)</label>
            <textarea
              id="candidate-keyskills"
              rows={3}
              value={form.keySkills}
              onChange={(event) => setForm((prev) => ({ ...prev, keySkills: event.target.value }))}
            />

            <label htmlFor="candidate-projects">Key Project Highlights (comma or newline separated)</label>
            <textarea
              id="candidate-projects"
              rows={3}
              value={form.keyProjectHighlights}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  keyProjectHighlights: event.target.value,
                }))
              }
            />

            <label htmlFor="candidate-context">Candidate Context</label>
            <textarea
              id="candidate-context"
              rows={3}
              value={form.candidateContext}
              onChange={(event) => setForm((prev) => ({ ...prev, candidateContext: event.target.value }))}
            />

            <label htmlFor="candidate-cv-summary">CV Summary</label>
            <textarea
              id="candidate-cv-summary"
              rows={4}
              value={form.cvTextSummary}
              onChange={(event) => setForm((prev) => ({ ...prev, cvTextSummary: event.target.value }))}
              placeholder="Auto-filled from CV extraction, editable"
            />

            <label htmlFor="candidate-screening">Screening Cache (JSON keyed by positionId)</label>
            <textarea
              id="candidate-screening"
              rows={3}
              value={form.screeningCache}
              onChange={(event) => setForm((prev) => ({ ...prev, screeningCache: event.target.value }))}
              placeholder='{"position-id": {"score": 0.8}}'
            />

            {form.cvMetadata && (
              <p className="positions-meta">
                CV metadata: <strong>{form.cvMetadata.originalName}</strong> ({form.cvMetadata.contentType},{' '}
                {form.cvMetadata.size} bytes)
              </p>
            )}

            <div className="form-actions-row">
              <button type="submit" disabled={saving || deleting}>
                {saving ? 'Saving...' : isEditMode ? 'Update Candidate' : 'Create Candidate'}
              </button>
              {isEditMode && (
                <button
                  type="button"
                  className="secondary-button"
                  onClick={() => onCreateApplication?.({ candidateId: editingId })}
                  disabled={saving || deleting}
                >
                  Create Application
                </button>
              )}
              {isEditMode && (
                <button
                  type="button"
                  className="danger-button"
                  onClick={deleteSelectedCandidate}
                  disabled={saving || deleting}
                >
                  {deleting ? 'Deleting...' : 'Delete'}
                </button>
              )}
            </div>
          </form>

          {message && <p className="helper-note">{message}</p>}
          {error && <p className="error">{error}</p>}
        </article>
      </div>
    </section>
  );
}
