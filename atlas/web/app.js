/* ATLAS — application controller.
 * API + SSE chat streaming, transcript, tool chips, approval cards, panels,
 * markets ticker, voice wiring, settings drawer, engage/boot and setup flows.
 */
(function () {
  'use strict';

  var MD = window.AtlasMD;
  var HUD = window.AtlasHUD;
  var V = window.AtlasVoice;

  // ------------------------------------------------------------------ helpers
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  var esc = MD.escapeHtml;
  function clip(s, n) { s = String(s == null ? '' : s); return s.length > n ? s.slice(0, n - 1) + '…' : s; }
  function num(v) { if (v == null || v === '' || typeof v === 'boolean') return null; var n = Number(v); return isFinite(n) ? n : null; }
  function el(tag, cls, html) { var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function pad2(n) { return String(n).padStart(2, '0'); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function safeUrl(u) { return MD.safeUrl(u); } // http(s) only, parsed, no credentials
  /** Write text only when it changed (cheap to call every second / every event). */
  function setText(sel, v) { var n = typeof sel === 'string' ? $(sel) : sel; v = String(v); if (n && n.textContent !== v) n.textContent = v; }

  var TOKEN = (($('meta[name="atlas-token"]') || {}).content || '').trim();
  var reduceMotion = false;
  try { reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { /* ignore */ }

  var KEYS = {
    history: 'atlas.history.v1',
    transcript: 'atlas.transcript.v1',
    voice: 'atlas.voice.v1',
    news: 'atlas.newsTab.v1'
  };

  // Every storage access is guarded: private modes and blocked storage throw.
  var store = {
    get: function (k, d) {
      try { var v = window.localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; }
    },
    raw: function (k) {
      try { return window.localStorage.getItem(k); } catch (e) { return null; }
    },
    set: function (k, v) {
      try { window.localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; }
    },
    del: function (k) { try { window.localStorage.removeItem(k); } catch (e) { /* ignore */ } }
  };

  // ------------------------------------------------------------------ persisted-state validation
  /* Everything read back from localStorage is untrusted: it may come from an older build, be
   * truncated, hand-edited or wrong-typed. Each entry is checked on its own; anything malformed is
   * dropped (never thrown on), and storage is rewritten with the repaired copy. */
  var MAX_TEXT = 200000;
  var MAX_HISTORY_KEEP = 120;
  var MAX_TRANSCRIPT_KEEP = 150;
  var MAX_SEND = 100;
  function isObj(v) { return v != null && typeof v === 'object' && !Array.isArray(v); }
  function str(v, max) {
    if (typeof v === 'string') return max && v.length > max ? v.slice(0, max) : v;
    if (typeof v === 'number' && isFinite(v)) return String(v);
    return null;
  }
  function tsOf(v) { var n = Number(v); return isFinite(n) && n > 0 && n < 8.64e15 ? n : Date.now(); }
  var ROLES = { user: 1, assistant: 1, tool: 1 };

  function cleanHistoryMsg(m) {
    if (!isObj(m) || !ROLES[m.role]) return null;
    var out = { role: m.role };
    if (typeof m.content === 'string') out.content = m.content.length > MAX_TEXT ? m.content.slice(0, MAX_TEXT) : m.content;
    else if (m.content == null) out.content = null;
    else return null;
    if (m.role === 'user' && typeof out.content !== 'string') return null;
    if (m.role === 'assistant' && m.tool_calls != null) {
      if (!Array.isArray(m.tool_calls)) return null;
      var calls = m.tool_calls.filter(function (c) {
        return isObj(c) && typeof c.id === 'string' && isObj(c.function) && typeof c.function.name === 'string';
      });
      if (calls.length) out.tool_calls = calls.slice(0, 32);
    }
    if (m.role === 'assistant' && !out.tool_calls && !(typeof out.content === 'string' && out.content)) return null;
    if (m.role === 'tool') {
      if (typeof m.tool_call_id !== 'string' || !m.tool_call_id) return null;
      out.tool_call_id = m.tool_call_id;
      if (typeof m.name === 'string') out.name = m.name;
      if (out.content == null) out.content = '';
    }
    return out;
  }
  function trimToUser(list) {
    while (list.length && list[0].role !== 'user') list.shift(); // never start mid tool-exchange
    return list;
  }
  function cleanHistory(arr) {
    if (!Array.isArray(arr)) return [];
    var out = [];
    arr.slice(-MAX_HISTORY_KEEP * 2).forEach(function (m) {
      var c = null;
      try { c = cleanHistoryMsg(m); } catch (e) { c = null; }
      if (c) out.push(c);
    });
    return trimToUser(out.slice(-MAX_HISTORY_KEEP));
  }

  var TOOL_STATES = { run: 1, ok: 1, err: 1 };
  var SYS_TONES = { info: 1, warn: 1, error: 1, ok: 1 };
  function cleanTool(it) {
    if (!isObj(it)) return null;
    return {
      id: str(it.id, 200) || '', name: str(it.name, 120) || '',
      label: str(it.label, 200) || str(it.name, 120) || 'Tool',
      hint: str(it.hint, 200) || '', state: TOOL_STATES[it.state] ? it.state : 'err', summary: str(it.summary, 2000) || ''
    };
  }
  function cleanPart(p) {
    if (!isObj(p)) return null;
    switch (p.t) {
      case 'text': {
        var t = str(p.text, MAX_TEXT);
        return t == null ? null : { t: 'text', text: t };
      }
      case 'tools': {
        if (!Array.isArray(p.items)) return null;
        var items = p.items.map(cleanTool).filter(Boolean).slice(0, 64);
        return items.length ? { t: 'tools', items: items } : null;
      }
      case 'confirm': {
        var fields = Array.isArray(p.fields) ? p.fields.filter(function (f) { return Array.isArray(f) && f.length >= 2; })
          .map(function (f) { return [str(f[0], 200) || '', str(f[1], 4000) || '']; }).slice(0, 40) : [];
        var out = {
          t: 'confirm', id: str(p.id, 200) || '', call_id: str(p.call_id, 200) || '', name: str(p.name, 120) || '',
          title: str(p.title, 400) || '', fields: fields, body: str(p.body, 20000) || '', code: p.code === true,
          result: p.result === true || p.result === false ? p.result : null, expired: p.expired === true
        };
        if (!out.id) return null; // no id: not a card the server ever sent
        if (out.result == null) out.expired = true; // the stream that could have resolved it is gone
        return out;
      }
      case 'error':
      case 'note': {
        var x = str(p.text, 4000);
        return x == null ? null : { t: p.t, text: x };
      }
      default:
        return null;
    }
  }
  function cleanEntry(e) {
    if (!isObj(e)) return null;
    if (e.k === 'u') {
      var text = str(e.text, MAX_TEXT);
      if (text == null) return null;
      return { k: 'u', text: text, ts: tsOf(e.ts), via: e.via === 'voice' || e.via === 'wake' ? e.via : 'text' };
    }
    if (e.k === 's') {
      var st = str(e.text, 4000);
      if (st == null) return null;
      return { k: 's', text: st, tone: SYS_TONES[e.tone] ? e.tone : 'info', tag: str(e.tag, 40) || 'SYSTEM', ts: tsOf(e.ts) };
    }
    if (e.k === 'a') {
      if (!Array.isArray(e.parts)) return null;
      var parts = e.parts.slice(0, 200).map(function (p) {
        try { return cleanPart(p); } catch (x) { return null; }
      }).filter(Boolean);
      parts.forEach(function (p) {
        if (p.t === 'tools') p.items.forEach(function (it) { if (it.state === 'run') { it.state = 'err'; it.summary = it.summary || 'interrupted'; } });
      });
      if (!parts.length) return null; // nothing left worth showing
      return { k: 'a', parts: parts, ts: tsOf(e.ts), model: str(e.model, 200) || '' };
    }
    return null;
  }
  function cleanTranscript(arr) {
    if (!Array.isArray(arr)) return [];
    var out = [];
    arr.slice(-MAX_TRANSCRIPT_KEEP).forEach(function (e) {
      var c = null;
      try { c = cleanEntry(e); } catch (x) { c = null; }
      if (c) out.push(c);
    });
    return out;
  }
  function cleanVoice(v) {
    var d = { voiceURI: '', rate: 1, pitch: 1, speak: true, wake: false };
    if (!isObj(v)) return d;
    var rate = Number(v.rate), pitch = Number(v.pitch);
    return {
      voiceURI: typeof v.voiceURI === 'string' ? v.voiceURI.slice(0, 300) : '',
      rate: isFinite(rate) && rate >= 0.5 && rate <= 2 ? rate : 1,
      pitch: isFinite(pitch) && pitch >= 0 && pitch <= 2 ? pitch : 1,
      speak: typeof v.speak === 'boolean' ? v.speak : true,
      wake: v.wake === true
    };
  }
  function cleanCat(c) { return typeof c === 'string' && /^[a-z][a-z0-9_-]{0,23}$/i.test(c) ? c : 'top'; }

  // ------------------------------------------------------------------ app state
  var S = {
    st: null,
    engaged: false,
    streaming: false,
    abort: null,
    turn: null,
    toolsRunning: 0,
    pendingConfirm: null,
    errorUntil: 0,
    listening: null,
    armed: false,
    speaking: false,
    lastVia: 'text',
    expired: false,
    history: [],
    transcript: [],
    usage: { prompt: 0, completion: 0, cost: 0 },
    turns: 0,
    model: '',
    newsCat: cleanCat(store.get(KEYS.news, 'top')),
    newsCache: {},
    cpuHist: [],
    stick: true,
    voice: cleanVoice(store.get(KEYS.voice, null)),
    sessionStart: Date.now(),
    turnStats: [],   // this page session: [{tokens, ttft, ms}] per finished turn
    lastTtft: null
  };
  // ATLAS's own browser window, as seen from the current turn's tool events (see "web agent" below).
  var web = { running: 0, steps: 0, host: '', ids: {}, last: null, hideT: 0 };

  /** Load, validate and (if anything was dropped or rewritten) repair one stored list. */
  function loadList(key, clean) {
    var raw = store.raw(key);
    if (raw == null) return [];
    var parsed = null;
    try { parsed = JSON.parse(raw); } catch (e) { parsed = null; }
    var out = [];
    try { out = clean(parsed); } catch (e) { out = []; }
    var same = false;
    try { same = JSON.stringify(out) === raw; } catch (e) { same = false; }
    if (!same) { if (out.length) store.set(key, out); else store.del(key); }
    return out;
  }
  S.history = loadList(KEYS.history, cleanHistory);
  S.transcript = loadList(KEYS.transcript, cleanTranscript);
  (function repairSmallKeys() {
    var rawV = store.raw(KEYS.voice);
    if (rawV != null && rawV !== JSON.stringify(S.voice)) store.set(KEYS.voice, S.voice);
    var rawN = store.raw(KEYS.news);
    if (rawN != null && rawN !== JSON.stringify(S.newsCat)) store.set(KEYS.news, S.newsCat);
  })();

  /** Write a list, dropping older entries until it fits the quota. Never throws. */
  function saveList(key, list, steps) {
    if (store.set(key, list)) return list;
    for (var i = 0; i < steps.length; i++) {
      var cut = list.slice(-steps[i]);
      if (key === KEYS.history) trimToUser(cut);
      if (store.set(key, cut)) return cut;
    }
    store.del(key);
    return list;
  }
  function saveHistory() {
    if (S.history.length > MAX_HISTORY_KEEP) S.history = trimToUser(S.history.slice(-MAX_HISTORY_KEEP));
    S.history = saveList(KEYS.history, S.history, [30, 8]);
  }
  function persistedPart(p) {
    if (p.t === 'confirm') {
      return { t: 'confirm', id: p.id, call_id: p.call_id, name: p.name, title: p.title, fields: p.fields, body: p.body, code: p.code, result: p.result, expired: !!p.expired };
    }
    if (p.t === 'tools') return { t: 'tools', items: p.items };
    if (p.t === 'text') return { t: 'text', text: p.text };
    return { t: p.t, text: p.text };
  }
  function saveTranscript() {
    if (S.transcript.length > MAX_TRANSCRIPT_KEEP) S.transcript = S.transcript.slice(-MAX_TRANSCRIPT_KEEP);
    var clean = S.transcript.filter(function (e) { return !e.ephemeral; }).map(function (e) {
      if (e.k !== 'a') return e;
      return { k: 'a', ts: e.ts, model: e.model, parts: e.parts.map(persistedPart) };
    });
    saveList(KEYS.transcript, clean, [30, 8]);
  }
  // Throttled save for the in-progress turn, so a reload mid-stream keeps the partial bubble.
  var saveTranscriptSoon = (function () {
    var timer = 0, last = 0;
    return function () {
      if (timer) return;
      timer = setTimeout(function () { timer = 0; last = Date.now(); saveTranscript(); }, Math.max(0, 1200 - (Date.now() - last)));
    };
  })();
  function saveVoice() { store.set(KEYS.voice, S.voice); }

  /** The outgoing /api/chat payload: the last ~100 messages, starting on a user turn. */
  function outgoingHistory() {
    var h = S.history.slice(-MAX_SEND);
    trimToUser(h);
    return h.length ? h : S.history.slice(-1);
  }

  // ------------------------------------------------------------------ toasts
  var toastBox = $('#toasts');
  var toastKeys = {};
  function toast(msg, opts) {
    opts = opts || {};
    var tone = opts.tone || 'error';
    if (opts.key) {
      var last = toastKeys[opts.key];
      if (last && Date.now() - last < (opts.cooldown || 120000)) return null;
      toastKeys[opts.key] = Date.now();
    }
    var t = el('div', 'toast toast-' + tone);
    t.setAttribute('role', tone === 'error' ? 'alert' : 'status');
    var label = { error: 'FAULT', warn: 'NOTICE', info: 'INFO', ok: 'CONFIRMED' }[tone] || 'INFO';
    t.innerHTML = '<span class="toast-tag">' + label + '</span><span class="toast-msg">' + esc(clip(msg, 300)) + '</span>';
    if (opts.action) {
      var b = el('button', 'toast-act', esc(opts.action.label));
      b.type = 'button';
      b.addEventListener('click', opts.action.run);
      t.appendChild(b);
    }
    var x = el('button', 'toast-x', '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>');
    x.type = 'button';
    x.setAttribute('aria-label', 'Dismiss');
    x.addEventListener('click', function () { dismiss(t); });
    t.appendChild(x);
    toastBox.appendChild(t);
    while (toastBox.children.length > 4) toastBox.removeChild(toastBox.firstChild);
    if (!opts.sticky) setTimeout(function () { dismiss(t); }, opts.ms || (tone === 'error' ? 7000 : 4200));
    return t;
  }
  function dismiss(t) {
    if (!t || !t.parentNode) return;
    t.classList.add('out');
    setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 260);
  }

  function sessionExpired() {
    if (S.expired) return;
    S.expired = true;
    setText('#co-link', 'Session expired');
    var cl = $('#co-link');
    if (cl && cl.parentNode) cl.parentNode.parentNode.classList.add('is-fault');
    toast('Session expired — reload the page', {
      sticky: true, tone: 'error',
      action: { label: 'Reload', run: function () { location.reload(); } }
    });
  }

  // ------------------------------------------------------------------ API
  function api(path, opts) {
    opts = opts || {};
    var headers = { 'X-Atlas-Token': TOKEN, 'Accept': opts.raw ? 'text/event-stream' : 'application/json' };
    var init = { method: opts.method || 'GET', headers: headers, cache: 'no-store', signal: opts.signal };
    if (opts.body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    return fetch(path, init).then(function (res) {
      if (res.status === 401) {
        sessionExpired();
        var e401 = new Error('Session expired — reload the page');
        e401.status = 401;
        throw e401;
      }
      if (opts.raw) {
        if (!res.ok) {
          return res.text().then(function (txt) {
            var msg = 'Request failed (HTTP ' + res.status + ')';
            try { var j = JSON.parse(txt); msg = j.detail || j.error || msg; } catch (e) { /* ignore */ }
            var err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
            err.status = res.status;
            throw err;
          });
        }
        return res;
      }
      return res.text().then(function (txt) {
        var data = null;
        try { data = txt ? JSON.parse(txt) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var msg = (data && (data.detail || data.error)) || ('Request failed (HTTP ' + res.status + ')');
          var err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
          err.status = res.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    }, function (err) {
      if (err && err.name === 'AbortError') throw err;
      var e = new Error('Can’t reach the local server');
      e.network = true;
      throw e;
    });
  }

  // ------------------------------------------------------------------ core + voice
  var coreWrap = $('#core-wrap');
  var core = null;
  try {
    core = new HUD.Core($('#core-canvas'), {
      state: 'standby',
      onGeometry: function (g) { coreWrap.style.setProperty('--core-r', g.R.toFixed(1) + 'px'); layoutCallouts(g); }
    });
    core.start();
  } catch (e) { core = null; }

  /* The corner call-outs (model, link, session, spend) run a hairline leader from their label row to
   * the outer ring; the canvas keeps the band between them free for its tool call-outs. */
  function layoutCallouts(g) {
    var co = $$('.core-co', coreWrap);
    var shown = co.length === 4 && co[0].offsetParent !== null;
    var reserve = shown ? Math.max(co[0].offsetHeight, co[1].offsetHeight) + 8 : 0;
    if (core) core.coReserve = reserve;
    if (!shown) return;
    function lead(y) {
      var dy = Math.abs(y - g.cy), r = g.R + 7;
      var x = dy < r ? Math.sqrt(r * r - dy * dy) : 0;
      return Math.max(0, Math.round(g.cx - x - 4));
    }
    coreWrap.style.setProperty('--co-wt', lead(co[0].offsetTop + 6) + 'px');
    coreWrap.style.setProperty('--co-wb', lead(co[1].offsetTop + 6) + 'px');
  }

  var speaker = new V.Speaker();
  var listener = new V.Listener();
  var mic = new V.MicMeter(HUD.BARS);
  speaker.enabled = !!S.voice.speak;
  speaker.voiceURI = S.voice.voiceURI || '';
  speaker.rate = num(S.voice.rate) || 1;
  speaker.pitch = num(S.voice.pitch) || 1;

  speaker.onstate = function (on) {
    S.speaking = on;
    if (on) listener.pause();
    else {
      listener.resume();
      if (S.awaitVoiceAnswer && S.pendingConfirm && listener.supported) {
        S.awaitVoiceAnswer = false;
        setTimeout(function () { if (S.pendingConfirm && !S.listening) startListening(); }, 250);
      }
    }
    refreshState();
  };
  speaker.onboundary = function () { if (core) core.pulse(0.8); };
  speaker.onvoices = function () { fillVoices(); updateVoiceReadouts(); };

  listener.oninterim = function (txt) { var inp = $('#cmd-input'); inp.value = txt; inp.classList.add('interim'); };
  listener.onstate = function (mode) {
    S.listening = mode;
    if (!mode) { S.armed = false; $('#cmd-input').classList.remove('interim'); }
    if (mode === 'command') startMic(); else stopMic();
    refreshState();
  };
  // The core pulls mic levels inside its own animation frame while the mic is live; nothing runs
  // when the mic is off, and the core itself sleeps while the tab is hidden.
  function micLevels() { return mic.on ? mic.levels() : null; }
  function startMic() { mic.start(); if (core) core.setInputSource(micLevels); }
  function stopMic() { mic.stop(); if (core) core.setInputSource(null); }
  listener.onwake = function () {
    S.armed = true;
    V.chime('wake');
    if (core) core.pulse(1);
    refreshState();
    setTimeout(function () { S.armed = false; refreshState(); }, 8000);
  };
  listener.onheard = function (text) {
    if (S.pendingConfirm) {
      // Every phrase is consumed while a card waits; it never becomes a new request.
      var ans = approvalFromPhrase(text);
      if (ans === true) return true;
      if (isStrict(S.pendingConfirm.name)) strictNudge('voice');
      else toast('Say “yes” to approve or “no” to deny.', { tone: 'info', key: 'say-yn', cooldown: 5000 });
      return true;
    }
    if (S.streaming && /^(stop|cancel|abort|enough|be quiet|silence|never ?mind)\b/i.test(text)) { stopAll(); return true; }
    return false;
  };
  listener.onfinal = function (text, via) {
    var inp = $('#cmd-input');
    inp.classList.remove('interim');
    inp.value = '';
    S.armed = false;
    sendMessage(text, via);
  };
  listener.onerror = function (err) {
    if (err === 'not-allowed' || err === 'service-not-allowed') {
      toast('Microphone access was blocked. Allow it in the browser to use voice.', { key: 'mic-denied' });
      if (S.voice.wake) { S.voice.wake = false; saveVoice(); var wt = $('#wake-toggle'); if (wt) wt.checked = false; }
    } else if (err === 'network') {
      toast('Speech recognition needs a network connection in this browser.', { tone: 'warn', key: 'sr-net' });
    } else if (err === 'audio-capture') {
      toast('No microphone was found.', { tone: 'warn', key: 'sr-audio' });
    }
  };

  function startListening() {
    if (!listener.supported) {
      toast('Voice input isn’t supported in this browser — try Chrome or Edge. You can still type.', { tone: 'info', key: 'sr-unsupported' });
      return;
    }
    if (S.speaking || speaker.busy()) stopSpeaking();
    V.unlockAudio();
    listener.start('command');
  }
  function toggleListening() {
    if (S.listening === 'command') listener.stop();
    else startListening();
  }

  // ------------------------------------------------------------------ state display
  var CORE_TEXT = { 'AWAITING AUTH': 'AUTH REQ', 'STANDBY · WAKE': 'WAKE ARMED', 'PROCESSING': 'THINKING' };
  var STATUS_TEXT = { standby: 'STANDBY', listening: 'LISTENING', thinking: 'PROCESSING', speaking: 'SPEAKING', error: 'FAULT' };
  function computeState() {
    if (Date.now() < S.errorUntil) return 'error';
    if (S.listening === 'command' || S.armed) return 'listening';
    if (S.speaking) return 'speaking';
    if (S.streaming) return 'thinking';
    return 'standby';
  }
  function refreshState() {
    var st = computeState();
    document.body.dataset.state = st;
    if (core) core.setState(st);
    var label = STATUS_TEXT[st];
    if (st === 'thinking') label = S.pendingConfirm ? 'AWAITING AUTH' : web.running > 0 ? 'BROWSING' : S.toolsRunning > 0 ? 'EXECUTING' : 'PROCESSING';
    if (st === 'standby' && S.listening === 'wake') label = 'STANDBY · WAKE';
    $('#status-text').textContent = label;
    $('#status-pill').title = 'Status: ' + label.toLowerCase();
    $('#core-state').textContent = CORE_TEXT[label] || label;
    var micBtn = $('#mic-btn');
    var on = S.listening === 'command';
    micBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    micBtn.setAttribute('aria-label', on ? 'Stop voice input' : 'Start voice input');
    micBtn.classList.toggle('on', on);
    micBtn.classList.toggle('passive', S.listening === 'wake');
    document.body.classList.toggle('is-streaming', S.streaming);
    document.body.classList.toggle('has-pending', !!S.pendingConfirm);
    var send = $('#send-btn');
    send.setAttribute('aria-label', S.streaming ? 'Stop response' : 'Send');
    send.classList.toggle('stop', S.streaming);
  }
  function errorFlash() {
    S.errorUntil = Date.now() + 2600;
    refreshState();
    setTimeout(refreshState, 2700);
  }

  // ------------------------------------------------------------------ names / state
  function assistantName() { return (S.st && S.st.assistant_name) || 'ATLAS'; }
  function userAddress() { return (S.st && (S.st.address_as || S.st.user_name)) || ''; }

  function applyState(st) {
    S.st = st || S.st || {};
    var name = assistantName();
    document.title = name;
    $('#brand-name').textContent = name.toUpperCase();
    var cn = $('#core-name');
    cn.textContent = name.toUpperCase();
    cn.style.setProperty('--name-scale', String(Math.min(1, 6 / Math.max(3, name.length))));
    $$('[data-name]').forEach(function (n) { n.textContent = name; });
    listener.wakeName = name;
    if (!S.model) setModel(S.st.model || '');
    buildNewsTabs(S.st.news_categories);
    var host = location.host || '127.0.0.1';
    $('#eg-host').textContent = host;
    $('#eg-token').textContent = TOKEN && TOKEN.indexOf('{{') === -1 ? 'VERIFIED' : 'MISSING';
  }

  function loadState() {
    return api('/api/state').then(function (st) {
      applyState(st);
      return st;
    }).catch(function (e) {
      if (e.status !== 401) {
        toast('Can’t reach ' + assistantName() + '’s server: ' + e.message, { key: 'state' });
        setTimeout(loadState, 5000);
      }
      return null;
    });
  }

  // ------------------------------------------------------------------ telemetry
  function shortModel(m) { if (!m) return 'AUTO'; var s = String(m); var i = s.indexOf('/'); return i >= 0 ? s.slice(i + 1) : s; }
  function fmtTokens(n) { n = n || 0; return n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e4 ? (n / 1e3).toFixed(1) + 'K' : n.toLocaleString(); }
  function fmtMs(ms) { var n = num(ms); return n == null ? '—' : n >= 10000 ? (n / 1000).toFixed(1) + ' s' : n >= 1000 ? (n / 1000).toFixed(2) + ' s' : Math.round(n) + ' ms'; }
  function setModel(m) {
    S.model = m || '';
    var cm = $('#co-model');
    setText(cm, S.model ? shortModel(S.model) : 'Automatic');
    if (cm) cm.title = S.model || 'Automatic';
    setText('#s-model', S.model || 'Automatic');
  }
  function renderUsage() {
    var total = S.usage.prompt + S.usage.completion;
    setText('#co-tokens', fmtTokens(total) + ' tokens');
    setText('#co-tokens-s', S.turns + (S.turns === 1 ? ' turn' : ' turns'));
    setText('#co-spend', '$' + S.usage.cost.toFixed(4));
    setText('#s-prompt', fmtTokens(S.usage.prompt));
    setText('#s-completion', fmtTokens(S.usage.completion));
    setText('#s-cost', '$' + S.usage.cost.toFixed(4));
    setText('#s-turns', String(S.turns));
    renderSession();
  }
  /** Session panel: first-token latency and a tokens-per-turn bar chart (this page session). */
  var SC_MAX = 16;
  function renderSession() {
    var list = S.turnStats;
    var last = list[list.length - 1] || null;
    var lat = list.filter(function (x) { return num(x.ttft) != null; });
    var ttft = S.lastTtft != null ? S.lastTtft : last ? last.ttft : null;
    setText('#s-ttft', fmtMs(ttft));
    setText('#s-ttft-avg', lat.length ? fmtMs(lat.reduce(function (a, x) { return a + x.ttft; }, 0) / lat.length) : '—');
    setText('#s-last', last ? fmtMs(last.ms) : '—');
    setText('#co-model-s', 'First token ' + fmtMs(ttft));
    var withTok = list.filter(function (x) { return x.tokens > 0; }).slice(-SC_MAX);
    setText('#s-tpt', withTok.length ? fmtTokens(withTok[withTok.length - 1].tokens) + ' last' : '—');
    var g = $('#s-chart .sc-bars');
    if (!g) return;
    var max = withTok.reduce(function (a, x) { return Math.max(a, x.tokens); }, 0) || 1;
    var bw = 160 / SC_MAX;
    var html = '';
    withTok.forEach(function (x, i) {
      var h = Math.max(1.5, x.tokens / max * 44);
      var xx = (SC_MAX - withTok.length + i) * bw;
      html += '<rect class="' + (i === withTok.length - 1 ? 'sc-now' : 'sc-bar') + '" x="' + (xx + bw * 0.18).toFixed(2) + '" y="' + (48 - h).toFixed(2) + '" width="' + (bw * 0.64).toFixed(2) + '" height="' + h.toFixed(2) + '"/>';
    });
    if (g.innerHTML !== html) g.innerHTML = html;
    var empty = $('#s-chart-empty');
    if (empty) empty.hidden = withTok.length > 0;
    var plot = $('#s-chart');
    if (plot) plot.setAttribute('aria-label', withTok.length ? 'Tokens per turn, last ' + withTok.length + ' turns: ' + withTok.map(function (x) { return fmtTokens(x.tokens); }).join(', ') : 'Tokens per turn: no turns yet');
  }
  function updateVoiceReadouts() {
    var micTxt = listener.supported ? (S.voice.wake ? 'wake word' : 'push-to-talk') : 'text only';
    document.body.classList.toggle('no-sr', !listener.supported);
    setText('#co-link-s', 'Mic · ' + micTxt);
    var v = speaker.supported ? speaker.currentVoice() : null;
    var outTxt = !speaker.supported ? 'unavailable' : !S.voice.speak ? 'muted' : v ? clip(v.name.replace(/^(Microsoft|Google)\s+/i, ''), 24) : 'system';
    setText('#co-spend-s', 'Voice · ' + outTxt);
    setText('#s-mic', micTxt);
    setText('#s-voice', outTxt);
    var mb = $('#mute-btn');
    if (mb) {
      var muted = !S.voice.speak || !speaker.supported;
      mb.setAttribute('aria-pressed', muted ? 'true' : 'false');
      mb.classList.toggle('is-muted', muted);
      mb.title = !speaker.supported ? 'Speech output isn’t available in this browser' : muted ? 'Spoken replies are muted — unmute' : 'Spoken replies are on — mute';
      if (!speaker.supported) mb.setAttribute('aria-disabled', 'true');
    }
    var micBtn = $('#mic-btn');
    if (!listener.supported) {
      micBtn.setAttribute('aria-disabled', 'true');
      micBtn.title = 'Voice input isn’t supported in this browser';
      micBtn.classList.add('unsupported');
    }
  }

  // ------------------------------------------------------------------ transcript
  var tx = $('#transcript');
  var jumpBtn = $('#jump-latest');
  var srLive = $('#sr-live');
  var partEls = new WeakMap();
  var renderQueue = new Set();
  var frameReq = 0;
  var wantScroll = 0;      // 0 = none, 1 = follow if pinned, 2 = force to bottom
  var wantCard = null;     // an approval card taller than the view: show its top (title, fields) first
  var scrollDirty = false; // the user scrolled: re-measure at the start of the next frame

  function fmtClock(ts) {
    var d = new Date(ts || Date.now());
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function nearBottom() { return tx.scrollHeight - tx.scrollTop - tx.clientHeight < 48; }
  function setJump(show) { if (jumpBtn.hidden === show) jumpBtn.hidden = !show; }

  /* One frame does all transcript work: measure first (layout is still clean), then write the DOM,
   * then scroll once. Nothing here reads layout between writes, so there is no forced reflow. */
  function scheduleFrame() { if (!frameReq) frameReq = requestAnimationFrame(runFrame); }
  function runFrame() {
    frameReq = 0;
    if (scrollDirty) {
      scrollDirty = false;
      S.stick = nearBottom();
      if (S.stick) setJump(false);
    }
    if (renderQueue.size) {
      renderQueue.forEach(paintText);
      renderQueue.clear();
    }
    if (wantCard) {
      var card = wantCard;
      wantCard = null;
      if (card.isConnected) {
        var cr = card.getBoundingClientRect(), tr = tx.getBoundingClientRect();
        if (cr.height > tr.height - 40) {
          wantScroll = 0;
          tx.scrollTop += cr.top - tr.top - 26;
          S.stick = false;
          return;
        }
      }
    }
    if (wantScroll) {
      var force = wantScroll === 2;
      wantScroll = 0;
      if (force) S.stick = true;
      if (S.stick) { tx.scrollTop = tx.scrollHeight; setJump(false); }
      else if (S.streaming) setJump(true);
    }
  }
  function scrollToEnd(force) {
    wantScroll = Math.max(wantScroll, force ? 2 : 1);
    scheduleFrame();
  }
  tx.addEventListener('scroll', function () { scrollDirty = true; scheduleFrame(); }, { passive: true });
  jumpBtn.addEventListener('click', function () { scrollToEnd(true); });
  // Re-pin to the bottom when the transcript is resized (window resize, core shrinking, phone keyboard).
  if (typeof ResizeObserver === 'function') {
    try { new ResizeObserver(function () { if (S.stick) scrollToEnd(false); }).observe(tx); } catch (e) { /* ignore */ }
  }

  /** Announce a finished reply once (the log itself is aria-live=off so streaming isn't read out). */
  var announceT = 0;
  function announce(text) {
    if (!srLive) return;
    var t = clip(String(text || '').replace(/\s+/g, ' ').trim(), 600);
    if (!t) return;
    clearTimeout(announceT);
    srLive.textContent = '';
    announceT = setTimeout(function () { srLive.textContent = t; }, 80);
  }

  /* Streaming text is painted incrementally: blocks that can no longer change (everything before
   * the last blank line outside a code fence) are rendered once; only the tail is rebuilt. */
  function paintText(p) {
    var node = partEls.get(p);
    if (!node) return;
    var text = p.text || '';
    var st = node._md || (node._md = { stable: 0, tail: [] });
    for (var i = 0; i < st.tail.length; i++) { if (st.tail[i].parentNode === node) node.removeChild(st.tail[i]); }
    st.tail = [];
    var cut = MD.stableCut(text);
    if (cut < st.stable) { node.textContent = ''; st.stable = 0; }
    if (cut > st.stable) { node.appendChild(MD.fragment(text.slice(st.stable, cut))); st.stable = cut; }
    var frag = MD.fragment(text.slice(st.stable));
    st.tail = Array.prototype.slice.call(frag.childNodes);
    node.appendChild(frag);
  }

  function setConvoMode() {
    var has = S.transcript.some(function (e) { return e.k === 'u' || e.k === 'a'; });
    document.body.classList.toggle('has-convo', has);
    // The greeting hero stands in for an empty transcript; any line (a reminder, say) replaces it.
    $('#transcript-empty').hidden = has || S.transcript.length > 0;
  }
  function partOfDay(h) { return h < 5 ? 'evening' : h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening'; }
  function paintHero(now) {
    setText('#hero-greet', 'Good ' + partOfDay(now.getHours()));
    var who = userAddress();
    setText('#hero-name', who ? ', ' + who : '');
    setText('#hero-date', now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' }));
  }

  function renderUser(entry) {
    var who = (S.st && S.st.user_name) || 'You';
    var a = el('article', 'msg msg-u');
    a.innerHTML = '<header class="msg-head"><span class="msg-time">' + fmtClock(entry.ts) + '</span>' +
      (entry.via && entry.via !== 'text' ? '<span class="msg-via">' + (entry.via === 'wake' ? 'WAKE' : 'VOICE') + '</span>' : '') +
      '<span class="msg-who">' + esc(who.toUpperCase()) + '</span></header>' +
      '<div class="msg-body"><p>' + esc(entry.text).replace(/\n/g, '<br>') + '</p></div>';
    return a;
  }

  function renderSysLine(entry) {
    var d = el('div', 'msg-sys tone-' + (entry.tone || 'info'));
    d.innerHTML = '<span class="sys-line" aria-hidden="true"></span><span class="sys-txt"><b>' + esc(entry.tag || 'SYSTEM') + '</b> ' + esc(entry.text) +
      '<time>' + fmtClock(entry.ts) + '</time></span><span class="sys-line" aria-hidden="true"></span>';
    return d;
  }

  function renderAssistantShell(entry) {
    var a = el('article', 'msg msg-a');
    a.innerHTML = '<header class="msg-head"><span class="msg-glyph" aria-hidden="true"></span><span class="msg-who">' + esc(assistantName().toUpperCase()) + '</span>' +
      '<span class="msg-time">' + fmtClock(entry.ts) + '</span><span class="msg-model">' + esc(shortModel(entry.model || '')) + '</span></header>' +
      '<div class="msg-body"></div>';
    partEls.set(entry, a);
    var body = a.querySelector('.msg-body');
    (entry.parts || []).forEach(function (p) { body.appendChild(renderPart(p, entry)); });
    return a;
  }

  function renderPart(p, entry) {
    var node;
    if (p.t === 'text') {
      node = el('div', 'md');
      MD.renderInto(node, p.text || '');
      node._md = { stable: 0, tail: Array.prototype.slice.call(node.childNodes) };
    } else if (p.t === 'tools') {
      node = el('div', 'tools');
      (p.items || []).forEach(function (it) { node.appendChild(renderTool(it)); });
    } else if (p.t === 'confirm') {
      node = renderConfirm(p, entry);
    } else if (p.t === 'error') {
      node = el('div', 'msg-error', '<span class="err-tag">FAULT</span> ' + esc(p.text || 'Unknown error'));
    } else if (p.t === 'note') {
      node = el('div', 'msg-note', esc(p.text || ''));
    } else {
      node = el('div');
    }
    partEls.set(p, node);
    return node;
  }

  function toolHint(args) {
    if (!args || typeof args !== 'object') return '';
    var keys = ['query', 'q', 'topic', 'category', 'location', 'city', 'symbols', 'symbol', 'expression', 'command', 'to', 'subject',
      'title', 'app', 'name', 'url', 'path', 'message', 'fact', 'action', 'keys', 'text', 'question', 'entity_id', 'service',
      'destination', 'key', 'option', 'direction'];
    for (var i = 0; i < keys.length; i++) {
      var v = args[keys[i]];
      if (v == null || v === '') continue;
      if (Array.isArray(v)) v = v.join(', ');
      if (typeof v === 'object') continue;
      if (keys[i] === 'url') v = hostOf(v) || v;
      return clip(String(v).replace(/\s+/g, ' '), 42);
    }
    return '';
  }
  /** "www.opentable.com/r/x" → "opentable.com"; '' for anything that isn't a web address. */
  function hostOf(u) {
    var s = String(u || '').trim();
    if (!s) return '';
    if (!/^[a-z][a-z0-9+.-]*:/i.test(s)) s = 'https://' + s;
    try {
      var h = new URL(s).hostname;
      return /^[a-z0-9.-]+$/i.test(h) ? h.replace(/^www\./i, '') : '';
    } catch (e) { return ''; }
  }

  function renderTool(it) {
    var c = el('span', 'tool');
    c.dataset.state = it.state || 'run';
    c.dataset.id = it.id || '';
    var hint = it.hint ? '<span class="tool-hint">' + esc(it.hint) + '</span>' : '';
    c.innerHTML = '<i class="tool-ico" aria-hidden="true"></i><span class="tool-label">' + esc(it.label || it.name || 'Tool') + '</span>' + hint +
      '<span class="tool-sum">' + esc(it.summary || '') + '</span>';
    // No role=status here: each chip would be its own live region and chatter during a briefing.
    c.title = (it.label || it.name || '') + (it.hint ? ' — ' + it.hint : '') + (it.summary ? ' · ' + it.summary : '');
    return c;
  }

  // ------------------------------------------------------------------ web agent indicator
  /* While ATLAS drives its own browser window (browser_* tools, reservation_search) a small live
   * readout sits on the core: the current action, the site and the step count. It stays up, dimmed,
   * between steps and fades a moment after the turn ends. */
  var waEl = $('#web-agent');
  function isWebTool(name) { name = String(name || ''); return /^browser_/.test(name) || name === 'reservation_search'; }
  function webDetail(name, args) {
    var a = isObj(args) ? args : {};
    var v = '';
    if (name === 'reservation_search') v = [a.query, a.party_size ? 'party of ' + a.party_size : ''].filter(function (x) { return x && typeof x !== 'object'; }).join(' · ');
    else if (name === 'browser_type') v = typeof a.text === 'string' && a.text ? '“' + clip(a.text, 24) + '”' : '';
    else if (name === 'browser_fill') v = Array.isArray(a.fields) ? a.fields.length + (a.fields.length === 1 ? ' field' : ' fields') : '';
    else if (name === 'browser_select') v = a.option;
    else if (name === 'browser_press') v = a.key;
    else if (name === 'browser_scroll') v = a.direction;
    else if (name === 'browser_look') v = a.question;
    return v != null && typeof v !== 'object' ? clip(String(v).replace(/\s+/g, ' '), 38) : '';
  }
  /* Tool results and panel refreshes can arrive while a card still waits; a pending website card
   * keeps the readout on "awaiting approval" until it resolves. */
  function webCardPending() { return !!(S.pendingConfirm && S.pendingConfirm.result == null && !S.pendingConfirm.expired && confirmKind(S.pendingConfirm.name) === 'web'); }
  function webPaint(state) {
    if (!waEl) return;
    if (state !== 'err' && webCardPending()) state = 'auth';
    clearTimeout(web.hideT);
    var last = web.last || { action: 'Working', detail: '' };
    waEl.dataset.state = state;
    var act = state === 'auth' ? 'Awaiting your approval' : last.action + (last.detail ? ' · ' + last.detail : '');
    $('#wa-act').textContent = act;
    $('#wa-host').textContent = web.host || '';
    $('#wa-step').textContent = web.steps ? 'STEP ' + pad2(web.steps) : '';
    if (waEl.hidden) { waEl.hidden = false; waEl.classList.remove('out'); }
    document.body.classList.add('web-active');
  }
  function webStart(it, args) {
    web.running++;
    web.steps++;
    web.ids[it.id] = true;
    if (it.name === 'browser_open') { var h = hostOf(isObj(args) ? args.url : ''); if (h) web.host = h; }
    web.last = { action: it.label || 'Working', detail: webDetail(it.name, args) };
    webPaint('run');
  }
  function webFinish(id, ok) {
    if (!web.ids[id]) return;
    delete web.ids[id];
    web.running = Math.max(0, web.running - 1);
    if (!web.running) webPaint(ok ? 'ok' : 'err');
  }
  function webAwait(p) {
    var site = hostOf(cardField(p.fields || [], 'site'));
    if (site) web.host = site;
    webPaint('auth');
  }
  function webResolved(approved) { if (waEl && !waEl.hidden && waEl.dataset.state === 'auth') webPaint(approved ? 'run' : 'err'); }
  function webEnd() {
    web.running = 0; web.ids = {}; web.steps = 0; web.host = ''; web.last = null;
    document.body.classList.remove('web-active');
    if (!waEl || waEl.hidden) return;
    if (waEl.dataset.state === 'run' || waEl.dataset.state === 'auth') waEl.dataset.state = 'ok';
    clearTimeout(web.hideT);
    web.hideT = setTimeout(function () {
      waEl.classList.add('out');
      web.hideT = setTimeout(function () { waEl.hidden = true; waEl.classList.remove('out'); }, reduceMotion ? 0 : 400);
    }, 2600);
  }

  var CONFIRM_MS = 300000; // the server waits 300 s for a decision (agent.CONFIRM_TIMEOUT)
  var confirmTimer = 0;
  function fmtLeft(ms) { var sec = Math.ceil(ms / 1000); return Math.floor(sec / 60) + ':' + pad2(sec % 60); }
  function isTouch() { try { return window.matchMedia('(pointer: coarse)').matches; } catch (e) { return false; } }

  function tickConfirm() {
    var p = S.pendingConfirm;
    if (!p || p.result != null || p.expired) { clearInterval(confirmTimer); confirmTimer = 0; return; }
    var left = Math.max(0, (p.deadline || 0) - Date.now());
    var card = partEls.get(p);
    if (card && card._timer) {
      card._timer.bar.style.transform = 'scaleX(' + (left / CONFIRM_MS).toFixed(4) + ')';
      card._timer.label.textContent = fmtLeft(left);
      card.classList.toggle('urgent', left <= 30000);
    }
    if (!left && !p.sending) {
      lockConfirm(p, 'expired');
      refreshState();
      saveTranscript();
    }
  }
  function startConfirmTimer() {
    clearInterval(confirmTimer);
    confirmTimer = setInterval(tickConfirm, 1000);
    tickConfirm();
  }

  var ICON_OK = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg>';
  var ICON_NO = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';

  /* Approvals for actions that leave the computer get their own header and body layout:
   * website clicks show the page excerpt, texts show the message as it will be sent. */
  var CONFIRM_KINDS = {
    web: { head: 'Confirm on website', aria: 'Confirm on website',
      icon: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.3 3.6 5.1 3.6 8.5s-1.2 6.2-3.6 8.5c-2.4-2.3-3.6-5.1-3.6-8.5s1.2-6.2 3.6-8.5z"/></svg>' },
    msg: { head: 'Confirm message', aria: 'Confirm message',
      icon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z"/><path d="M8 9.5h8M8 12.5h5"/></svg>' },
    call: { head: 'Confirm call', aria: 'Confirm phone call',
      icon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a1 1 0 0 1-1 1A16 16 0 0 1 4 5a1 1 0 0 1 1-1z"/></svg>' },
    home: { head: 'Confirm home action', aria: 'Confirm home action',
      icon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 11l8-6.5 8 6.5"/><path d="M6.5 9.5V20h11V9.5"/><path d="M10 20v-5h4v5"/></svg>' }
  };
  function confirmKind(name) {
    name = String(name || '');
    if (/^browser_/.test(name)) return 'web';
    if (name === 'send_text_message') return 'msg';
    if (name === 'make_phone_call') return 'call';
    if (name === 'smart_home_control') return 'home';
    return '';
  }
  /* High-risk cards need a deliberate click or key press. Spoken and typed yes/no are off for them:
   * audio ATLAS plays itself (a video opened with play_media, a page read aloud) or text its own
   * keyboard tools type must never be able to answer "yes". The list covers code, files, messages,
   * calls, keystrokes, website clicks and the home, plus the gated launchers in pc.py (unknown apps,
   * programs/scripts/network paths opened by path, network-folder searches). */
  var STRICT_TOOLS = {
    run_command: 1, power_action: 1, write_file: 1, read_file: 1, send_email: 1, send_text_message: 1, make_phone_call: 1,
    type_text: 1, press_keys: 1, close_application: 1, smart_home_control: 1, open_application: 1, open_path: 1, find_files: 1
  };
  function isStrict(name) { name = String(name || ''); return STRICT_TOOLS[name] === 1 || /^browser_/.test(name); }
  var STRICT_ARM_MS = 600; // Approve / Deny wake up a moment after the card appears (no stray click or key lands on them)
  function strictHelp() { return isTouch() ? 'tap Approve or Deny' : 'click Approve or press Y (Deny: N)'; }
  function cardField(fields, label) {
    for (var i = 0; i < fields.length; i++) if (String(fields[i][0]).toLowerCase() === label) return String(fields[i][1] || '');
    return '';
  }
  /** A website card's body ("Filled in:\n• …\n\nOn the page:\n…") as titled, readable sections. */
  function pageBodyHtml(body) {
    // A paragraph whose first line is a short "Label:" starts a section; other paragraphs join the one before.
    var secs = [];
    String(body).split(/\n{2,}/).forEach(function (b) {
      var lines = b.replace(/^\n+|\n+$/g, '').split('\n');
      if (!lines.join('').trim()) return;
      if (lines.length > 1 && /^[^:\n]{1,40}:$/.test(lines[0].trim())) secs.push({ head: lines.shift().trim().slice(0, -1), text: lines.join('\n') });
      else if (secs.length) secs[secs.length - 1].text += '\n\n' + lines.join('\n');
      else secs.push({ head: '', text: lines.join('\n') });
    });
    return '<div class="confirm-page" tabindex="0" role="region" aria-label="Page details">' + secs.map(function (x) {
      return '<section class="cp-sec">' + (x.head ? '<h5 class="cp-h">' + esc(x.head) + '</h5>' : '') + '<div class="cp-txt">' + esc(x.text) + '</div></section>';
    }).join('') + '</div>';
  }

  function renderConfirm(p) {
    var card = el('div', 'confirm');
    var kind = confirmKind(p.name);
    var K = CONFIRM_KINDS[kind];
    if (kind) card.dataset.kind = kind;
    card.dataset.state = p.result === true ? 'approved' : p.result === false ? 'denied' : p.expired ? 'expired' : 'pending';
    card.setAttribute('role', 'group');
    card.setAttribute('aria-label', (K ? K.aria : 'Approval required') + ': ' + String(p.title || 'action'));
    var fields = (Array.isArray(p.fields) ? p.fields : []).filter(function (f) { return Array.isArray(f) && f.length >= 2; });
    var grid = fields.length ? '<dl class="confirm-fields">' + fields.map(function (f) {
      return '<div><dt>' + esc(f[0]) + '</dt><dd>' + esc(f[1]) + '</dd></div>';
    }).join('') + '</dl>' : '';
    var body = '';
    if (p.body && kind === 'web') body = pageBodyHtml(p.body);
    else if (p.body && kind === 'msg') {
      var n = Array.from(String(p.body)).length;
      body = '<figure class="confirm-msg"><figcaption>Message · ' + n + ' character' + (n === 1 ? '' : 's') + '</figcaption>' +
        '<p class="cm-bubble">' + esc(p.body) + '</p></figure>';
    } else if (p.body) body = '<pre class="confirm-body' + (p.code ? ' code' : '') + '" tabindex="0">' + esc(p.body) + '</pre>';
    var site = kind === 'web' ? hostOf(cardField(fields, 'site')) || clip(cardField(fields, 'site'), 60) : '';
    var head = K
      ? '<span class="confirm-idx">' + K.icon + '<span>' + esc(K.head) + '</span>' + (site ? '<i class="confirm-site">' + esc(site) + '</i>' : '') + '</span>'
      : '<span class="confirm-idx">AUTH // ' + esc(String(p.name || 'action').toUpperCase()) + '</span>';
    var strict = isStrict(p.name);
    if (strict) card.dataset.strict = 'true';
    var hintId = 'ch-' + Math.random().toString(36).slice(2, 10);
    var hint = strict
      ? '<span class="confirm-hint strict" id="' + hintId + '"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5.5" y="10.5" width="13" height="9.5" rx="1"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/></svg>' +
        '<span><b>' + (isTouch() ? 'Tap Approve' : 'Click Approve or press Y') + '</b><small>Voice &amp; typed answers are off for this action</small></span></span>'
      : '<span class="confirm-hint" id="' + hintId + '">' + esc(listener.supported ? 'or say “yes” / “no”' : 'or type yes / no') + '</span>';
    card.innerHTML = '<i class="confirm-haz" aria-hidden="true"></i>' +
      '<div class="confirm-head">' + head +
      '<span class="confirm-flag" tabindex="-1" aria-live="polite"></span></div>' +
      '<h4 class="confirm-title">' + esc(p.title || 'Approve this action?') + '</h4>' + grid + body +
      '<div class="confirm-timer" aria-hidden="true"><span class="ct-track"><i class="ct-bar"></i></span><b class="ct-left">' + fmtLeft(CONFIRM_MS) + '</b></div>' +
      '<div class="confirm-actions">' +
      '<button type="button" class="btn btn-primary btn-sm" data-act="approve" aria-keyshortcuts="y" aria-describedby="' + hintId + '">' + ICON_OK + '<span>Approve</span><kbd aria-hidden="true">Y</kbd></button>' +
      '<button type="button" class="btn btn-danger btn-sm" data-act="deny" aria-keyshortcuts="n">' + ICON_NO + '<span>Deny</span><kbd aria-hidden="true">N</kbd></button>' +
      hint + '</div>' +
      '<div class="confirm-stamp" aria-hidden="true"></div>';
    var flag = card.querySelector('.confirm-flag');
    var stamp = card.querySelector('.confirm-stamp');
    card._timer = { bar: card.querySelector('.ct-bar'), label: card.querySelector('.ct-left') };
    function paint() {
      var st = card.dataset.state;
      flag.textContent = st === 'pending' ? (p.sending ? 'TRANSMITTING…' : 'AWAITING APPROVAL') : st === 'approved' ? 'APPROVED' : st === 'denied' ? 'DENIED' : 'EXPIRED';
      stamp.textContent = st === 'pending' ? '' : flag.textContent;
      $$('button', card).forEach(function (b) { b.disabled = st !== 'pending' || !!p.sending || !!p.arming; });
    }
    if (strict && card.dataset.state === 'pending' && p.result == null) {
      p.arming = true;
      setTimeout(function () { p.arming = false; paint(); }, STRICT_ARM_MS);
    }
    paint();
    card._paint = paint;
    $$('button[data-act]', card).forEach(function (b) {
      b.addEventListener('click', function () { resolveConfirm(p, b.dataset.act === 'approve'); });
    });
    return card;
  }

  /** After a card resolves, focus goes to the command bar (or the card's status on touch), never <body>. */
  function refocusAfterCard(card) {
    var a = document.activeElement;
    if (a && a !== document.body && !card.contains(a)) return;
    if (!S.engaged || isDrawerOpen()) return;
    var flag = card.querySelector('.confirm-flag');
    if (isTouch() && flag) { try { flag.focus({ preventScroll: true }); } catch (e) { /* ignore */ } return; }
    try { $('#cmd-input').focus({ preventScroll: true }); } catch (e) { /* ignore */ }
  }

  function lockConfirm(p, state) {
    if (p.result != null || p.expired) return false;
    if (state === 'approved') p.result = true;
    else if (state === 'denied') p.result = false;
    else p.expired = true;
    p.sending = false;
    var card = partEls.get(p);
    if (card) {
      card.dataset.state = state;
      card.classList.remove('urgent');
      if (card._paint) card._paint();
      refocusAfterCard(card);
      if (state !== 'expired') scrollToEnd(true);
    }
    if (S.pendingConfirm === p) S.pendingConfirm = null;
    tickConfirm();
    return true;
  }

  function resolveConfirm(p, approved) {
    if (!p || p.result != null || p.expired || p.sending || p.arming) return;
    p.sending = true;
    var card = partEls.get(p);
    if (card) {
      // Disabling the focused button would drop focus to <body>; park it on the card's status first.
      var flag = card.querySelector('.confirm-flag');
      if (card.contains(document.activeElement) && flag) { try { flag.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
      if (card._paint) card._paint();
    }
    api('/api/confirm/' + encodeURIComponent(p.id), { method: 'POST', body: { approved: !!approved } })
      .then(function () {
        lockConfirm(p, approved ? 'approved' : 'denied');
        V.chime(approved ? 'ok' : 'error');
      })
      .catch(function (e) {
        if (e.status === 404) { lockConfirm(p, 'expired'); toast('That approval request has expired.', { tone: 'warn' }); }
        else if (e.status !== 401) {
          p.sending = false;
          if (card && card._paint) card._paint();
          toast('Couldn’t send your decision: ' + e.message);
        }
      })
      .then(function () {
        p.sending = false;
        refreshState();
        saveTranscript();
      });
  }

  /* Typed or spoken answers must be a whole yes/no phrase ("yes", "go ahead", "no thanks"), so a
   * sentence that merely contains "ok" or "do it" can never approve a command by accident. */
  var YES_WORDS = 'yes|yeah|yep|yup|ya|sure|ok|okay|alright|all right|affirmative|correct|approve|approved|i approve|confirm|confirmed|' +
    'send it|send|do it|go ahead|go for it|proceed|run it|run|execute|execute it|carry on|make it so|accept|allow|allow it';
  var NO_WORDS = 'no|nope|nah|deny|denied|decline|declined|cancel|cancel it|cancel that|stop|stop it|don\'t|dont|do not|don\'t do it|' +
    'do not do it|negative|abort|reject|rejected|never mind|nevermind|no way|not now|hold on|wait|block|block it|refuse';
  var YES_RE = new RegExp('^(?:' + YES_WORDS + ')(?: (?:' + YES_WORDS + '))*$');
  var NO_RE = new RegExp('^(?:' + NO_WORDS + ')(?: (?:' + NO_WORDS + '))*$');
  function normPhrase(t) {
    var name = assistantName().toLowerCase().replace(/[^a-z0-9 ]+/g, '').trim();
    var s = String(t || '').toLowerCase().replace(/[’`]/g, "'").replace(/[^a-z0-9' ]+/g, ' ').replace(/\s+/g, ' ').trim();
    s = s.replace(/^(?:(?:um+|uh+|erm?|well|so|hey|oh)\s+)+/, '');
    if (name) {
      if (s.indexOf(name + ' ') === 0) s = s.slice(name.length + 1);
      if (s.slice(-name.length - 1) === ' ' + name) s = s.slice(0, -name.length - 1);
    }
    s = s.replace(/(?:\s+(?:please|thanks|thank you|now|then|sir|ma'am|mate|boss))+$/, '');
    return s.trim();
  }
  /** true: the phrase answered the pending card. 'strict': it was a yes/no, but this card only takes a
   *  click or key press. false: not an answer. */
  function approvalFromPhrase(text) {
    var p = S.pendingConfirm;
    if (!p) return false;
    var t = normPhrase(text);
    if (!t) return false;
    var no = NO_RE.test(t), yes = !no && YES_RE.test(t);
    if (!no && !yes) return false;
    if (isStrict(p.name)) return 'strict';
    resolveConfirm(p, yes);
    return true;
  }
  /** Point at the card's buttons when a spoken / typed answer can't be used. */
  function strictNudge(how) {
    var p = S.pendingConfirm;
    toast((how === 'voice' ? 'Voice' : 'Typed') + ' answers are off for this approval — ' + strictHelp() + '.', { tone: 'warn', key: 'strict-' + how, cooldown: 5000 });
    var card = p && partEls.get(p);
    if (card) {
      card.classList.remove('nudge');
      void card.offsetWidth; // restart the highlight animation
      card.classList.add('nudge');
    }
  }

  var MAX_DOM_ENTRIES = 220;
  function appendEntry(entry) {
    S.transcript.push(entry);
    var node = entry.k === 'u' ? renderUser(entry) : entry.k === 's' ? renderSysLine(entry) : renderAssistantShell(entry);
    node.classList.add('enter');
    tx.appendChild(node);
    // Long sessions: keep the DOM bounded (storage keeps the last 150 entries anyway).
    var rows = Array.prototype.filter.call(tx.children, function (n) { return n.classList.contains('msg') || n.classList.contains('msg-sys'); });
    for (var i = 0; i < rows.length - MAX_DOM_ENTRIES; i++) tx.removeChild(rows[i]);
    setConvoMode();
    scrollToEnd(entry.k === 'u');
    return node;
  }

  function systemLine(text, tone, tag, ephemeral) {
    var entry = { k: 's', text: text, tone: tone || 'info', tag: tag || 'SYSTEM', ts: Date.now(), ephemeral: !!ephemeral };
    appendEntry(entry);
    if (!ephemeral) saveTranscript();
    return entry;
  }

  function addPart(entry, p) {
    entry.parts.push(p);
    var shell = partEls.get(entry);
    var node = renderPart(p, entry);
    if (shell) shell.querySelector('.msg-body').appendChild(node);
    return node;
  }

  function scheduleText(p) {
    renderQueue.add(p);
    wantScroll = Math.max(wantScroll, 1);
    scheduleFrame();
  }

  function restoreTranscript() {
    tx.querySelectorAll('.msg, .msg-sys').forEach(function (n) { n.remove(); });
    var kept = [];
    S.transcript.forEach(function (entry) {
      // Entries were validated on load; still, one bad entry must never stop the rest (or init).
      try {
        var node = entry.k === 'u' ? renderUser(entry) : entry.k === 's' ? renderSysLine(entry) : renderAssistantShell(entry);
        tx.appendChild(node);
        kept.push(entry);
      } catch (e) {
        if (window.console) console.warn('ATLAS: skipped a stored transcript entry', e);
      }
    });
    if (kept.length !== S.transcript.length) { S.transcript = kept; saveTranscript(); }
    // Older sessions with history but no transcript: rebuild from the API history.
    if (!S.transcript.length && S.history.length) {
      S.history.forEach(function (m) {
        try {
          if (m.role === 'user' && typeof m.content === 'string') {
            var u = { k: 'u', text: m.content, ts: Date.now(), via: 'text' };
            tx.appendChild(renderUser(u)); S.transcript.push(u);
          } else if (m.role === 'assistant' && typeof m.content === 'string' && m.content) {
            var a = { k: 'a', parts: [{ t: 'text', text: m.content }], ts: Date.now(), model: '' };
            tx.appendChild(renderAssistantShell(a)); S.transcript.push(a);
          }
        } catch (e) { /* skip */ }
      });
    }
    setConvoMode();
    scrollToEnd(true);
  }

  function clearConversation() {
    stopAll();
    S.pendingConfirm = null;
    tickConfirm();
    S.history = [];
    S.transcript = [];
    store.del(KEYS.history);
    store.del(KEYS.transcript);
    tx.querySelectorAll('.msg, .msg-sys').forEach(function (n) { n.remove(); });
    setConvoMode();
    toast('Conversation cleared.', { tone: 'ok' });
  }

  // ------------------------------------------------------------------ chat
  function drainSSE(buf, onEvent) {
    var tail = '';
    if (buf.charAt(buf.length - 1) === '\r') { tail = '\r'; buf = buf.slice(0, -1); }
    buf = buf.replace(/\r\n?/g, '\n') + tail;
    var idx;
    while ((idx = buf.indexOf('\n\n')) !== -1) {
      var block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      var data = block.split('\n').filter(function (l) { return l.indexOf('data:') === 0; })
        .map(function (l) { return l.slice(5).replace(/^ /, ''); }).join('\n');
      if (!data || data === '[DONE]') continue;
      var ev;
      try { ev = JSON.parse(data); } catch (e) { continue; }
      if (ev && typeof ev === 'object') {
        try { onEvent(ev); } catch (e) { if (window.console) console.warn('ATLAS event handler failed', e); }
      }
    }
    return buf;
  }

  /** Stop speaking and stay quiet for the rest of the current turn (tokens keep rendering). */
  function stopSpeaking() {
    if (S.turn) S.turn.muted = true;
    speaker.stop();
  }
  function stopAll() {
    stopSpeaking();
    if (S.abort) { try { S.abort.abort(); } catch (e) { /* ignore */ } }
  }

  /** Returns true when the text was used (sent, or answered a pending approval). */
  function sendMessage(raw, via) {
    var text = String(raw || '').trim();
    if (!text) return false;
    if (S.expired) { toast('Session expired — reload the page', { key: 'expired', cooldown: 5000 }); return false; }
    if (S.pendingConfirm) {
      if (approvalFromPhrase(text) === true) return true;
      if (isStrict(S.pendingConfirm.name)) strictNudge(via === 'voice' || via === 'wake' ? 'voice' : 'text');
      else toast('Answer the pending approval first — type “yes” or “no”, or press Y / N.', { tone: 'warn', key: 'pending', cooldown: 4000 });
      return false;
    }
    if (S.streaming) { toast('Still working on the last request — press Esc to interrupt.', { tone: 'info', key: 'busy', cooldown: 4000 }); return false; }
    if (S.st && S.st.has_key === false) { showSetup(); return false; }

    speaker.stop();
    S.lastVia = via || 'text';
    S.stick = true;
    appendEntry({ k: 'u', text: text, ts: Date.now(), via: S.lastVia });
    S.history.push({ role: 'user', content: text });
    saveHistory();
    saveTranscript();
    runTurn();
    return true;
  }

  function runTurn() {
    var entry = { k: 'a', parts: [], ts: Date.now(), model: S.model };
    var shell = appendEntry(entry);
    shell.classList.add('pending');
    shell.setAttribute('aria-busy', 'true');
    var cur = null;       // current text part
    var tools = null;     // current tools group
    var toolIndex = {};   // id -> item
    var gotDone = false;
    var t0 = performance.now();
    var first = true;
    var turn = { entry: entry, muted: false, historyDone: false, tokens: 0, ttft: null };

    function speakOn() { return S.voice.speak && !turn.muted; }
    function partialText() {
      return entry.parts.filter(function (p) { return p.t === 'text'; }).map(function (p) { return p.text; }).join('\n\n').trim();
    }
    /* No 'done' (abort, failure, page closing): keep what was already said, so the model has the
     * context and user/assistant turns keep alternating. */
    function pushPartial() {
      if (gotDone || turn.historyDone) return;
      turn.historyDone = true;
      var partial = partialText();
      if (partial) S.history.push({ role: 'assistant', content: partial });
      saveHistory();
    }
    turn.pushPartial = pushPartial;

    S.turn = turn;
    S.streaming = true;
    S.toolsRunning = 0;
    refreshState();
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    S.abort = ctrl;

    function firstSignal() {
      if (!first) return;
      first = false;
      shell.classList.remove('pending');
      turn.ttft = S.lastTtft = Math.round(performance.now() - t0);
      renderSession();
    }
    function closeText() {
      if (cur) { var n = partEls.get(cur); if (n) n.classList.remove('streaming'); }
      cur = null;
    }
    function chipFor(group, id) {
      var g = partEls.get(group);
      if (!g) return null;
      var chips = g.querySelectorAll('.tool');
      for (var i = 0; i < chips.length; i++) if (chips[i].dataset.id === String(id)) return chips[i];
      return null;
    }

    function onEvent(ev) {
      switch (ev.type) {
        case 'model':
          if (typeof ev.model === 'string' && ev.model) {
            setModel(ev.model);
            entry.model = ev.model;
            var mm = shell.querySelector('.msg-model');
            if (mm) mm.textContent = shortModel(ev.model);
          }
          break;
        case 'token':
          if (typeof ev.text !== 'string') break;
          firstSignal();
          if (!cur) {
            cur = { t: 'text', text: '' };
            tools = null;
            addPart(entry, cur).classList.add('streaming');
          }
          cur.text += ev.text;
          scheduleText(cur);
          if (speakOn()) speaker.feed(ev.text);
          saveTranscriptSoon();
          break;
        case 'segment_end':
          closeText();
          if (speakOn()) speaker.flush();
          break;
        case 'tool':
          firstSignal();
          if (ev.state === 'start') {
            closeText();
            if (!tools) { tools = { t: 'tools', items: [] }; addPart(entry, tools); }
            var it = { id: str(ev.id, 200) || '', name: str(ev.name, 120) || '', label: str(ev.label, 200) || str(ev.name, 120) || 'Tool', hint: toolHint(ev.args), state: 'run', summary: '' };
            tools.items.push(it);
            toolIndex[it.id] = { item: it, group: tools };
            var gnode = partEls.get(tools);
            if (gnode) gnode.appendChild(renderTool(it));
            if (core) core.calloutStart(it.id || ('tool-' + tools.items.length), it.label);
            S.toolsRunning++;
            if (isWebTool(it.name)) webStart(it, ev.args);
          } else {
            var ref = toolIndex[str(ev.id, 200) || ''];
            if (ref) {
              ref.item.state = ev.state === 'done' ? 'ok' : 'err';
              ref.item.summary = str(ev.summary, 2000) || (ev.state === 'done' ? 'done' : 'failed');
              var chip = chipFor(ref.group, ref.item.id);
              if (chip) chip.replaceWith(renderTool(ref.item));
              if (core) core.calloutEnd(ref.item.id, ev.state === 'done');
              webFinish(ref.item.id, ev.state === 'done');
            }
            S.toolsRunning = Math.max(0, S.toolsRunning - 1);
          }
          scrollToEnd(false);
          refreshState();
          saveTranscriptSoon();
          break;
        case 'confirm':
          firstSignal();
          closeText();
          tools = null;
          var cp = {
            t: 'confirm', id: str(ev.id, 200) || '', call_id: str(ev.call_id, 200) || '', name: str(ev.name, 120) || '',
            title: str(ev.title, 400) || '', fields: Array.isArray(ev.fields) ? ev.fields : [], body: str(ev.body, 20000) || '',
            code: !!ev.code, result: null, deadline: Date.now() + CONFIRM_MS
          };
          var cardNode = addPart(entry, cp);
          S.pendingConfirm = cp;
          if (confirmKind(cp.name) === 'web') webAwait(cp);
          startConfirmTimer();
          var strictCard = isStrict(cp.name);
          // A voice request gets a one-shot listener for the answer, except on cards that ignore spoken answers.
          S.awaitVoiceAnswer = !strictCard && (S.lastVia !== 'text' || S.voice.wake);
          if (strictCard && S.listening === 'command') listener.stop();
          V.chime('wake');
          if (speakOn() && speaker.supported) speaker.say(strictCard ? 'Awaiting your approval on screen.' : 'Awaiting your approval.');
          else if (S.awaitVoiceAnswer && listener.supported) { S.awaitVoiceAnswer = false; setTimeout(function () { if (S.pendingConfirm === cp && !S.listening) startListening(); }, 300); }
          scrollToEnd(true);
          wantCard = cardNode;
          saveTranscript();
          refreshState();
          break;
        case 'confirm_result':
          var target = S.pendingConfirm && S.pendingConfirm.id === ev.id ? S.pendingConfirm :
            entry.parts.filter(function (x) { return x.t === 'confirm' && x.id === ev.id; })[0];
          if (target) {
            // The server answers "denied" when its 300 s wait ran out: show that as EXPIRED.
            var timedOut = !ev.approved && !target.sending && target.deadline && Date.now() >= target.deadline - 3000;
            lockConfirm(target, ev.approved ? 'approved' : timedOut ? 'expired' : 'denied');
            if (confirmKind(target.name) === 'web') webResolved(!!ev.approved);
          }
          S.awaitVoiceAnswer = false;
          if (S.listening === 'command') listener.stop();
          refreshState();
          saveTranscriptSoon();
          break;
        case 'usage':
          S.usage.prompt += num(ev.prompt_tokens) || 0;
          S.usage.completion += num(ev.completion_tokens) || 0;
          S.usage.cost += num(ev.cost) || 0;
          turn.tokens += (num(ev.prompt_tokens) || 0) + (num(ev.completion_tokens) || 0);
          renderUsage();
          break;
        case 'refresh':
          if (ev.panel === 'reminders') loadReminders();
          if (ev.panel === 'agenda') loadAgenda();
          if (ev.panel === 'memory' && isDrawerOpen()) loadMemory();
          break;
        case 'error':
          firstSignal();
          closeText();
          var et = str(ev.text, 2000) || 'Something went wrong.';
          addPart(entry, { t: 'error', text: et });
          toast(et);
          V.chime('error');
          errorFlash();
          scrollToEnd(false);
          break;
        case 'done':
          gotDone = true;
          if (Array.isArray(ev.messages)) {
            ev.messages.forEach(function (m) { if (isObj(m) && ROLES[m.role]) S.history.push(m); });
          }
          S.turns++;
          renderUsage();
          saveHistory();
          break;
        default:
          break;
      }
    }

    var req = api('/api/chat', { method: 'POST', body: { messages: outgoingHistory() }, signal: ctrl ? ctrl.signal : undefined, raw: true });
    req.then(function (res) {
      if (!res.body || !res.body.getReader) {
        return res.text().then(function (txt) { drainSSE(txt + '\n\n', onEvent); });
      }
      var reader = res.body.getReader();
      var dec = new TextDecoder();
      var buf = '';
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) {
            buf += dec.decode();
            drainSSE(buf + '\n\n', onEvent);
            return;
          }
          buf = drainSSE(buf + dec.decode(r.value, { stream: true }), onEvent);
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      if (e && e.name === 'AbortError') {
        addPart(entry, { t: 'note', text: 'Interrupted.' });
        return;
      }
      if (e && e.status === 401) {
        addPart(entry, { t: 'error', text: 'Session expired — reload the page.' });
        return;
      }
      var msg = (e && e.message) || 'Request failed';
      addPart(entry, { t: 'error', text: msg });
      toast(msg);
      errorFlash();
    }).then(function () {
      shell.classList.remove('pending');
      shell.removeAttribute('aria-busy');
      closeText();
      if (speakOn()) speaker.flush();
      pushPartial();
      if (S.pendingConfirm) lockConfirm(S.pendingConfirm, 'expired');
      entry.parts.forEach(function (p) {
        if (p.t === 'tools') p.items.forEach(function (it) {
          if (it.state === 'run') {
            it.state = 'err'; it.summary = 'interrupted';
            var c = chipFor(p, it.id);
            if (c) c.replaceWith(renderTool(it));
            if (core) core.calloutEnd(it.id, false);
          }
        });
      });
      if (!entry.parts.length) { addPart(entry, { t: 'note', text: 'No response.' }); }
      if (core) core.calloutsClear(2600);
      S.turnStats.push({ tokens: turn.tokens, ttft: turn.ttft, ms: Math.round(performance.now() - t0) });
      if (S.turnStats.length > 60) S.turnStats.shift();
      renderSession();
      if (S.turn === turn) S.turn = null;
      S.streaming = false;
      S.abort = null;
      S.toolsRunning = 0;
      webEnd();
      saveTranscript();
      refreshState();
      scrollToEnd(false);
      // Screen readers hear the finished reply once (the streaming bubble itself is not announced).
      var said = V.cleanForSpeech(partialText());
      if (!said) {
        var last = entry.parts[entry.parts.length - 1];
        said = last && (last.t === 'error' || last.t === 'note') ? last.text : '';
      }
      announce(said ? assistantName() + ': ' + said : '');
    });
  }

  // Closing or reloading mid-reply keeps the partial answer (transcript and history).
  // (Only while a reply is streaming: otherwise this tab has nothing newer than storage.)
  window.addEventListener('pagehide', function () {
    if (!S.turn) return;
    try {
      if (S.turn.pushPartial) S.turn.pushPartial();
      saveTranscript();
    } catch (e) { /* ignore */ }
  });

  // ------------------------------------------------------------------ command bar
  var form = $('#cmdbar');
  var input = $('#cmd-input');
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var v = input.value;
    if (S.pendingConfirm) {
      // A typed "yes" / "no" answers a low-risk card; anything else (and any answer to a high-risk card) stays in the box.
      if (!v.trim()) {
        if (isStrict(S.pendingConfirm.name)) strictNudge('text');
        else toast('Type “yes” or “no”, or press Y / N, to answer the pending approval.', { tone: 'info', key: 'pending', cooldown: 4000 });
        return;
      }
      if (sendMessage(v, 'text')) { input.value = ''; input.classList.remove('interim'); }
      return;
    }
    if (S.streaming && !v.trim()) { stopAll(); return; }
    if (S.streaming) {
      toast('Still working — press Esc or the stop button to interrupt.', { tone: 'info', key: 'busy', cooldown: 4000 });
      return;
    }
    input.value = '';
    input.classList.remove('interim');
    if (!sendMessage(v, 'text') && v.trim() && !input.value) input.value = v;
  });
  $('#send-btn').addEventListener('click', function (e) {
    if (S.streaming && !S.pendingConfirm && !input.value.trim()) { e.preventDefault(); stopAll(); }
  });
  $('#mic-btn').addEventListener('click', function () { toggleListening(); });
  $('#core').addEventListener('click', function () {
    if (S.speaking || speaker.busy()) { stopSpeaking(); refreshState(); return; }
    if (S.streaming) { stopAll(); return; }
    toggleListening();
  });

  var QUICK = {
    briefing: 'Give me my daily briefing: local weather, top headlines across world, US, business and tech, a markets snapshot, my unread email count if email is set up, and upcoming reminders. Keep it tight.',
    myday: "What's on my calendar and to-do list today?",
    headlines: 'What are the top headlines right now?',
    markets: 'Give me a quick markets snapshot.',
    inbox: 'Check my inbox — how many unread emails do I have, and is anything important?',
    system: 'Run a quick system check on this computer.'
  };
  // Chips that only start a request: the text goes into the command bar for the user to finish.
  var PREFILL = { table: 'Book a table for 2 at ' };
  function prefill(text) {
    input.classList.remove('interim');
    input.value = text;
    try {
      input.focus({ preventScroll: true });
      input.setSelectionRange(text.length, text.length);
      input.scrollLeft = input.scrollWidth;
    } catch (e) { /* ignore */ }
  }
  $$('#transcript-empty .try').forEach(function (b) {
    b.addEventListener('click', function () {
      if (b.dataset.prefill) prefill(b.dataset.prefill);
      else if (b.dataset.try) sendMessage(b.dataset.try, 'text');
    });
  });
  $$('#chips .chip').forEach(function (c) {
    c.addEventListener('click', function () {
      var a = c.dataset.action;
      if (PREFILL[a]) { prefill(PREFILL[a]); return; }
      var q = QUICK[a];
      if (q) sendMessage(q, 'text');
    });
  });

  // ------------------------------------------------------------------ keyboard
  document.addEventListener('keydown', function (e) {
    var t = e.target;
    var tag = (t && t.tagName) || '';
    var typing = /INPUT|TEXTAREA|SELECT/.test(tag) || !!(t && t.isContentEditable);
    var overlay = !$('#setup').hidden || !$('#engage').hidden;
    if ((e.ctrlKey || e.metaKey) && (e.code === 'Space' || e.key === ' ')) {
      if (!S.engaged || overlay) return;
      e.preventDefault();
      toggleListening();
      return;
    }
    if (e.key === 'Escape') {
      if (isDrawerOpen()) { closeDrawer(); return; }
      var acted = false;
      if (S.listening === 'command') { listener.stop(true); acted = true; }
      if (S.speaking || speaker.busy()) { stopSpeaking(); acted = true; }
      if (S.streaming) { stopAll(); acted = true; }
      if (acted) { e.preventDefault(); refreshState(); }
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || typing || overlay || !S.engaged || isDrawerOpen()) return;
    // Y / N answer a pending approval card (never while typing in a field, never auto-repeat).
    // A key press is deliberate input, so this also answers high-risk cards (once their buttons are armed).
    if (S.pendingConfirm && (e.key === 'y' || e.key === 'Y' || e.key === 'n' || e.key === 'N')) {
      e.preventDefault();
      if (!e.repeat && e.isTrusted !== false) resolveConfirm(S.pendingConfirm, e.key === 'y' || e.key === 'Y');
      return;
    }
    if (e.key === '/') { e.preventDefault(); input.focus(); }
  });

  // ------------------------------------------------------------------ panels: common
  function panelStatus(id, status, text) {
    var p = document.getElementById(id);
    if (!p) return;
    p.dataset.status = status;
    var sync = p.querySelector('[data-sync]');
    if (sync) sync.textContent = text || (status === 'live' ? fmtHM(new Date()) : status === 'offline' ? 'OFFLINE' : 'SYNC');
  }
  function fmtHM(d) { return pad2(d.getHours()) + ':' + pad2(d.getMinutes()); }
  var ICON_FAULT = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.8l9.2 9.2-9.2 9.2L2.8 12z"/><path d="M12 7.6v5.6M12 16.2v.2"/></svg>';
  var ICON_EMPTY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10"/></svg>';
  var ICON_RETRY = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.8 4.2v4.4h-4.4"/></svg>';
  /** A fault block (striped, with icon and retry) or, with tone 'empty', a calm "nothing here" note. */
  function offlineBlock(title, detail, retry, tone) {
    var d = el('div', 'offline' + (tone === 'empty' ? ' is-empty' : ''));
    d.innerHTML = '<span class="of-ico">' + (tone === 'empty' ? ICON_EMPTY : ICON_FAULT) + '</span>' +
      '<span class="of-txt"><b>' + esc(title) + '</b><span>' + esc(clip(detail || 'No data received.', 160)) + '</span></span>';
    if (retry) {
      var b = el('button', 'btn btn-ghost btn-xs of-retry', ICON_RETRY + '<span>Retry</span>');
      b.type = 'button';
      b.addEventListener('click', retry);
      d.appendChild(b);
    }
    return d;
  }
  function errText(e) { return e && e.status === 401 ? 'Session expired — reload the page.' : (e && e.message) || 'Unavailable'; }

  // ------------------------------------------------------------------ weather
  var WX_ICONS = {
    clear: '<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M5.3 18.7l1.6-1.6M17.1 6.9l1.6-1.6"/>',
    night: '<path d="M19.5 14.5A8 8 0 0 1 9.5 4.5a8 8 0 1 0 10 10z"/>',
    partly: '<path d="M8 5.5v1.4M3.6 9.5H5M4.9 6.4l1 1M11.2 6.4l-1 1"/><path d="M5.6 12.2A3.4 3.4 0 0 1 11 8.4"/><path d="M8.5 19h9a3.5 3.5 0 0 0 .4-7 5 5 0 0 0-9.6 1.3A2.9 2.9 0 0 0 8.5 19z"/>',
    partlynight: '<path d="M10.5 8.2A4 4 0 0 1 5 4a4.4 4.4 0 1 0 5.5 4.2z"/><path d="M8.5 19h9a3.5 3.5 0 0 0 .4-7 5 5 0 0 0-9.6 1.3A2.9 2.9 0 0 0 8.5 19z"/>',
    cloud: '<path d="M7 18.5h10.5a4 4 0 0 0 .5-8 5.8 5.8 0 0 0-11.2 1.6A3.3 3.3 0 0 0 7 18.5z"/>',
    fog: '<path d="M4 9h16M6 13h12M4 17h16"/>',
    drizzle: '<path d="M7 14.5h10.5a4 4 0 0 0 .5-8 5.8 5.8 0 0 0-11.2 1.6A3.3 3.3 0 0 0 7 14.5z"/><path d="M9 17.5l-.5 1.5M13 17.5l-.5 1.5M17 17.5l-.5 1.5"/>',
    rain: '<path d="M7 13.5h10.5a4 4 0 0 0 .5-8 5.8 5.8 0 0 0-11.2 1.6A3.3 3.3 0 0 0 7 13.5z"/><path d="M8.5 16l-1.2 3.5M12.5 16l-1.2 3.5M16.5 16l-1.2 3.5"/>',
    snow: '<path d="M7 13.5h10.5a4 4 0 0 0 .5-8 5.8 5.8 0 0 0-11.2 1.6A3.3 3.3 0 0 0 7 13.5z"/><path d="M8.5 17v.1M12 19v.1M15.5 17v.1M10 21v.1M14 21v.1"/>',
    storm: '<path d="M7 13.5h10.5a4 4 0 0 0 .5-8 5.8 5.8 0 0 0-11.2 1.6A3.3 3.3 0 0 0 7 13.5z"/><path d="M12.5 14l-2.5 4h3l-2 4"/>'
  };
  function wxKind(code, text, isDay) {
    var c = num(code);
    var day = isDay !== false && isDay !== 0;
    if (c != null) {
      if (c === 0) return day ? 'clear' : 'night';
      if (c === 1 || c === 2) return day ? 'partly' : 'partlynight';
      if (c === 3) return 'cloud';
      if (c === 45 || c === 48) return 'fog';
      if (c >= 51 && c <= 57) return 'drizzle';
      if ((c >= 61 && c <= 67) || (c >= 80 && c <= 82)) return 'rain';
      if ((c >= 71 && c <= 77) || c === 85 || c === 86) return 'snow';
      if (c >= 95) return 'storm';
    }
    var t = String(text || '').toLowerCase();
    if (/thunder|storm/.test(t)) return 'storm';
    if (/snow|sleet|flurr/.test(t)) return 'snow';
    if (/drizzle/.test(t)) return 'drizzle';
    if (/rain|shower/.test(t)) return 'rain';
    if (/fog|mist|haze/.test(t)) return 'fog';
    if (/overcast|cloudy$|^cloud/.test(t) && !/partly|mainly/.test(t)) return 'cloud';
    if (/partly|mainly|few clouds/.test(t)) return 'partly';
    if (/clear|sun|fair/.test(t)) return 'clear';
    return 'cloud';
  }
  function wxSvg(kind, cls) { return '<svg class="' + (cls || 'wx-ico') + '" viewBox="0 0 24 24" aria-hidden="true">' + (WX_ICONS[kind] || WX_ICONS.cloud) + '</svg>'; }
  function round(v) { var n = num(v); return n == null ? '—' : String(Math.round(n)); }

  function renderWeather(d) {
    var body = $('#weather-body');
    var cur = d.current || {};
    var units = d.units || {};
    var tu = units.temp || '°';
    var loc = String(d.location || '').split(',');
    var city = (loc[0] || 'Local').trim();
    var region = loc.slice(1, 2).join(',').trim();
    var days = Array.isArray(d.forecast) ? d.forecast.slice(0, 4) : [];
    var html = '<div class="wx">' +
      '<div class="wx-now">' + wxSvg(wxKind(cur.code, cur.condition, cur.is_day), 'wx-ico wx-ico-lg') +
      '<div class="wx-temp"><span class="wx-deg">' + round(cur.temperature) + '</span><span class="wx-unit">' + esc(tu) + '</span></div>' +
      '<div class="wx-desc"><b>' + esc(cur.condition || '—') + '</b><span>' + esc(city) + (region ? ' · ' + esc(region) : '') + '</span></div></div>' +
      '<dl class="wx-stats">' +
      '<div><dt>Feels</dt><dd>' + round(cur.feels_like) + '°</dd></div>' +
      '<div><dt>Humid</dt><dd>' + round(cur.humidity) + '%</dd></div>' +
      '<div><dt>Wind</dt><dd>' + round(cur.wind) + ' <small>' + esc(units.wind || '') + '</small></dd></div></dl>';
    if (days.length) {
      // Forecast tiles: day, icon, high / low, and a precipitation bar.
      html += '<ol class="wx-days" style="--n:' + days.length + '">' + days.map(function (f, i) {
        var dt = f.date ? new Date(String(f.date) + 'T12:00:00') : null;
        var dn = i === 0 ? 'Today' : dt && !isNaN(dt) ? dt.toLocaleDateString(undefined, { weekday: 'short' }) : '—';
        var pc = Math.round(Math.max(0, Math.min(100, num(f.precip_chance) || 0)));
        return '<li class="wd' + (i === 0 ? ' is-today' : '') + (pc >= 50 ? ' is-wet' : '') + '" title="' + esc(String(f.condition || '')) + '">' +
          '<span class="wd-name">' + esc(dn) + '</span>' + wxSvg(wxKind(null, f.condition, true), 'wx-ico wx-ico-sm') +
          '<span class="wd-temp"><span class="sr-only">high </span><b>' + round(f.high) + '°</b><span class="sr-only">, low </span><i>' + round(f.low) + '°</i></span>' +
          '<span class="wd-pp"><span class="wd-bar" aria-hidden="true"><i style="width:' + pc + '%"></i></span>' +
          '<span class="wd-ppv"><svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.5C4.4 4 3 5.6 3 7.4a3 3 0 0 0 6 0C9 5.6 7.6 4 6 1.5z"/></svg>' + pc + '%<span class="sr-only"> chance of rain</span></span></span></li>';
      }).join('') + '</ol>';
    }
    html += '</div>';
    body.innerHTML = html;
    setText('#hero-wx', round(cur.temperature) + tu + ' · ' + String(cur.condition || '').trim() + ' · ' + city);
  }

  function loadWeather() {
    return api('/api/weather').then(function (d) {
      if (!d || d.error || !d.current) {
        setText('#hero-wx', '');
        panelStatus('panel-weather', 'offline');
        $('#weather-body').replaceChildren(offlineBlock('WEATHER UPLINK OFFLINE', d && d.error, loadWeather));
        return;
      }
      renderWeather(d);
      panelStatus('panel-weather', 'live');
    }).catch(function (e) {
      panelStatus('panel-weather', 'offline');
      $('#weather-body').replaceChildren(offlineBlock('WEATHER UPLINK OFFLINE', errText(e), loadWeather));
    });
  }

  // ------------------------------------------------------------------ system
  var sysBuilt = false;
  function buildSystem() {
    var body = $('#system-body');
    body.innerHTML = '<div class="sys">' +
      ['cpu:CPU', 'mem:MEM', 'disk:DISK', 'bat:PWR'].map(function (s) {
        var k = s.split(':');
        return '<div class="sys-row" data-k="' + k[0] + '"><span class="sys-k">' + k[1] + '</span>' +
          '<span class="meter" role="meter" aria-valuemin="0" aria-valuemax="100" aria-label="' + k[1] + ' usage"><i></i></span>' +
          '<span class="sys-v">—</span><span class="sys-x"></span></div>';
      }).join('') +
      '<div class="sys-spark"><svg viewBox="0 0 120 26" preserveAspectRatio="none" aria-hidden="true"><path class="sp-area"/><path class="sp-line"/></svg><span>CPU · 2 MIN</span></div>' +
      '<dl class="sys-meta"><div><dt>Host</dt><dd data-m="host">—</dd></div><div><dt>Uptime</dt><dd data-m="up">—</dd></div>' +
      '<div><dt>OS</dt><dd data-m="os">—</dd></div></dl></div>';
    sysBuilt = true;
  }
  function setMeter(k, pct, valText, extra) {
    var row = $('#system-body .sys-row[data-k="' + k + '"]');
    if (!row) return;
    if (pct == null) { row.hidden = true; return; }
    row.hidden = false;
    var p = Math.max(0, Math.min(100, pct));
    row.querySelector('.meter i').style.width = p + '%';
    row.querySelector('.meter').setAttribute('aria-valuenow', String(Math.round(p)));
    row.querySelector('.sys-v').textContent = valText;
    row.querySelector('.sys-x').textContent = extra || '';
    row.classList.toggle('hot', p >= 85);
    row.classList.toggle('warm', p >= 65 && p < 85);
  }
  function fmtUptime(h) {
    var n = num(h);
    if (n == null) return '—';
    var mins = Math.round(n * 60), d = Math.floor(mins / 1440), hh = Math.floor((mins % 1440) / 60), mm = mins % 60;
    return d ? d + 'd ' + pad2(hh) + 'h' : pad2(hh) + 'h ' + pad2(mm) + 'm';
  }
  function renderSystemPanel(d) {
    if (!sysBuilt) buildSystem();
    var cpu = num(d.cpu_percent);
    setMeter('cpu', cpu, cpu == null ? '—' : cpu.toFixed(1) + '%', num(d.cpu_cores) != null ? d.cpu_cores + ' cores' : '');
    var m = d.memory || {};
    setMeter('mem', num(m.percent), num(m.percent) == null ? '—' : Math.round(m.percent) + '%', num(m.used_gb) != null ? m.used_gb.toFixed(1) + '/' + Math.round(m.total_gb) + 'G' : '');
    var dk = d.disk;
    setMeter('disk', dk ? num(dk.percent) : null, dk ? Math.round(dk.percent) + '%' : '', dk && num(dk.free_gb) != null ? Math.round(dk.free_gb) + 'G free' : '');
    var b = d.battery;
    setMeter('bat', b ? num(b.percent) : null, b ? Math.round(b.percent) + '%' : '', b ? (b.plugged_in ? 'AC' : num(b.minutes_left) ? Math.floor(b.minutes_left / 60) + 'h' + pad2(b.minutes_left % 60) : 'BATT') : '');
    var row = $('#system-body .sys-row[data-k="bat"]');
    if (row && b && !b.plugged_in && num(b.percent) != null) row.classList.toggle('hot', b.percent <= 15);
    var meta = $('#system-body .sys-meta');
    meta.querySelector('[data-m="host"]').textContent = d.host || '—';
    meta.querySelector('[data-m="up"]').textContent = fmtUptime(d.uptime_hours);
    meta.querySelector('[data-m="os"]').textContent = d.os || '—';
    if (cpu != null) {
      S.cpuHist.push(cpu);
      if (S.cpuHist.length > 24) S.cpuHist.shift();
    }
    drawSpark();
  }
  function drawSpark() {
    var svg = $('#system-body .sys-spark svg');
    if (!svg) return;
    var h = S.cpuHist.length ? S.cpuHist : [0];
    var pts = h.map(function (v, i) { return [h.length === 1 ? 120 : i / (h.length - 1) * 120, 24 - Math.max(0, Math.min(100, v)) / 100 * 22]; });
    if (pts.length === 1) pts.unshift([0, pts[0][1]]);
    var line = 'M' + pts.map(function (p) { return p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' L');
    svg.querySelector('.sp-line').setAttribute('d', line);
    svg.querySelector('.sp-area').setAttribute('d', line + ' L120 26 L0 26 Z');
  }
  function loadSystem() {
    return api('/api/system').then(function (d) {
      if (!d || d.error || (d.cpu_percent == null && !d.memory)) {
        sysBuilt = false;
        panelStatus('panel-system', 'offline');
        $('#system-body').replaceChildren(offlineBlock('TELEMETRY OFFLINE', d && d.error, loadSystem));
        return;
      }
      renderSystemPanel(d);
      panelStatus('panel-system', 'live');
    }).catch(function (e) {
      sysBuilt = false;
      panelStatus('panel-system', 'offline');
      $('#system-body').replaceChildren(offlineBlock('TELEMETRY OFFLINE', errText(e), loadSystem));
    });
  }

  // ------------------------------------------------------------------ agenda (calendar · to-dos · reminders)
  /* One panel for the day ahead: today's and tomorrow's events (GET /api/agenda?days=2), open to-dos
   * (same call) and pending reminders (GET /api/reminders). Both feeds fail independently; every value
   * is validated before use and rendered as text only (event titles come from outside calendars). */
  var reminders = [];
  var AG = { data: null, err: '', remErr: '', tried: false, at: 0, seq: 0 };
  function parseDue(s) { var d = new Date(s); return isNaN(d) ? null : d; }
  function relTime(d) {
    var diff = d - Date.now();
    var m = Math.round(diff / 60000);
    if (diff < 0) return 'due';
    if (m < 1) return '<1m';
    if (m < 60) return 'in ' + m + 'm';
    var h = Math.floor(m / 60);
    if (h < 24) return 'in ' + h + 'h ' + pad2(m % 60) + 'm';
    return 'in ' + Math.round(h / 24) + 'd';
  }
  function hm(d) { return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' }); }
  function dueLabel(d) {
    var now = new Date();
    var tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
    if (d.toDateString() === now.toDateString()) return 'Today ' + hm(d);
    if (d.toDateString() === tomorrow.toDateString()) return 'Tomorrow ' + hm(d);
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) + ' ' + hm(d);
  }
  function dayStart(d, add) { var x = new Date(d); x.setHours(0, 0, 0, 0); if (add) x.setDate(x.getDate() + add); return x; }
  /** "2026-11-04" (an all-day date, local midnight) or an ISO date-time; null if neither. */
  function agTime(s) {
    if (typeof s !== 'string' || !s) return null;
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
    var d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s);
    return isNaN(d) ? null : d;
  }
  function cleanEvent(e) {
    if (!isObj(e)) return null;
    var allDay = e.all_day === true;
    var start = agTime(e.start), end = agTime(e.end);
    if (!start) return null;
    if (allDay && end) end = dayStart(end, 1); // the server's all-day end date is inclusive
    if (!end || end < start) end = allDay ? dayStart(start, 1) : start;
    return {
      title: (str(e.title, 300) || '').trim() || '(untitled event)', start: start, end: end, allDay: allDay,
      location: (str(e.location, 200) || '').replace(/\s+/g, ' ').trim(), calendar: (str(e.calendar, 60) || '').trim(),
      when: str(e.when, 120) || '', tentative: e.tentative === true, free: e.show_as === 'free'
    };
  }
  var PRIORITY = { high: 3, normal: 2, low: 1 };
  function cleanTask(t) {
    if (!isObj(t) || t.done === true) return null;
    var title = (str(t.title, 300) || '').trim();
    if (!title) return null;
    return {
      title: title, priority: PRIORITY[t.priority] ? t.priority : 'normal', overdue: t.overdue === true,
      due: str(t.due, 40) || '', dueHuman: str(t.due_human, 80) || ''
    };
  }
  function askAbout(text) {
    if (!sendMessage(text, 'text')) return;
    if (isPhone() && document.body.dataset.view !== 'core') setView('core');
  }
  function agItem(cls, html, ask, title) {
    var li = el('li');
    var b = el('button', 'ag-item ' + cls, '<span class="sr-only">Ask about: </span>' + html);
    b.type = 'button';
    if (title) b.title = title;
    b.addEventListener('click', function () { askAbout(ask); });
    li.appendChild(b);
    return li;
  }
  function agNotice(tone, tag, text, action) {
    var d = el('div', 'ag-note tone-' + tone);
    d.innerHTML = '<b>' + esc(tag) + '</b><span>' + esc(clip(text, 160)) + '</span>';
    if (action) {
      var b = el('button', 'ag-note-act', esc(action.label));
      b.type = 'button';
      b.addEventListener('click', action.run);
      d.appendChild(b);
    }
    return d;
  }
  function agHead(label, extra) {
    return el('h3', 'ag-h', '<span>' + esc(label) + '</span>' + (extra ? '<i>' + esc(extra) + '</i>' : ''));
  }
  function shortDay(d) { return d.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short' }); }

  function eventRow(e, now, isNext) {
    var live = !e.allDay && e.start <= now && now < e.end;
    var t1, t2;
    if (e.allDay) { t1 = 'All day'; t2 = ''; }
    else if (live) { t1 = 'Now'; t2 = e.end > e.start ? hm(e.end) : ''; }
    else { t1 = hm(e.start); t2 = e.end > e.start ? hm(e.end) : ''; }
    var loc = /^(https?|webcal):\/\//i.test(e.location) ? hostOf(e.location) || e.location : e.location; // a meeting link: its site
    var sub = [loc, e.calendar].filter(Boolean).map(function (x) { return clip(x, 60); }).join(' · ');
    var tag = live ? '<em class="ag-tag live">Live</em>'
      : isNext && e.start - now < 3 * 3600000 ? '<em class="ag-tag">' + esc(relTime(e.start)) + '</em>'
      : e.tentative ? '<em class="ag-tag dim">Maybe</em>' : '';
    var html = '<span class="ag-time"><b>' + esc(t1) + '</b>' + (t2 ? '<i>' + esc(t2) + '</i>' : '') + '</span>' +
      '<span class="ag-main"><span class="ag-title">' + esc(e.title) + '</span>' + (sub ? '<span class="ag-sub">' + esc(sub) + '</span>' : '') + '</span>' + tag;
    var when = e.when || (e.allDay ? shortDay(e.start) + ', all day' : dueLabel(e.start));
    var cls = 'ag-ev' + (live ? ' is-live' : '') + (isNext ? ' is-next' : '') + (e.free ? ' is-free' : '');
    return agItem(cls, html, 'Tell me about my calendar event "' + clip(e.title, 160) + '" (' + clip(when, 80) + ').', e.title + (sub ? ' — ' + sub : ''));
  }
  function taskRow(t) {
    var bars = '<span class="ag-pri" data-p="' + t.priority + '" title="' + esc(t.priority) + ' priority"><i></i><i></i><i></i></span>';
    var due = t.dueHuman || t.due;
    var html = bars + '<span class="ag-main"><span class="ag-title">' + esc(t.title) + '</span>' +
      (due ? '<span class="ag-sub">Due ' + esc(due) + '</span>' : '') + '</span>' +
      (t.overdue ? '<em class="ag-tag over">Overdue</em>' : t.priority === 'high' ? '<em class="ag-tag hi">High</em>' : '');
    var ask = 'Tell me about my to-do "' + clip(t.title, 160) + '"' + (due ? ' (due ' + clip(due, 60) + (t.overdue ? ', overdue' : '') + ')' : '') + '.';
    return agItem('ag-task' + (t.overdue ? ' is-over' : '') + ' pri-' + t.priority, html, ask, t.title);
  }
  function reminderRow(r) {
    var d = parseDue(r.due);
    var msg = (str(r.message, 300) || 'Reminder').trim() || 'Reminder';
    var soon = d && d - Date.now() < 10 * 60000;
    var html = '<span class="ag-time"><b>' + esc(d ? relTime(d) : '—') + '</b><i>' + esc(d ? hm(d) : '') + '</i></span>' +
      '<span class="ag-main"><span class="ag-title">' + esc(msg) + '</span>' + (d && d.toDateString() !== new Date().toDateString() ? '<span class="ag-sub">' + esc(dueLabel(d)) + '</span>' : '') + '</span>';
    return agItem('ag-rem' + (soon ? ' is-soon' : ''), html, 'Tell me about my reminder "' + clip(msg, 160) + '"' + (d ? ' (' + dueLabel(d) + ')' : '') + '.', msg);
  }

  function renderAgenda() {
    if (!AG.tried) return; // keep the skeleton until the agenda call has answered once
    var body = $('#agenda-body');
    var d = AG.data;
    var now = new Date();
    var tomorrow0 = dayStart(now, 1), after0 = dayStart(now, 2);
    var events = (d && Array.isArray(d.events) ? d.events : []).slice(0, 60).map(function (e) {
      try { return cleanEvent(e); } catch (x) { return null; }
    }).filter(function (e) {
      return e && (e.end > now || (+e.end === +e.start && e.start >= now - 60000)); // drop what has ended since the last fetch
    }).sort(function (a, b) { return a.start - b.start || (b.allDay - a.allDay); });
    var tasks = (d && Array.isArray(d.tasks) ? d.tasks : []).slice(0, 30).map(function (t) {
      try { return cleanTask(t); } catch (x) { return null; }
    }).filter(Boolean);
    var openTasks = d ? Math.max(tasks.length, num(d.open_tasks) || 0) : 0;
    var rems = reminders.slice(0, 20);
    var frag = document.createDocumentFragment();

    // ---- notices: feed down, calendar not connected, per-feed errors
    if (AG.err && !d) frag.appendChild(agNotice('err', 'Agenda offline', AG.err, { label: 'Retry', run: loadAgenda }));
    else if (AG.err) frag.appendChild(agNotice('warn', 'Offline', 'Showing the last sync from ' + fmtHM(new Date(AG.at)) + '.', { label: 'Retry', run: loadAgenda }));
    if (d && d.feeds_connected === false) {
      frag.appendChild(agNotice('cal', 'Calendar not connected', 'Link Google, iCloud or Outlook to see your events here.', {
        label: 'Connect', run: function () { openDrawer({ section: 'calendar', focus: 'CALENDAR_ICS_URLS' }); }
      }));
    }
    (d && Array.isArray(d.errors) ? d.errors : []).slice(0, 3).forEach(function (m) {
      var t = str(m, 300);
      if (t) frag.appendChild(agNotice('warn', 'Feed error', t, { label: 'Retry', run: loadAgenda }));
    });
    if (AG.remErr) frag.appendChild(agNotice('warn', 'Reminders offline', AG.remErr, { label: 'Retry', run: loadReminders }));

    var total = events.length + tasks.length + rems.length;
    if (!total) {
      if (frag.childNodes.length) {
        // a notice already explains the state: one quiet line instead of a second big block
        if (d) frag.appendChild(el('p', 'ag-none', 'No open to-dos or reminders. Try “add a to-do to renew my passport”.'));
      } else {
        var empty = el('div', 'empty-state ag-empty');
        empty.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6.5h16V20H4z"/><path d="M4 10.5h16M8.5 3.5v5M15.5 3.5v5"/><path d="M9 15l2 2 4-4"/></svg>' +
          '<b>Clear schedule</b><span>Nothing on the calendar today or tomorrow, and no open to-dos or reminders. Try “add a to-do to renew my passport”.</span>';
        frag.appendChild(empty);
      }
    } else {
      // ---- schedule, grouped by day
      if (d && (events.length || d.feeds_connected !== false)) {
        var sec = el('section', 'ag-sec ag-sched');
        sec.setAttribute('aria-label', 'Schedule');
        var groups = [
          { label: 'Today', date: now, list: events.filter(function (e) { return e.start < tomorrow0; }) },
          { label: 'Tomorrow', date: tomorrow0, list: events.filter(function (e) { return e.start >= tomorrow0 && e.start < after0; }) },
          { label: 'Later', date: null, list: events.filter(function (e) { return e.start >= after0; }) }
        ];
        var next = events.filter(function (e) { return !e.allDay && e.start > now; })[0] || null;
        groups.forEach(function (g, gi) {
          if (!g.list.length && gi > 0) return;
          sec.appendChild(agHead(g.label, g.date ? shortDay(g.date) : ''));
          if (!g.list.length) { sec.appendChild(el('p', 'ag-none', 'Nothing else today.')); return; }
          var ul = el('ul', 'ag-list');
          g.list.slice(0, 12).forEach(function (e) { ul.appendChild(eventRow(e, now, e === next)); });
          sec.appendChild(ul);
          if (g.list.length > 12) sec.appendChild(el('p', 'ag-none', '+' + (g.list.length - 12) + ' more'));
        });
        if (!events.filter(function (e) { return e.start >= tomorrow0 && e.start < after0; }).length) sec.appendChild(el('p', 'ag-none ag-tmr', 'Tomorrow is clear so far.'));
        frag.appendChild(sec);
      }
      // ---- to-dos
      if (tasks.length) {
        var ts = el('section', 'ag-sec ag-tasks');
        ts.setAttribute('aria-label', 'To-do list');
        var over = tasks.filter(function (t) { return t.overdue; }).length;
        ts.appendChild(agHead('To-do', openTasks + ' open' + (over ? ' · ' + over + ' overdue' : '')));
        var tl = el('ul', 'ag-list');
        tasks.slice(0, 10).forEach(function (t) { tl.appendChild(taskRow(t)); });
        ts.appendChild(tl);
        if (openTasks > Math.min(10, tasks.length)) {
          var more = el('button', 'ag-more', '+' + (openTasks - Math.min(10, tasks.length)) + ' more — show the full list');
          more.type = 'button';
          more.addEventListener('click', function () { askAbout('Show me my full to-do list.'); });
          ts.appendChild(more);
        }
        frag.appendChild(ts);
      }
      // ---- reminders
      if (rems.length) {
        var rs = el('section', 'ag-sec ag-rems');
        rs.setAttribute('aria-label', 'Reminders');
        rs.appendChild(agHead('Reminders', String(reminders.length)));
        var rl = el('ul', 'ag-list');
        rems.forEach(function (r) { rl.appendChild(reminderRow(r)); });
        rs.appendChild(rl);
        frag.appendChild(rs);
      }
    }
    $('#agenda-count').textContent = String(events.length + openTasks + reminders.length);
    body.replaceChildren(frag);
  }

  function loadAgenda() {
    var seq = ++AG.seq;
    return api('/api/agenda?days=2').then(function (d) {
      if (seq !== AG.seq) return;
      if (!isObj(d)) throw new Error('Unexpected answer from the agenda feed');
      AG.data = d; AG.err = ''; AG.at = Date.now();
      panelStatus('panel-agenda', 'live');
    }).catch(function (e) {
      if (seq !== AG.seq) return;
      AG.err = errText(e);
      panelStatus('panel-agenda', 'offline');
    }).then(function () {
      if (seq !== AG.seq) return;
      AG.tried = true;
      renderAgenda();
    });
  }
  function loadReminders() {
    return api('/api/reminders').then(function (d) {
      if (!d || d.error || !Array.isArray(d.reminders)) throw new Error((d && typeof d.error === 'string' && d.error) || 'No data received.');
      reminders = d.reminders.filter(function (r) { return isObj(r) && (typeof r.message === 'string' || typeof r.due === 'string') && r.fired !== true; })
        .sort(function (a, b) { var x = parseDue(a.due), y = parseDue(b.due); return (x ? +x : Infinity) - (y ? +y : Infinity); });
      AG.remErr = '';
    }).catch(function (e) {
      AG.remErr = errText(e);
    }).then(function () { safely('agenda render', renderAgenda); });
  }
  function pollDue() {
    if (S.expired) return;
    api('/api/reminders/due').then(function (d) {
      var due = d && Array.isArray(d.due) ? d.due : [];
      if (!due.length) return;
      due.forEach(function (r) {
        if (!isObj(r)) return;
        var msg = str(r.message, 500) || 'Reminder';
        V.chime('reminder');
        if (core) core.pulse(1.2);
        systemLine(msg, 'warn', 'REMINDER');
        announce('Reminder: ' + msg);
        if (S.voice.speak) speaker.say('Reminder: ' + msg);
        try {
          if ('Notification' in window && Notification.permission === 'granted') {
            new Notification(assistantName() + ' — Reminder', { body: msg, tag: 'atlas-' + (r.id || msg), silent: true });
          }
        } catch (e) { /* ignore */ }
      });
      loadReminders();
    }).catch(function () { /* polled again shortly */ });
  }

  // ------------------------------------------------------------------ news
  var CAT_LABELS = { top: 'Top', world: 'World', us: 'U.S.', business: 'Business', technology: 'Tech', science: 'Science', health: 'Health', sports: 'Sports', entertainment: 'Culture', hackernews: 'HN' };
  var builtCats = '';
  function buildNewsTabs(cats) {
    var list = Array.isArray(cats) && cats.length ? cats.slice() : ['top', 'world', 'us', 'business', 'technology', 'science'];
    ['top', 'world', 'business', 'technology', 'science'].forEach(function (c) { if (list.indexOf(c) === -1) list.push(c); });
    var key = list.join(',');
    if (key === builtCats) return;
    builtCats = key;
    if (list.indexOf(S.newsCat) === -1) S.newsCat = 'top';
    var bar = $('#news-tabs');
    bar.innerHTML = '';
    list.forEach(function (c) {
      var b = el('button', 'tab', esc(CAT_LABELS[c] || c));
      b.type = 'button';
      b.setAttribute('role', 'tab');
      b.dataset.cat = c;
      b.id = 'tab-' + c;
      b.setAttribute('aria-controls', 'news-body');
      bar.appendChild(b);
    });
    paintTabs();
  }
  function paintTabs() {
    $$('#news-tabs .tab').forEach(function (b) {
      var on = b.dataset.cat === S.newsCat;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
      if (on) $('#news-body').setAttribute('aria-labelledby', b.id);
    });
  }
  $('#news-tabs').addEventListener('click', function (e) {
    var b = e.target.closest('.tab');
    if (!b) return;
    selectCat(b.dataset.cat, true);
  });
  $('#news-tabs').addEventListener('keydown', function (e) {
    var tabs = $$('#news-tabs .tab');
    var i = tabs.findIndex(function (t) { return t.dataset.cat === S.newsCat; });
    var n = null;
    if (e.key === 'ArrowRight') n = (i + 1) % tabs.length;
    else if (e.key === 'ArrowLeft') n = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === 'Home') n = 0;
    else if (e.key === 'End') n = tabs.length - 1;
    if (n == null) return;
    e.preventDefault();
    selectCat(tabs[n].dataset.cat, true);
    tabs[n].focus();
  });
  function selectCat(cat, user) {
    if (!cat) return;
    S.newsCat = cat;
    store.set(KEYS.news, cat);
    paintTabs();
    var tab = $('#tab-' + cat);
    if (tab && user && tab.scrollIntoView) { try { tab.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) { /* ignore */ } }
    var cached = S.newsCache[cat];
    if (cached && Date.now() - cached.at < 5 * 60000) renderNews(cached.data);
    else loadNews();
  }
  function renderNews(d) {
    var body = $('#news-body');
    var arts = (d && Array.isArray(d.articles) ? d.articles : []).filter(function (a) { return a && a.title; });
    if (!arts.length) {
      body.replaceChildren(d && d.error ? offlineBlock('FEED OFFLINE', d.error, loadNews) : offlineBlock('NO STORIES', 'Nothing in this section right now.', loadNews, 'empty'));
      return;
    }
    var ol = el('ol', 'news-list');
    arts.slice(0, 14).forEach(function (a, i) {
      var li = el('li', 'news-item');
      var link = safeUrl(a.link);
      li.innerHTML = '<button type="button" class="news-main" aria-label="Ask about: ' + esc(a.title) + '">' +
        '<span class="news-idx">' + pad2(i + 1) + '</span>' +
        '<span class="news-text"><span class="news-title">' + esc(a.title) + '</span>' +
        '<span class="news-meta">' + esc(a.source || 'Unknown') + (a.age ? '<i aria-hidden="true">·</i>' + esc(a.age) : '') + '</span>' +
        (a.summary ? '<span class="news-sum">' + esc(clip(a.summary, 220)) + '</span>' : '') + '</span></button>' +
        (link ? '<a class="news-link" href="' + esc(link) + '" target="_blank" rel="noopener noreferrer" aria-label="Open article in a new tab"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/></svg></a>' : '');
      li.querySelector('.news-main').addEventListener('click', function () {
        sendMessage('Tell me more about: ' + a.title, 'text');
        if (document.body.dataset.view !== 'core' && isPhone()) setView('core');
      });
      ol.appendChild(li);
    });
    var shown = Math.min(arts.length, 14);
    var srcs = {};
    arts.slice(0, 14).forEach(function (a) { if (a.source) srcs[a.source] = 1; });
    var nSrc = Object.keys(srcs).length;
    var foot = el('p', 'news-end', '<span>End of feed</span><span>' + shown + (shown === 1 ? ' story' : ' stories') +
      (nSrc ? ' · ' + nSrc + (nSrc === 1 ? ' source' : ' sources') : '') + ' · ' + esc(fmtHM(new Date())) + '</span>');
    body.replaceChildren(ol, foot);
    body.scrollTop = 0;
  }
  var newsSeq = 0;
  function loadNews() {
    var cat = S.newsCat;
    var seq = ++newsSeq;
    if (!S.newsCache[cat]) $('#news-body').innerHTML = '<div class="skeleton list"><i></i><i></i><i></i><i></i><i></i></div>';
    return api('/api/headlines?category=' + encodeURIComponent(cat)).then(function (d) {
      if (seq !== newsSeq) return;
      var ok = d && Array.isArray(d.articles) && d.articles.length && !d.error;
      if (ok) S.newsCache[cat] = { at: Date.now(), data: d };
      renderNews(d || {});
      panelStatus('panel-news', ok ? 'live' : 'offline');
    }).catch(function (e) {
      if (seq !== newsSeq) return;
      panelStatus('panel-news', 'offline');
      $('#news-body').replaceChildren(offlineBlock('FEED OFFLINE', errText(e), loadNews));
    });
  }

  // ------------------------------------------------------------------ markets ticker
  function fmtPrice(p) {
    var n = num(p);
    if (n == null) return '—';
    var abs = Math.abs(n);
    var dec = abs >= 100000 ? 0 : abs >= 10 ? 2 : abs >= 1 ? 2 : 4;
    return n.toLocaleString(undefined, { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }
  function sparkSvg(arr, up) {
    var a = (Array.isArray(arr) ? arr : []).map(num).filter(function (x) { return x != null; });
    if (a.length < 2) return '';
    var min = Math.min.apply(null, a), max = Math.max.apply(null, a), span = max - min || 1;
    var pts = a.map(function (v, i) { return (i / (a.length - 1) * 40).toFixed(1) + ',' + (13 - (v - min) / span * 11).toFixed(1); }).join(' ');
    return '<svg class="tk-spark ' + (up ? 'up' : 'down') + '" viewBox="0 0 40 14" preserveAspectRatio="none" aria-hidden="true"><polyline points="' + pts + '"/></svg>';
  }
  function tickerItem(q, i) {
    var ch = num(q.change_pct);
    var up = ch == null ? true : ch >= 0;
    var cls = ch == null ? 'flat' : up ? 'up' : 'down';
    return '<span class="tk ' + cls + '" data-i="' + i + '"><span class="tk-name">' + esc(q.name || q.symbol || '') + '</span>' +
      '<span class="tk-price">' + esc(fmtPrice(q.price)) + '</span>' +
      '<span class="tk-chg">' + tickerChange(ch) + '</span>' + sparkSvg(q.spark, up) + '</span>';
  }
  function tickerChange(ch) { return ch == null ? '—' : (ch >= 0 ? '▲ ' : '▼ ') + Math.abs(ch).toFixed(2) + '%'; }
  function tickerSig(quotes) { return quotes.map(function (q) { return String(q.symbol || q.name || ''); }).join('|'); }
  var tickerState = { sig: '', quotes: null };

  /* Same instruments as last time: rewrite the numbers inside every copy of the tape and leave the
   * marquee animation alone, so it keeps gliding instead of jumping back to the start. */
  function updateTickerInPlace(quotes) {
    var track = $('#ticker-track');
    quotes.forEach(function (q, i) {
      var ch = num(q.change_pct);
      var up = ch == null ? true : ch >= 0;
      var cls = 'tk ' + (ch == null ? 'flat' : up ? 'up' : 'down');
      var price = fmtPrice(q.price);
      var chg = tickerChange(ch);
      var spark = sparkSvg(q.spark, up);
      $$('.tk[data-i="' + i + '"]', track).forEach(function (n) {
        if (n.className !== cls) n.className = cls;
        var pe = n.querySelector('.tk-price'), ce = n.querySelector('.tk-chg');
        if (pe && pe.textContent !== price) pe.textContent = price;
        if (ce && ce.textContent !== chg) ce.textContent = chg;
        var old = n.querySelector('.tk-spark');
        if (old) old.remove();
        if (spark) n.insertAdjacentHTML('beforeend', spark);
      });
    });
    // Price widths can shift a few pixels; keep the loop seamless without restarting it.
    requestAnimationFrame(function () {
      var g = track.firstElementChild;
      if (!g || !track.classList.contains('run')) return;
      var w = g.getBoundingClientRect().width;
      var copies = Math.max(1, Math.round(parseFloat(track.style.getPropertyValue('--tk-dist')) / (w || 1)) || 1);
      var dist = w * copies;
      if (w && Math.abs(dist - parseFloat(track.style.getPropertyValue('--tk-dist') || '0')) > 1) track.style.setProperty('--tk-dist', dist + 'px');
    });
  }

  function renderTicker(quotes, force) {
    var track = $('#ticker-track');
    var vp = $('#ticker-viewport');
    var sig = tickerSig(quotes);
    var hasTape = !!track.querySelector('.tk-group');
    tickerState.quotes = quotes;
    if (!force && hasTape && sig === tickerState.sig) { updateTickerInPlace(quotes); return; }
    tickerState.sig = sig;
    var items = quotes.map(tickerItem).join('<span class="tk-sep" aria-hidden="true">◆</span>');
    var group = '<span class="tk-group">' + items + '<span class="tk-sep" aria-hidden="true">◆</span></span>';
    track.innerHTML = group;
    track.classList.remove('run');
    // Repeat the group until one copy is wider than the viewport, then double it for a seamless loop.
    requestAnimationFrame(function () {
      var g = track.firstElementChild;
      if (!g) return;
      var w = g.getBoundingClientRect().width || 1;
      var copies = Math.max(1, Math.ceil(vp.clientWidth / w));
      var half = new Array(copies + 1).join(group);
      track.innerHTML = half + half.replace(/<span class="tk-group">/g, '<span class="tk-group" aria-hidden="true">');
      var dist = w * copies;
      track.style.setProperty('--tk-dist', dist + 'px');
      track.style.setProperty('--tk-dur', Math.max(20, dist / 42) + 's');
      track.classList.add('run');
    });
  }
  function loadMarkets() {
    return api('/api/markets').then(function (d) {
      var q = d && Array.isArray(d.quotes) ? d.quotes.filter(function (x) { return x && (x.name || x.symbol); }) : [];
      if (!q.length) {
        tickerState.sig = '';
        $('#ticker').dataset.status = 'offline';
        $('#ticker-track').classList.remove('run');
        $('#ticker-track').innerHTML = '<span class="ticker-msg off">MARKET FEED OFFLINE' + (d && d.error ? ' — ' + esc(clip(d.error, 90)) : '') + ' · RETRYING</span>';
        return;
      }
      $('#ticker').dataset.status = 'live';
      renderTicker(q);
    }).catch(function (e) {
      tickerState.sig = '';
      $('#ticker').dataset.status = 'offline';
      $('#ticker-track').classList.remove('run');
      $('#ticker-track').innerHTML = '<span class="ticker-msg off">MARKET FEED OFFLINE — ' + esc(clip(errText(e), 90)) + '</span>';
    });
  }

  // ------------------------------------------------------------------ clock
  var dtf = null;
  try { dtf = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }); } catch (e) { dtf = null; }
  function tickClock() {
    var now = new Date();
    var html;
    if (dtf && dtf.formatToParts) {
      html = dtf.formatToParts(now).map(function (p) {
        return p.type === 'dayPeriod' ? '<small>' + esc(p.value) + '</small>' : esc(p.value);
      }).join('');
    } else {
      html = pad2(now.getHours()) + ':' + pad2(now.getMinutes()) + ':' + pad2(now.getSeconds());
    }
    $('#clock-time').innerHTML = html;
    var date = now.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' }).toUpperCase();
    $('#clock-date').textContent = date;
    $('#eg-date').textContent = date;
    paintHero(now);
    var up = Math.floor((Date.now() - S.sessionStart) / 1000);
    $('#session-uptime').textContent = pad2(Math.floor(up / 3600)) + ':' + pad2(Math.floor(up / 60) % 60) + ':' + pad2(up % 60);
  }

  // ------------------------------------------------------------------ settings drawer
  var drawer = $('#settings');
  var scrim = $('#scrim');
  var setForm = $('#settings-form');
  var lastFocus = null;
  function isDrawerOpen() { return !drawer.hidden; }

  // Credentials are never sent to the browser and never prefilled: a blank field keeps the current value.
  var SECRET_KEYS = ['OPENROUTER_API_KEY', 'EMAIL_PASSWORD', 'TWILIO_AUTH_TOKEN', 'HOME_ASSISTANT_TOKEN', 'CALENDAR_ICS_URLS'];
  // Where a field's current value lives in /api/state; every other key is read from state.extra.
  var STATE_FIELD = {
    ASSISTANT_NAME: 'assistant_name', USER_NAME: 'user_name', USER_ADDRESS_AS: 'address_as', USER_PHONE: 'user_phone',
    HOME_ADDRESS: 'home_address', WORK_ADDRESS: 'work_address', HOME_CITY: 'home_city', UNITS: 'units',
    NEWS_REGION: 'news_region', NEWS_LANGUAGE: 'news_language', ATLAS_MODEL: 'model', ATLAS_SEARCH_MODEL: 'search_model',
    EMAIL_ADDRESS: 'email_address', SMTP_HOST: 'smtp_host', SMTP_PORT: 'smtp_port', IMAP_HOST: 'imap_host', IMAP_PORT: 'imap_port'
  };
  var SELECT_DEFAULT = { UNITS: 'imperial', CALENDAR_PROVIDER: 'google' }; // what a blank setting means on the server
  // Checked only when the field is filled in; blank always means "use the default".
  var FIELD_RULES = {
    SMTP_PORT: [/^\d{1,5}$/, 'Mail server ports are whole numbers, e.g. 465.'],
    IMAP_PORT: [/^\d{1,5}$/, 'Mail server ports are whole numbers, e.g. 993.'],
    CALENDAR_DAY_HOURS: [/^\d{1,2}(:[0-5]\d)?\s*-\s*\d{1,2}(:[0-5]\d)?$/, '“Your day” should look like 08:00-20:00.'],
    DEFAULT_COUNTRY_CODE: [/^(\+|00)?\s*[1-9]\d{0,2}$/, 'The country code should look like +1 or +44.'],
    HOME_ASSISTANT_URL: [/^https?:\/\/[^\s/?#]+[^\s]*$/i, 'The Home Assistant URL should start with http:// or https://.'],
    TWILIO_ACCOUNT_SID: [/^AC[0-9a-f]{32}$/i, 'A Twilio account SID is 34 characters and starts with AC.']
  };
  var EYE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/></svg>';

  function isSecretField(f) { return f.hasAttribute('data-secret') || SECRET_KEYS.indexOf(f.name) !== -1; }
  function truthy(v) { return /^(1|true|yes|on)$/i.test(String(v == null ? '' : v).trim()); }
  function settingValue(st, key) {
    var v = STATE_FIELD[key] ? st[STATE_FIELD[key]] : (isObj(st.extra) ? st.extra[key] : null);
    return v == null || typeof v === 'object' ? '' : String(v);
  }
  function settingsFields() { return $$('[name]', setForm).filter(function (f) { return f.name && !f.disabled; }); }
  function fieldValue(f) {
    if (f.type === 'checkbox') return f.checked ? 'true' : 'false';
    var v = String(f.value || '').trim();
    if (f.name === 'NEWS_REGION') return v.toUpperCase();
    if (f.name === 'NEWS_LANGUAGE') return v.toLowerCase();
    return v;
  }
  function setField(f, value) {
    if (f.type === 'checkbox') { f.checked = truthy(value); f.dataset.orig = fieldValue(f); return; }
    var v = value == null ? '' : String(value);
    if (f.tagName === 'SELECT' && v && !$$('option', f).some(function (o) { return o.value === v; })) {
      var o = document.createElement('option'); // keep an unusual saved value instead of silently replacing it
      o.value = v; o.textContent = v;
      f.appendChild(o);
    }
    f.value = v;
    f.dataset.orig = fieldValue(f); // what the field showed: only edits are sent on save
  }
  function fieldChanged(f) {
    if (isSecretField(f)) return fieldValue(f) !== '';
    return f.dataset.orig == null || fieldValue(f) !== f.dataset.orig;
  }
  function markDirty() {
    $$('.set-sec', setForm).forEach(function (sec) {
      sec.classList.toggle('dirty', $$('[name]', sec).some(function (f) { return !f.disabled && fieldChanged(f); }));
    });
  }
  function sectionBadge(key, text, on, warn) {
    var b = setForm.querySelector('[data-badge="' + key + '"]');
    if (!b) return;
    b.textContent = text || '';
    b.hidden = !text;
    b.classList.toggle('on', !!on);
    b.classList.toggle('warn', !on && !!warn);
  }
  function paintBadges(st) {
    var sc = isObj(st.secrets_configured) ? st.secrets_configured : {};
    var ex = isObj(st.extra) ? st.extra : {};
    function has(k) { return sc[k] === true || (k === 'OPENROUTER_API_KEY' && st.has_key === true) || (k === 'EMAIL_PASSWORD' && st.email_configured === true); }
    $$('.cfg[data-cfg]', setForm).forEach(function (b) {
      var on = has(b.dataset.cfg);
      b.textContent = on ? 'Configured · blank = keep' : 'Not set';
      b.classList.toggle('on', on);
    });
    $('#key-hint').textContent = st.key_hint ? String(st.key_hint) : '';
    $('#email-note').textContent = '';
    sectionBadge('voice', !speaker.supported ? 'Text only' : !S.voice.speak ? 'Muted' : S.voice.wake ? 'Wake word' : 'Voice on', speaker.supported && S.voice.speak);
    sectionBadge('you', st.user_name ? clip(String(st.user_name), 18) : 'Not set', !!st.user_name);
    sectionBadge('model', st.has_key === false ? 'No key' : shortModel(st.model), st.has_key !== false);
    sectionBadge('email', st.email_configured ? 'Connected' : 'Off', !!st.email_configured);
    sectionBadge('calendar', has('CALENDAR_ICS_URLS') ? 'Connected' : 'Not connected', has('CALENDAR_ICS_URLS'));
    var tw = [!!ex.TWILIO_ACCOUNT_SID, has('TWILIO_AUTH_TOKEN'), !!ex.TWILIO_FROM_NUMBER].filter(Boolean).length;
    var mac = /^macos/i.test(String(st.os || ''));
    sectionBadge('comms', tw === 3 ? 'Twilio' : mac ? 'Messages app' : tw ? 'Incomplete' : 'Off', tw === 3 || mac, tw > 0 && tw < 3 && !mac);
    var ha = [!!ex.HOME_ASSISTANT_URL, has('HOME_ASSISTANT_TOKEN')].filter(Boolean).length;
    sectionBadge('home', ha === 2 ? 'Connected' : ha ? 'Incomplete' : 'Off', ha === 2, ha === 1);
    var ch = { chrome: 'Chrome', msedge: 'Edge', chromium: 'Chromium' }[String(ex.BROWSER_CHANNEL || '').toLowerCase()];
    sectionBadge('browser', truthy(ex.BROWSER_HEADLESS) ? 'Headless' : ex.BROWSER_EXECUTABLE ? 'Custom path' : ch || 'Auto', true);
  }

  function openDrawer(opts) {
    if (!isObj(opts) || (typeof Event === 'function' && opts instanceof Event)) opts = {};
    if (!isDrawerOpen()) {
      lastFocus = document.activeElement;
      drawer.hidden = false;
      scrim.hidden = false;
      requestAnimationFrame(function () { drawer.classList.add('open'); scrim.classList.add('open'); });
      $('#settings-btn').setAttribute('aria-expanded', 'true');
      populateSettings();
    }
    var sec = opts.section ? $('#sec-' + opts.section) : null;
    if (sec) sec.open = true;
    setTimeout(function () {
      var f = opts.focus ? setForm.querySelector('[name="' + opts.focus + '"]') : null;
      if (!f && sec) f = sec.querySelector('summary');
      if (!f) f = setForm.querySelector('.set-sec[open] .set-body input, .set-sec[open] .set-body select') || setForm.querySelector('summary');
      if (sec) { try { sec.scrollIntoView({ block: 'start', behavior: reduceMotion ? 'auto' : 'smooth' }); } catch (e) { /* ignore */ } }
      if (f) { try { f.focus({ preventScroll: !!sec }); } catch (e) { /* ignore */ } }
    }, sec ? 120 : 60);
  }
  function closeDrawer() {
    drawer.classList.remove('open');
    scrim.classList.remove('open');
    $('#settings-btn').setAttribute('aria-expanded', 'false');
    setTimeout(function () { drawer.hidden = true; scrim.hidden = true; }, reduceMotion ? 0 : 280);
    if (lastFocus && lastFocus.focus && document.contains(lastFocus)) lastFocus.focus();
  }
  $('#settings-btn').addEventListener('click', function () { openDrawer(); });
  $('#settings-close').addEventListener('click', closeDrawer);
  $('#settings-cancel').addEventListener('click', closeDrawer);
  scrim.addEventListener('click', closeDrawer);
  drawer.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab') return;
    var f = $$('button, input, select, textarea, a[href], summary', drawer).filter(function (x) { return !x.disabled && x.offsetParent !== null; });
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  setForm.addEventListener('input', function (e) { if (e.target && e.target.removeAttribute) e.target.removeAttribute('aria-invalid'); markDirty(); });
  setForm.addEventListener('change', markDirty);
  $$('.reveal', setForm).forEach(function (b) {
    b.innerHTML = EYE;
    b.title = 'Show or hide what you typed';
    b.addEventListener('click', function () {
      var f = document.getElementById(b.getAttribute('aria-controls'));
      if (!f) return;
      var show = f.type === 'password';
      f.type = show ? 'text' : 'password';
      b.setAttribute('aria-pressed', show ? 'true' : 'false');
    });
  });

  function populateSettings() {
    var st = S.st || {};
    settingsFields().forEach(function (f) {
      if (isSecretField(f)) {
        f.value = '';
        f.type = 'password';
        f.removeAttribute('aria-invalid');
        return;
      }
      var v = settingValue(st, f.name);
      if (!v && SELECT_DEFAULT[f.name]) v = SELECT_DEFAULT[f.name];
      setField(f, v);
      f.removeAttribute('aria-invalid');
    });
    $$('.reveal', setForm).forEach(function (b) { b.setAttribute('aria-pressed', 'false'); });
    var wl = setForm.querySelector('[name="MARKET_WATCHLIST"]');
    if (wl && Array.isArray(st.watchlist) && st.watchlist.length) wl.placeholder = st.watchlist.slice(0, 12).join(',');
    paintBadges(st);
    $('#rate-input').value = String(S.voice.rate || 1);
    $('#pitch-input').value = String(S.voice.pitch || 1);
    $('#rate-out').textContent = Number(S.voice.rate || 1).toFixed(2);
    $('#pitch-out').textContent = Number(S.voice.pitch || 1).toFixed(2);
    $('#speak-toggle').checked = !!S.voice.speak;
    $('#wake-toggle').checked = !!S.voice.wake;
    $('#wake-toggle').disabled = !listener.supported;
    var sup = [];
    if (!speaker.supported) sup.push('Speech output is not available in this browser.');
    if (!listener.supported) sup.push('Speech recognition is not available here — use Chrome or Edge for voice input and the wake word.');
    $('#voice-support').textContent = sup.join(' ');
    $('#voice-support').hidden = !sup.length;
    markDirty();
    fillVoices();
    loadModels();
    loadMemory();
  }

  function fillVoices() {
    var sel = $('#voice-select');
    if (!sel) return;
    var vs = speaker.voices();
    var def = speaker.defaultVoice();
    var opts = '<option value="">Automatic' + (def ? ' — ' + esc(def.name) : '') + '</option>';
    var sorted = vs.slice().sort(function (a, b) {
      var ra = /en[-_]GB/i.test(a.lang) ? 0 : /^en/i.test(a.lang) ? 1 : 2;
      var rb = /en[-_]GB/i.test(b.lang) ? 0 : /^en/i.test(b.lang) ? 1 : 2;
      return ra - rb || String(a.name).localeCompare(String(b.name));
    });
    sorted.forEach(function (v) { opts += '<option value="' + esc(v.voiceURI) + '">' + esc(v.name) + ' — ' + esc(v.lang) + '</option>'; });
    sel.innerHTML = opts;
    sel.value = S.voice.voiceURI || '';
    if (sel.value !== (S.voice.voiceURI || '')) sel.value = '';
    sel.disabled = !speaker.supported;
  }

  function fmtUsd(n) { var v = num(n); if (v == null) return '?'; return v === 0 ? '0' : v < 0.1 ? v.toFixed(3) : v < 10 ? v.toFixed(2) : v.toFixed(0); }
  function loadModels() {
    var sel = $('#model-select');
    var note = $('#model-note');
    var cur = (S.st && S.st.model) || '';
    note.textContent = 'loading…';
    var chosen = sel.value; // keep a choice made while the list was loading
    api('/api/models').then(function (d) {
      var models = d && Array.isArray(d.models) ? d.models : [];
      var html = '<option value="">Automatic (recommended)' + (d && d.current && !cur ? ' — ' + esc(d.current) : '') + '</option>';
      var seen = false;
      models.forEach(function (m) {
        if (!m || !m.id) return;
        if (m.id === cur) seen = true;
        var price = (m.prompt_price != null || m.completion_price != null) ? ' — $' + fmtUsd(m.prompt_price) + ' / $' + fmtUsd(m.completion_price) + ' per M' : '';
        html += '<option value="' + esc(m.id) + '">' + esc(m.name || m.id) + price + (m.vision ? ' · vision' : '') + '</option>';
      });
      if (cur && !seen) html += '<option value="' + esc(cur) + '">' + esc(cur) + '</option>';
      var pick = sel.value !== chosen ? sel.value : chosen;
      sel.innerHTML = html;
      sel.value = pick;
      if (sel.value !== pick) sel.value = cur;
      $('#model-list').innerHTML = models.map(function (m) { return m && m.id ? '<option value="' + esc(m.id) + '">' : ''; }).join('');
      note.textContent = d && d.error ? 'offline — ' + clip(d.error, 60) : models.length + ' tool-capable · $ in / out per M tokens';
    }).catch(function (e) {
      note.textContent = 'unavailable — ' + clip(errText(e), 60);
      if (cur && !$$('option', sel).some(function (o) { return o.value === cur; })) { sel.innerHTML = '<option value="">Automatic (recommended)</option><option value="' + esc(cur) + '">' + esc(cur) + '</option>'; sel.value = cur; }
    });
  }

  function loadMemory() {
    var ul = $('#memory-list');
    api('/api/memory').then(function (d) {
      var mems = d && Array.isArray(d.memories) ? d.memories : [];
      sectionBadge('memory', mems.length ? mems.length + (mems.length === 1 ? ' fact' : ' facts') : 'Empty', mems.length > 0);
      if (!mems.length) { ul.innerHTML = '<li class="muted">Nothing remembered yet. Say “remember that…” to add facts.</li>'; return; }
      ul.innerHTML = '';
      mems.slice().reverse().forEach(function (m) {
        var li = el('li', 'mem');
        li.innerHTML = '<span class="mem-fact">' + esc(m.fact || '') + '</span><span class="mem-date">' + esc(String(m.saved || '').slice(0, 10)) + '</span>';
        var b = el('button', 'icon-btn icon-btn-sm', '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M10 7V4.5h4V7M7 7l1 13h8l1-13"/></svg>');
        b.type = 'button';
        b.setAttribute('aria-label', 'Forget: ' + clip(m.fact || '', 80));
        b.addEventListener('click', function () {
          b.disabled = true;
          api('/api/memory/' + encodeURIComponent(m.id), { method: 'DELETE' }).then(function () {
            li.classList.add('out');
            setTimeout(function () { li.remove(); if (!ul.children.length) loadMemory(); }, 220);
          }).catch(function (e) { b.disabled = false; toast('Couldn’t delete that memory: ' + errText(e)); });
        });
        li.appendChild(b);
        ul.appendChild(li);
      });
    }).catch(function (e) {
      ul.innerHTML = '<li class="muted">Memory unavailable — ' + esc(errText(e)) + '</li>';
    });
  }

  // voice controls apply instantly (stored locally)
  $('#voice-select').addEventListener('change', function (e) { S.voice.voiceURI = e.target.value; speaker.voiceURI = e.target.value; saveVoice(); updateVoiceReadouts(); });
  $('#rate-input').addEventListener('input', function (e) { S.voice.rate = Number(e.target.value) || 1; speaker.rate = S.voice.rate; $('#rate-out').textContent = S.voice.rate.toFixed(2); saveVoice(); });
  $('#pitch-input').addEventListener('input', function (e) { S.voice.pitch = Number(e.target.value) || 1; speaker.pitch = S.voice.pitch; $('#pitch-out').textContent = S.voice.pitch.toFixed(2); saveVoice(); });
  function setSpeak(on) {
    S.voice.speak = !!on;
    speaker.enabled = S.voice.speak;
    if (!S.voice.speak) speaker.stop();
    saveVoice();
    var t = $('#speak-toggle');
    if (t) t.checked = S.voice.speak;
    updateVoiceReadouts();
  }
  $('#speak-toggle').addEventListener('change', function (e) { setSpeak(e.target.checked); });
  $('#mute-btn').addEventListener('click', function () {
    if (!speaker.supported) { toast('Speech output isn’t available in this browser.', { tone: 'info', key: 'tts-na', cooldown: 5000 }); return; }
    setSpeak(!S.voice.speak);
    toast(S.voice.speak ? 'Spoken replies on.' : 'Spoken replies muted.', { tone: 'info', key: 'mute', cooldown: 800, ms: 1800 });
  });
  $('#wake-toggle').addEventListener('change', function (e) {
    S.voice.wake = e.target.checked && listener.supported; saveVoice();
    if (S.engaged) listener.setWake(S.voice.wake);
    updateVoiceReadouts(); refreshState();
  });
  $('#voice-test').addEventListener('click', function () {
    V.unlockAudio();
    if (!speaker.supported) { toast('Speech output isn’t available in this browser.', { tone: 'info' }); return; }
    var was = speaker.enabled; speaker.enabled = true;
    speaker.say('Voice check. ' + assistantName() + ' standing by.', true);
    speaker.enabled = was;
  });
  $('#clear-convo').addEventListener('click', function () {
    if (S.streaming) { toast('Wait for the current reply to finish, or press Esc.', { tone: 'warn' }); return; }
    clearConversation();
  });

  setForm.addEventListener('submit', function (e) {
    e.preventDefault();
    var values = {};
    var bad = null;
    settingsFields().forEach(function (f) {
      if (!fieldChanged(f)) return; // unchanged fields and blank secrets are never sent
      var v = fieldValue(f);
      var rule = FIELD_RULES[f.name];
      if (!bad && v && rule && !rule[0].test(v)) bad = { f: f, msg: rule[1] };
      values[f.name] = v;
    });
    if (bad) {
      var sec = bad.f.closest('.set-sec');
      if (sec) sec.open = true;
      bad.f.setAttribute('aria-invalid', 'true');
      try { bad.f.focus(); } catch (x) { /* ignore */ }
      toast(bad.msg, { tone: 'warn' });
      return;
    }
    if (!Object.keys(values).length) { toast('No changes to save.', { tone: 'info' }); closeDrawer(); return; }
    var btn = $('#settings-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    api('/api/setup', { method: 'POST', body: { values: values } }).then(function () {
      return api('/api/state');
    }).then(function (st) {
      $$('[data-secret]', setForm).forEach(function (f) { f.value = ''; f.type = 'password'; });
      S.model = '';
      applyState(st);
      setModel((st && st.model) || '');
      updateVoiceReadouts();
      var n = Object.keys(values).length;
      toast('Configuration saved (' + n + (n === 1 ? ' setting' : ' settings') + ').', { tone: 'ok' });
      closeDrawer();
      loadWeather(); S.newsCache = {}; loadNews(); loadMarkets(); loadAgenda();
    }).catch(function (err) {
      toast('Couldn’t save settings: ' + errText(err));
    }).then(function () {
      btn.disabled = false;
      btn.textContent = 'Save configuration';
    });
  });

  // ------------------------------------------------------------------ setup overlay
  var setupResolve = null;
  function showSetup() {
    var ov = $('#setup');
    ov.hidden = false;
    requestAnimationFrame(function () { ov.classList.add('open'); });
    setTimeout(function () { var f = ov.querySelector('input'); if (f) f.focus(); }, 80);
    return new Promise(function (res) { setupResolve = res; });
  }
  $('#setup-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var f = e.target;
    var key = f.OPENROUTER_API_KEY.value.trim();
    var errEl = $('#setup-error');
    if (!key) { errEl.textContent = 'An OpenRouter API key is required.'; errEl.hidden = false; return; }
    var values = { OPENROUTER_API_KEY: key };
    if (f.USER_NAME.value.trim()) values.USER_NAME = f.USER_NAME.value.trim();
    if (f.HOME_CITY.value.trim()) values.HOME_CITY = f.HOME_CITY.value.trim();
    var btn = f.querySelector('button[type="submit"]');
    btn.disabled = true;
    errEl.hidden = true;
    api('/api/setup', { method: 'POST', body: { values: values } }).then(function () {
      return api('/api/state');
    }).then(function (st) {
      applyState(st);
      if (!st.has_key) throw new Error('The key was not saved.');
      var ov = $('#setup');
      ov.classList.remove('open');
      setTimeout(function () { ov.hidden = true; }, 300);
      f.OPENROUTER_API_KEY.value = '';
      loadWeather();
      if (setupResolve) { setupResolve(true); setupResolve = null; }
    }).catch(function (err) {
      errEl.textContent = 'Setup failed: ' + errText(err);
      errEl.hidden = false;
    }).then(function () { btn.disabled = false; });
  });

  // ------------------------------------------------------------------ mobile views
  function isPhone() { try { return window.matchMedia('(max-width: 760px)').matches; } catch (e) { return false; } }
  function setView(v) {
    document.body.dataset.view = v;
    $$('.mobile-tabs button').forEach(function (b) {
      if (b.dataset.view === v) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    if (v === 'core') requestAnimationFrame(function () { if (core) core.resize(); scrollToEnd(true); });
    var main = $('#layout');
    if (main) main.scrollTop = 0;
  }
  $$('.mobile-tabs button').forEach(function (b) { b.addEventListener('click', function () { setView(b.dataset.view); }); });

  // ------------------------------------------------------------------ engage + boot
  var engageCore = null;
  try {
    engageCore = new HUD.Core($('#engage-canvas'), { state: 'dormant', booted: true, wings: false, labels: true });
    engageCore.start();
  } catch (e) { engageCore = null; }
  if (core) core.stop(); // main core sleeps behind the engage screen

  function greetingText() {
    var who = userAddress();
    return 'Good ' + partOfDay(new Date().getHours()) + (who ? ', ' + who : '') + '. All systems online.';
  }

  function bootLines(memCount) {
    var st = S.st || {};
    return [
      ['CORE MATRIX', 'ONLINE', 'ok'],
      ['SECURE UPLINK', TOKEN && TOKEN.indexOf('{{') === -1 ? 'VERIFIED' : 'NO TOKEN', TOKEN && TOKEN.indexOf('{{') === -1 ? 'ok' : 'warn'],
      ['LANGUAGE MODEL', st.has_key === false ? 'KEY REQUIRED' : shortModel(st.model).toUpperCase(), st.has_key === false ? 'warn' : 'ok'],
      ['SPEECH SYNTHESIS', speaker.supported ? 'READY' : 'UNAVAILABLE', speaker.supported ? 'ok' : 'warn'],
      ['SPEECH RECOGNITION', listener.supported ? 'READY' : 'TEXT ONLY', listener.supported ? 'ok' : 'warn'],
      ['FEEDS · NEWS · MARKETS', 'SYNCING', 'ok'],
      ['LONG-TERM MEMORY', memCount == null ? 'STANDBY' : memCount + ' RECORDS', 'ok']
    ];
  }

  function engage() {
    if (S.engaged) return;
    S.engaged = true;
    V.unlockAudio();
    V.chime('engage');
    try {
      if ('Notification' in window && Notification.permission === 'default') {
        var pr = Notification.requestPermission();
        if (pr && pr.catch) pr.catch(function () {});
      }
    } catch (e) { /* ignore */ }
    if (engageCore) { engageCore.setState('standby'); engageCore.pulse(1.2); }

    var ov = $('#engage');
    ov.classList.add('booting');
    $('#engage-action').hidden = true;
    var log = $('#boot-log');
    log.hidden = false;
    var memP = api('/api/memory').then(function (d) { return d && Array.isArray(d.memories) ? d.memories.length : null; }).catch(function () { return null; });
    var stP = S.st ? Promise.resolve(S.st) : loadState();

    Promise.all([memP, stP, sleep(120)]).then(function (r) {
      var lines = bootLines(r[0]);
      var step = reduceMotion ? 40 : 170;
      return lines.reduce(function (p, ln, i) {
        return p.then(function () {
          var li = el('li', 'boot-ln ' + ln[2]);
          li.innerHTML = '<span class="bl-t">' + (i * 0.17).toFixed(2).padStart(5, '0') + '</span><span class="bl-k">' + esc(ln[0]) +
            '</span><span class="bl-dots" aria-hidden="true"></span><span class="bl-v">' + esc(ln[1]) + '</span>';
          log.appendChild(li);
          if (engageCore) engageCore.pulse(0.5);
          return sleep(step);
        });
      }, Promise.resolve());
    }).then(function () {
      var fin = el('li', 'boot-ln final', '<span class="bl-k">ALL SYSTEMS NOMINAL</span>');
      log.appendChild(fin);
      return sleep(reduceMotion ? 50 : 260);
    }).then(function () {
      document.body.classList.remove('is-locked');
      setInert(false);
      ov.classList.add('leaving');
      if (core) { core.start(); core.powerUp(); core.resize(); }
      return sleep(reduceMotion ? 0 : 650);
    }).then(function () {
      ov.hidden = true;
      if (engageCore) { engageCore.destroy(); engageCore = null; }
      if (S.st && S.st.has_key === false) return showSetup();
      return true;
    }).then(function () {
      var g = greetingText();
      if (S.transcript.length) systemLine(g, 'info', assistantName().toUpperCase(), true);
      else paintHero(new Date());
      if (S.voice.speak) speaker.say(g);
      if (S.voice.wake && listener.supported) setTimeout(function () { listener.setWake(true); refreshState(); }, 400);
      input.focus({ preventScroll: true });
      refreshState();
    }).catch(function (e) {
      // never strand the user behind the overlay
      document.body.classList.remove('is-locked');
      setInert(false);
      ov.hidden = true;
      if (core) { core.start(); core.powerUp(); }
      if (window.console) console.warn('engage failed', e);
    });
  }
  $('#engage-btn').addEventListener('click', engage);

  // ------------------------------------------------------------------ boot
  $$('.panel').forEach(function (p) { p.insertAdjacentHTML('beforeend', '<i class="brk" aria-hidden="true"></i>'); });
  var lockables = ['.topbar', '#layout', '#ticker', '.mobile-tabs'];
  function setInert(on) { lockables.forEach(function (s) { var n = $(s); if (n) { if (on) n.setAttribute('inert', ''); else n.removeAttribute('inert'); } }); }
  setInert(true);

  function every(ms, fn, skipHidden) {
    return setInterval(function () { if (skipHidden && document.hidden) return; if (!S.expired) safely(fn.name || 'poll', fn); }, ms);
  }

  // Each boot step is isolated: a failure in one (bad stored data, a missing API) never stops
  // the clock, the panels or the polling loops from starting.
  function safely(label, fn) {
    try { return fn(); } catch (e) { if (window.console) console.warn('ATLAS boot step failed: ' + label, e); return null; }
  }
  safely('transcript', restoreTranscript);
  safely('voice readouts', updateVoiceReadouts);
  safely('usage', renderUsage);
  safely('clock', tickClock);
  setInterval(function () { if (!document.hidden) safely('clock', tickClock); }, 1000);
  safely('state', refreshState);

  safely('load', function () {
    loadState().then(function () { loadNews(); });
    loadWeather();
    loadSystem();
    loadAgenda();
    loadReminders();
    loadMarkets();
  });

  every(5000, loadSystem, true);
  every(10 * 60000, loadWeather, false);
  every(60000, loadReminders, false);
  every(5 * 60000, loadAgenda, false);
  every(30000, function () { if (AG.tried) renderAgenda(); }, true); // relative times, events that just ended
  every(10000, pollDue, false);
  every(5 * 60000, function () { S.newsCache = {}; loadNews(); }, true);
  every(60000, loadMarkets, true);

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) {
      tickClock();
      if (S.engaged) loadSystem();
      if (AG.tried && Date.now() - AG.at > 5 * 60000) loadAgenda();
    }
  });
  var resizeT = 0;
  window.addEventListener('resize', function () {
    clearTimeout(resizeT);
    resizeT = setTimeout(function () {
      if ($('#ticker').dataset.status === 'live' && tickerState.quotes) renderTicker(tickerState.quotes, true);
    }, 400);
  });

  setTimeout(function () { var b = $('#engage-btn'); if (b && !S.engaged) b.focus({ preventScroll: true }); }, 50);

  // Expose a tiny debug hook (no secrets).
  window.ATLAS_UI = { state: S, core: core, send: sendMessage, setView: setView, parseSSE: drainSSE, refresh: { markets: loadMarkets, news: loadNews, weather: loadWeather, system: loadSystem, reminders: loadReminders, agenda: loadAgenda }, openSettings: openDrawer };
})();
