/* CivicTrack API client — the only place that talks to the server. */
(function () {
  'use strict';

  // Same-origin by default. If the page is opened from another dev server (e.g. VS Code Live Server on :5500)
  // or from a file, point at the local API. Override anywhere with: window.CIVICTRACK_API = 'https://host/api'
  const BASE =
    window.CIVICTRACK_API ||
    (location.protocol === 'file:' || location.port === '5500' ? 'http://localhost:3000/api' : '/api');
  const TOKEN_KEY = 'civictrack-token';

  let token = null;
  try { token = sessionStorage.getItem(TOKEN_KEY); } catch { /* storage unavailable */ }
  let onSessionExpired = () => {};

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }

  function setToken(value) {
    token = value || null;
    try {
      if (token) sessionStorage.setItem(TOKEN_KEY, token);
      else sessionStorage.removeItem(TOKEN_KEY);
    } catch { /* ignore */ }
  }

  function qs(params = {}) {
    const p = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') p.set(k, v); });
    const s = p.toString();
    return s ? `?${s}` : '';
  }

  async function request(path, { method = 'GET', body, form, blob = false } = {}) {
    const headers = {};
    let payload;
    if (token) headers.Authorization = `Bearer ${token}`;
    if (form) payload = form;
    else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }

    let res;
    try {
      res = await fetch(BASE + path, { method, headers, body: payload });
    } catch {
      throw new ApiError(0, 'Cannot reach the server. Check your connection and try again.');
    }
    if (blob && res.ok) return res.blob();
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 401 && token) { setToken(null); onSessionExpired(); }
      throw new ApiError(res.status, data.error || 'Something went wrong. Please try again.');
    }
    return data;
  }

  window.CT = window.CT || {};
  window.CT.ApiError = ApiError;
  window.CT.api = {
    hasToken: () => !!token,
    setToken,
    onSessionExpired: (fn) => { onSessionExpired = fn; },

    meta: () => request('/meta'),
    register: (b) => request('/auth/register', { method: 'POST', body: b }),
    login: (b) => request('/auth/login', { method: 'POST', body: b }),
    me: () => request('/auth/me'),

    stats: () => request('/reports/stats'),
    reports: (params) => request('/reports' + qs(params)),
    track: (ref) => request('/reports/track/' + encodeURIComponent(ref)),
    mine: () => request('/reports/mine'),
    createReport: (b) => request('/reports', { method: 'POST', body: b }),
    report: (id) => request(`/reports/${id}`),
    uploadPhoto: (id, file) => {
      const form = new FormData();
      form.append('photo', file);
      return request(`/reports/${id}/photo`, { method: 'POST', form });
    },
    photo: (id) => request(`/reports/${id}/photo`, { blob: true }),

    staffReports: (params) => request('/staff/reports' + qs(params)),
    staffSummary: () => request('/staff/summary'),
    staffUpdate: (id, b) => request(`/staff/reports/${id}`, { method: 'PATCH', body: b }),
    staffDuplicate: (id, duplicateOf) => request(`/staff/reports/${id}/duplicate`, { method: 'POST', body: { duplicateOf } }),

    support: (b) => request('/support', { method: 'POST', body: b }),
  };
})();
