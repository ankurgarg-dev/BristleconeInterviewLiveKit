import { useCallback, useEffect, useState } from 'react';
import { apiClient } from '../api';

const EMPTY_FORM = {
  role_title: '',
  jd_text: '',
  level: '',
  must_haves: '',
  nice_to_haves: '',
  tech_stack: '',
  focus_areas: '',
  evaluation_policy: '',
  extraction_confidence: {},
  missing_fields: [],
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
  return {
    role_title: form.role_title.trim(),
    jd_text: form.jd_text,
    level: form.level.trim(),
    must_haves: textToList(form.must_haves),
    nice_to_haves: textToList(form.nice_to_haves),
    tech_stack: textToList(form.tech_stack),
    focus_areas: textToList(form.focus_areas),
    evaluation_policy: form.evaluation_policy.trim(),
    extraction_confidence: form.extraction_confidence || {},
    missing_fields: Array.isArray(form.missing_fields) ? form.missing_fields : [],
  };
}

function fromPosition(position) {
  return {
    role_title: position.role_title || '',
    jd_text: position.jd_text || '',
    level: position.level || '',
    must_haves: listToText(position.must_haves),
    nice_to_haves: listToText(position.nice_to_haves),
    tech_stack: listToText(position.tech_stack),
    focus_areas: listToText(position.focus_areas),
    evaluation_policy: position.evaluation_policy || '',
    extraction_confidence: position.extraction_confidence || {},
    missing_fields: position.missing_fields || [],
  };
}

