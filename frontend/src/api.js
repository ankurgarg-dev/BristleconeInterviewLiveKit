const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8080';

function formatApiDetail(detail) {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const text = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const loc = Array.isArray(item.loc) ? item.loc.join('.') : '';
          const msg = typeof item.msg === 'string' ? item.msg : JSON.stringify(item);
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item ?? '');
      })
      .filter(Boolean)
      .join('; ');
    if (text) return text;
  }
  if (detail && typeof detail === 'object') {
    try {
      return JSON.stringify(detail);
    } catch (_) {
      return String(detail);
    }
  }
  return '';
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        ...(options.headers || {}),
      },
      ...options,
    });
  } catch (err) {
    if (err instanceof TypeError) {
      throw new Error(`Network error calling ${API_BASE_URL}${path}. Verify backend API is running on ${API_BASE_URL}.`);
    }
    throw err;
  }

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = formatApiDetail(body.detail);
    throw new Error(detail || `request failed: ${response.status}`);
  }

  return response.status === 204 ? null : response.json();
}

export const apiClient = {
  me: () => api('/api/auth/me', { method: 'GET' }),
  login: (username, password) =>
    api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => api('/api/auth/logout', { method: 'POST' }),
  issueToken: (payload) =>
    api('/api/token', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  createOpenAIRealtimeToken: (payload) =>
    api('/api/openai/realtime/token', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    }),
  clientEvent: (event, detail) =>
    api('/api/client-event', {
      method: 'POST',
      body: JSON.stringify({ event, detail }),
    }),
  appendTranscript: (payload) =>
    api('/api/transcripts/append', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  transcriptStatus: (room) => api(`/api/transcripts/${encodeURIComponent(room)}/status`, { method: 'GET' }),
  downloadTranscript: async (room) => {
    const response = await fetch(`${API_BASE_URL}/api/transcripts/${encodeURIComponent(room)}/download`, {
      method: 'GET',
      credentials: 'include',
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `request failed: ${response.status}`);
    }
    const blob = await response.blob();
    const disposition = response.headers.get('content-disposition') || '';
    const match = disposition.match(/filename=\"?([^\";]+)\"?/i);
    return {
      blob,
      filename: match?.[1] || `${room}-transcript.txt`,
    };
  },
  listPositions: () => api('/api/positions', { method: 'GET' }),
  getPosition: (positionId) => api(`/api/positions/${encodeURIComponent(positionId)}`, { method: 'GET' }),
  createPosition: (payload) =>
    api('/api/positions', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updatePosition: (positionId, payload) =>
    api(`/api/positions/${encodeURIComponent(positionId)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deletePosition: (positionId) =>
    api(`/api/positions/${encodeURIComponent(positionId)}`, {
      method: 'DELETE',
    }),
  extractPosition: async ({ jdText, file }) => {
    const formData = new FormData();
    if (jdText?.trim()) formData.append('jd_text', jdText.trim());
    if (file) formData.append('file', file);
    const response = await fetch(`${API_BASE_URL}/api/positions/extract`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      if (response.status === 404) {
        throw new Error('positions extract API not found. Restart backend API server and try again.');
      }
      throw new Error(body.detail || `request failed: ${response.status}`);
    }
    return response.json();
  },
  listCandidates: () => api('/api/candidates', { method: 'GET' }),
  getCandidate: (candidateId) => api(`/api/candidates/${encodeURIComponent(candidateId)}`, { method: 'GET' }),
  createCandidate: (payload) =>
    api('/api/candidates', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateCandidate: (candidateId, payload) =>
    api(`/api/candidates/${encodeURIComponent(candidateId)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteCandidate: (candidateId) =>
    api(`/api/candidates/${encodeURIComponent(candidateId)}`, {
      method: 'DELETE',
    }),
  extractCandidate: async ({ cvText, file }) => {
    const formData = new FormData();
    if (cvText?.trim()) formData.append('cv_text', cvText.trim());
    if (file) formData.append('file', file);
    const response = await fetch(`${API_BASE_URL}/api/candidates/extract`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      if (response.status === 404) {
        throw new Error('candidates extract API not found. Restart backend API server and try again.');
      }
      throw new Error(body.detail || `request failed: ${response.status}`);
    }
    return response.json();
  },
  listApplications: () => api('/api/applications', { method: 'GET' }),
  getApplication: (applicationId) => api(`/api/applications/${encodeURIComponent(applicationId)}`, { method: 'GET' }),
  createApplication: (payload) =>
    api('/api/applications', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateApplication: (applicationId, payload) =>
    api(`/api/applications/${encodeURIComponent(applicationId)}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteApplication: (applicationId) =>
    api(`/api/applications/${encodeURIComponent(applicationId)}`, {
      method: 'DELETE',
    }),
  screenApplication: (applicationId) =>
    api(`/api/applications/${encodeURIComponent(applicationId)}/screen`, {
      method: 'POST',
    }),
  screenApplicationPreview: (payload) =>
    api('/api/applications/screen-preview', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  scheduleInterview: (applicationId, payload) =>
    api(`/api/applications/${encodeURIComponent(applicationId)}/schedule-interview`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  listInterviews: () => api('/api/interviews', { method: 'GET' }),
  listAgentPrompts: () => api('/api/settings/agent-prompts', { method: 'GET' }),
  updateAgentPrompt: (agent, prompt) =>
    api(`/api/settings/agent-prompts/${encodeURIComponent(agent)}`, {
      method: 'PUT',
      body: JSON.stringify({ prompt }),
    }),
  resetAgentPrompt: (agent) =>
    api(`/api/settings/agent-prompts/${encodeURIComponent(agent)}/reset`, {
      method: 'POST',
    }),
};
