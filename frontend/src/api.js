const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8080';

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
    ...options,
  });

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
};