function confidencePercent(value) {
  if (typeof value !== 'number' || Number.isNaN(value)) return null;
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

export function PositionsPage() {
  const [positions, setPositions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [extractMessage, setExtractMessage] = useState('');
  const [extractError, setExtractError] = useState('');
  const [openingPosition, setOpeningPosition] = useState(false);
  const [editingId, setEditingId] = useState(null);
  const [form, setForm] = useState(EMPTY_FORM);
  const [uploadedFile, setUploadedFile] = useState(null);

  const isEditMode = Boolean(editingId);

  const loadPositions = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const rows = await apiClient.listPositions();
      setPositions(rows);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPositions();
  }, [loadPositions]);

  function resetToCreate() {
    setEditingId(null);
    setUploadedFile(null);
    setForm(EMPTY_FORM);
    setMessage('');
    setError('');
    setExtractMessage('');
    setExtractError('');
  }

  async function openPosition(positionId) {
    setOpeningPosition(true);
    setError('');
    setMessage('');
    setExtractMessage('');
    setExtractError('');
    try {
      const fresh = await apiClient.getPosition(positionId);
      setEditingId(fresh.position_id);
      setUploadedFile(null);
      setForm(fromPosition(fresh));
    } catch (err) {
      setError(err.message);
    } finally {
      setOpeningPosition(false);
    }
  }

  async function runExtraction(fileOverride = null) {
    const file = fileOverride || uploadedFile;
    if (isEditMode) return;
    if (!file && !form.jd_text.trim()) {
      setExtractError('Add JD text or upload a file before extraction.');
      return;
    }

    setExtracting(true);
    setExtractError('');
    setExtractMessage(`Extracting fields from ${file ? file.name : 'JD text'}...`);
    try {
      const extracted = await apiClient.extractPosition({
        jdText: form.jd_text,
        file,
      });
      setForm((prev) => ({
        ...prev,
        ...fromPosition(extracted),
      }));
      const warningText = extracted.warnings?.length ? ` (${extracted.warnings.join('; ')})` : '';
      setExtractMessage(
        `JD extracted using ${extracted.used_llm ? 'LLM' : 'heuristic parser'}${warningText}`,
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
    setUploadedFile(file);
    if (!file || isEditMode) return;
    setExtractError('');
    setExtractMessage(`File selected (${file.name}). Auto extraction started...`);
    await runExtraction(file);
  }

  async function savePosition(event) {
    event.preventDefault();
    setSaving(true);
    setError('');
    setMessage('');

    try {
      const payload = toPayload(form);
      if (isEditMode) {
        const updated = await apiClient.updatePosition(editingId, payload);
        setForm(fromPosition(updated));
        setMessage('Position updated.');
      } else {
        await apiClient.createPosition(payload);
        setMessage('Position created.');
        setForm(EMPTY_FORM);
        setUploadedFile(null);
      }
      await loadPositions();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteSelectedPosition() {
    if (!editingId || deleting) return;
    const ok =
      typeof window === 'undefined' || typeof window.confirm !== 'function'
        ? true
        : window.confirm('Delete this position? This cannot be undone.');
    if (!ok) return;

    setDeleting(true);
    setError('');
    setMessage('');
    try {
      await apiClient.deletePosition(editingId);
      resetToCreate();
      await loadPositions();
      setMessage('Position deleted.');
    } catch (err) {
      setError(err.message);
    } finally {
      setDeleting(false);
    }
  }

  const overallConfidence = confidencePercent(form.extraction_confidence?.overall);

  return (
    <section className="portal-page positions-page">
      <div className="portal-hero positions-hero">
        <div>
          <h1>Positions</h1>
          <p>Create job openings from JD upload/text with auto extraction, then refine manually.</p>
        </div>
        <div className="positions-actions">
          <button type="button" onClick={resetToCreate}>
            Add New Position
          </button>
          <button type="button" onClick={loadPositions} disabled={loading}>
            {loading ? 'Refreshing...' : 'Refresh'}
          </button>
        </div>
      </div>

      <div className="positions-grid">
        <article className="portal-card positions-list-card">
          <h3>Position List</h3>
          {loading && <p>Loading positions...</p>}
          {!loading && positions.length === 0 && <p>No positions yet.</p>}
          {!loading && positions.length > 0 && (
            <div className="positions-table-wrap">
              <table className="positions-table">
                <thead>
                  <tr>
                    <th>Role</th>
                    <th>Level</th>
                    <th>Skills</th>
                    <th>Updated</th>
                    <th>Version</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((position) => (
                    <tr
                      key={position.position_id}
                      className={position.position_id === editingId ? 'selected' : ''}
                      onClick={() => openPosition(position.position_id)}
                    >
                      <td>{position.role_title || 'Untitled role'}</td>
                      <td>{position.level || '-'}</td>
                      <td>{position.must_haves?.length || 0}</td>
                      <td>{new Date(position.updated_at).toLocaleString()}</td>
                      <td>{position.version}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </article>

        <article className="portal-card positions-form-card">
          <h3>{isEditMode ? 'Edit Position' : 'Create Position'}</h3>
          {openingPosition && <p className="positions-meta">Loading selected position...</p>}
          {!isEditMode && (
            <>
              <label htmlFor="jd-file">JD File (.txt, .pdf, .docx)</label>
              <input id="jd-file" type="file" accept=".txt,.pdf,.docx" onChange={onFileSelected} />
              {uploadedFile && (
                <p className="positions-meta">
                  Selected file: <strong>{uploadedFile.name}</strong>
                </p>
              )}
              {extracting && <p className="positions-meta">Auto extraction in progress...</p>}
              {extractMessage && <p className="helper-note">{extractMessage}</p>}
              {extractError && <p className="error">{extractError}</p>}

              <label htmlFor="jd-text">JD Text</label>
              <textarea
                id="jd-text"
                rows={6}
                value={form.jd_text}
                onChange={(event) =>
                  setForm((prev) => ({
                    ...prev,
                    jd_text: event.target.value,
                  }))
                }
                placeholder="Paste job description text here"
              />

              <button type="button" onClick={() => runExtraction()} disabled={extracting}>
                {extracting ? 'Extracting...' : 'Extract Fields'}
              </button>
            </>
          )}

          <form className="positions-form" onSubmit={savePosition}>
            <label htmlFor="role-title">Role Title</label>
            <input
              id="role-title"
              value={form.role_title}
              onChange={(event) => setForm((prev) => ({ ...prev, role_title: event.target.value }))}
              placeholder="e.g. Senior Backend Engineer"
            />

            <label htmlFor="position-level">Level</label>
            <input
              id="position-level"
              value={form.level}
              onChange={(event) => setForm((prev) => ({ ...prev, level: event.target.value }))}
              placeholder="e.g. Senior"
            />

            <label htmlFor="must-haves">Must Haves (comma or newline separated)</label>
            <textarea
              id="must-haves"
              rows={3}
              value={form.must_haves}
              onChange={(event) => setForm((prev) => ({ ...prev, must_haves: event.target.value }))}
            />

            <label htmlFor="nice-to-haves">Nice To Haves (comma or newline separated)</label>
            <textarea
              id="nice-to-haves"
              rows={3}
              value={form.nice_to_haves}
              onChange={(event) => setForm((prev) => ({ ...prev, nice_to_haves: event.target.value }))}
            />

            <label htmlFor="tech-stack">Tech Stack (comma or newline separated)</label>
            <textarea
              id="tech-stack"
              rows={3}
              value={form.tech_stack}
              onChange={(event) => setForm((prev) => ({ ...prev, tech_stack: event.target.value }))}
            />

            <label htmlFor="focus-areas">Focus Areas (comma or newline separated)</label>
            <textarea
              id="focus-areas"
              rows={3}
              value={form.focus_areas}
              onChange={(event) => setForm((prev) => ({ ...prev, focus_areas: event.target.value }))}
            />

            <label htmlFor="evaluation-policy">Evaluation Policy</label>
            <textarea
              id="evaluation-policy"
              rows={3}
              value={form.evaluation_policy}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  evaluation_policy: event.target.value,
                }))
              }
            />

            {overallConfidence && (
              <p className="positions-meta">Extraction confidence: <strong>{overallConfidence}</strong></p>
            )}
            {!!form.missing_fields?.length && (
              <p className="positions-meta">
                Missing fields: <strong>{form.missing_fields.join(', ')}</strong>
              </p>
            )}

            <div className="form-actions-row">
              <button type="submit" disabled={saving || deleting}>
                {saving ? 'Saving...' : isEditMode ? 'Update Position' : 'Create Position'}
              </button>
              {isEditMode && (
                <button
                  type="button"
                  className="danger-button"
                  onClick={deleteSelectedPosition}
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
