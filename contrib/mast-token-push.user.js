// ==UserScript==
// @name         MAST token -> Home Assistant
// @namespace    ha-mast-water
// @version      0.3.0
// @description  Sends your MAST JWT and remembered-login credential to Home Assistant for automatic session renewal after your initial browser login.
// @match        https://www.mast.co.il/*
// @run-at       document-start
// @grant        unsafeWindow
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @connect      YOUR_HA_HOST
// ==/UserScript==

(function () {
  'use strict';

  // 1) Paste the webhook URL shown by the integration (Settings -> Devices & services
  //    -> MAST Water -> menu -> Reconfigure). Treat it as a secret.
  // 2) Replace YOUR_HA_HOST in the @connect line above with its host name
  //    (Tampermonkey also asks for permission the first time if you forget).
  const HA_WEBHOOK_URL = 'http://homeassistant.local:8123/api/webhook/PASTE_ID_HERE';

  const API_HOST = 'api.mast.co.il';
  const w = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

  function looksLikeJwt(t) {
    return typeof t === 'string' && /^[\w-]+\.[\w-]+\.[\w-]+$/.test(t);
  }

  function expiryMs(t) {
    try {
      let p = t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      p += '='.repeat((4 - (p.length % 4)) % 4);
      return JSON.parse(atob(p)).exp * 1000;
    } catch (e) {
      return NaN;
    }
  }

  function toast(text) {
    const show = () => {
      try {
        if (!document.body) return;
        const d = document.createElement('div');
        d.textContent = text;
        d.style.cssText =
          'position:fixed;z-index:2147483647;bottom:16px;right:16px;padding:8px 12px;' +
          'background:#1b5e20;color:#fff;font:13px sans-serif;border-radius:6px;opacity:.95';
        document.body.appendChild(d);
        setTimeout(() => d.remove(), 4000);
      } catch (e) {
        /* the toast is cosmetic */
      }
    };
    if (document.body) show();
    else document.addEventListener('DOMContentLoaded', show, { once: true });
  }

  let inFlight = false;

  function userToken() {
    try {
      const user = JSON.parse(w.localStorage.getItem('user') || 'null');
      const token = (user && user.token) || JSON.parse(w.localStorage.getItem('userToken') || 'null');
      return typeof token === 'string' ? token.trim() : '';
    } catch (e) {
      return '';
    }
  }

  function pushStoredSession() {
    try {
      push(JSON.parse(w.localStorage.getItem('jwtToken') || 'null'));
    } catch (e) {
      /* storage is optional */
    }
  }

  function push(rawHeaderValue) {
    const token = String(rawHeaderValue || '').replace(/^Bearer\s+/i, '').trim();
    if (!looksLikeJwt(token)) return;
    const exp = expiryMs(token);
    if (!(exp > Date.now())) return; // expired or unreadable
    const resumeToken = userToken();
    if (inFlight || (GM_getValue('lastPushedToken', '') === token &&
        GM_getValue('lastPushedUserToken', '') === resumeToken)) return;
    inFlight = true;
    GM_xmlhttpRequest({
      method: 'POST',
      url: HA_WEBHOOK_URL,
      headers: { 'Content-Type': 'application/json' },
      data: JSON.stringify({ token: token, ...(resumeToken ? { user_token: resumeToken } : {}) }),
      timeout: 15000,
      onload: function (r) {
        inFlight = false;
        if (r.status === 200) {
          GM_setValue('lastPushedToken', token);
          GM_setValue('lastPushedUserToken', resumeToken);
          toast('MAST session sent to Home Assistant');
          setTimeout(pushStoredSession, 0);
        } else {
          console.warn('[mast-token-push] Home Assistant answered', r.status);
        }
      },
      onerror: function () {
        inFlight = false;
        console.warn('[mast-token-push] could not reach Home Assistant');
      },
      ontimeout: function () {
        inFlight = false;
      },
    });
  }

  function isApi(url) {
    try {
      return new URL(url, w.location.href).host === API_HOST;
    } catch (e) {
      return false;
    }
  }

  // Capture both credentials once the login response has populated storage.
  // Outgoing JWT headers can be seen before the user object is written.
  let storageTimer;
  if (w.Storage && w.Storage.prototype) {
    const setItem = w.Storage.prototype.setItem;
    w.Storage.prototype.setItem = function (key, value) {
      const result = setItem.apply(this, arguments);
      try {
        if (this === w.localStorage && ['user', 'userToken', 'jwtToken'].includes(String(key))) {
          clearTimeout(storageTimer);
          storageTimer = setTimeout(pushStoredSession, 0);
        }
      } catch (e) {
        /* never break the page */
      }
      return result;
    };
  }
  setTimeout(pushStoredSession, 0);

  // The MAST web app talks to the API with XHR (axios) and sends the token in a
  // `jwt` request header, so watch for that header being set.
  const XHR = w.XMLHttpRequest && w.XMLHttpRequest.prototype;
  if (XHR) {
    const open = XHR.open;
    const setHeader = XHR.setRequestHeader;
    XHR.open = function (method, url) {
      this.__mastApi = isApi(url);
      return open.apply(this, arguments);
    };
    XHR.setRequestHeader = function (name, value) {
      try {
        if (this.__mastApi && String(name).toLowerCase() === 'jwt') push(value);
      } catch (e) {
        /* never break the page */
      }
      return setHeader.apply(this, arguments);
    };
  }

  // Same for fetch, in case the app uses it somewhere.
  if (typeof w.fetch === 'function') {
    const origFetch = w.fetch;
    w.fetch = function (input, init) {
      try {
        const url = typeof input === 'string' ? input : input && input.url;
        if (isApi(url)) {
          const h = (init && init.headers) || (input && input.headers);
          let v = null;
          if (h && typeof h.get === 'function') v = h.get('jwt');
          else if (Array.isArray(h)) {
            const e = h.find((x) => String(x[0]).toLowerCase() === 'jwt');
            v = e && e[1];
          } else if (h) {
            const k = Object.keys(h).find((x) => x.toLowerCase() === 'jwt');
            v = k && h[k];
          }
          if (v) push(v);
        }
      } catch (e) {
        /* never break the page */
      }
      return origFetch.apply(this, arguments);
    };
  }
})();
