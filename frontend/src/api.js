const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8080';

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
    throw new Error(body.detail || `request failed: ${response.status}`);
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
};
