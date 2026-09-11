/* Shared fetch wrapper (doc §10).
 *
 * One place to deal with the cross-cutting HTTP concerns so page code can just
 * await a value. The 401 / 428 branches are the hooks the security phase fills
 * in: 401 -> refresh-then-retry, 428 -> redirect to the gate the server names
 * (forced password change or 2FA enrolment).
 */
(function () {
  'use strict';

  class ApiError extends Error {
    constructor(message, status, body) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.body = body || {};
    }
  }

  async function apiFetch(path, options) {
    const opts = Object.assign({ credentials: 'same-origin' }, options || {});
    opts.headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});

    if (opts.json !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(opts.json);
      opts.method = opts.method || 'POST';
      delete opts.json;
    }

    let response;
    try {
      response = await fetch(path, opts);
    } catch (err) {
      // Network-level failure: no status, no body.
      throw new ApiError('ติดต่อเซิร์ฟเวอร์ไม่ได้', 0, {});
    }

    let body = {};
    if (response.status !== 204) {
      body = await response.json().catch(() => ({}));
    }

    if (response.ok) return body;

    if (response.status === 401) {
      // Security phase: attempt a silent refresh here, then retry once.
      window.dispatchEvent(new CustomEvent('api:unauthorized', { detail: body }));
    } else if (response.status === 428) {
      // Precondition Required -- the server is telling us which gate to show.
      window.dispatchEvent(new CustomEvent('api:precondition', { detail: body }));
    }

    throw new ApiError(body.detail || response.statusText || 'คำขอล้มเหลว', response.status, body);
  }

  window.apiFetch = apiFetch;
  window.ApiError = ApiError;
})();
