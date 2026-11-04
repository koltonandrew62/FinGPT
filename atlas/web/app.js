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
  function safeUrl(u) { return typeof u === 'string' && /^https?:\/\//i.test(u) ? u : null; }

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
    set: function (k, v) {
      try { window.localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; }
    },
    del: function (k) { try { window.localStorage.removeItem(k); } catch (e) { /* ignore */ } }
  };

  // ------------------------------------------------------------------ app state
  var S = {
    st: null,
    engaged: false,
    streaming: false,
    abort: null,
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
    newsCat: store.get(KEYS.news, 'top'),
    newsCache: {},
    cpuHist: [],
    stick: true,
    voice: Object.assign({ voiceURI: '', rate: 1, pitch: 1, speak: true, wake: false }, store.get(KEYS.voice, {}) || {}),
    sessionStart: Date.now()
  };

  var hist = store.get(KEYS.history, []);
  S.history = Array.isArray(hist) ? hist.filter(function (m) { return m && typeof m === 'object' && m.role; }) : [];
  var trn = store.get(KEYS.transcript, []);
  S.transcript = Array.isArray(trn) ? trn.filter(function (e) { return e && typeof e === 'object' && e.k; }) : [];

  function saveHistory() {
    if (S.history.length > 120) {
      S.history = S.history.slice(-120);
      while (S.history.length && S.history[0].role !== 'user') S.history.shift();
    }
    if (!store.set(KEYS.history, S.history)) {
      S.history = S.history.slice(-30);
      while (S.history.length && S.history[0].role !== 'user') S.history.shift();
      store.set(KEYS.history, S.history);
    }
  }
  function saveTranscript() {
    if (S.transcript.length > 150) S.transcript = S.transcript.slice(-150);
    var clean = S.transcript.filter(function (e) { return !e.ephemeral; });
    if (!store.set(KEYS.transcript, clean)) store.set(KEYS.transcript, clean.slice(-30));
  }
  function saveVoice() { store.set(KEYS.voice, S.voice); }

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
    $('#ro-link').textContent = 'SESSION EXPIRED';
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
      onGeometry: function (g) { coreWrap.style.setProperty('--core-r', g.R.toFixed(1) + 'px'); }
    });
    core.start();
  } catch (e) { core = null; }

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
    if (mode === 'command') mic.start(); else if (!mode || mode === 'wake') mic.stop();
    refreshState();
  };
  listener.onwake = function () {
    S.armed = true;
    V.chime('wake');
    if (core) core.pulse(1);
    refreshState();
    setTimeout(function () { S.armed = false; refreshState(); }, 8000);
  };
  listener.onheard = function (text) {
    if (S.pendingConfirm) return approvalFromPhrase(text) || true;
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

  function micLoop() {
    if (core && mic.on) { var lv = mic.levels(); if (lv) core.setInput(lv); }
    requestAnimationFrame(micLoop);
  }
  requestAnimationFrame(micLoop);

  function startListening() {
    if (!listener.supported) {
      toast('Voice input isn’t supported in this browser — try Chrome or Edge. You can still type.', { tone: 'info', key: 'sr-unsupported' });
      return;
    }
    if (S.speaking) speaker.stop();
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
    if (st === 'thinking') label = S.pendingConfirm ? 'AWAITING AUTH' : S.toolsRunning > 0 ? 'EXECUTING' : 'PROCESSING';
    if (st === 'standby' && S.listening === 'wake') label = 'STANDBY · WAKE';
    $('#status-text').textContent = label;
    $('#core-state').textContent = CORE_TEXT[label] || label;
    var micBtn = $('#mic-btn');
    var on = S.listening === 'command';
    micBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    micBtn.setAttribute('aria-label', on ? 'Stop voice input' : 'Start voice input');
    micBtn.classList.toggle('on', on);
    micBtn.classList.toggle('passive', S.listening === 'wake');
    document.body.classList.toggle('is-streaming', S.streaming);
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
  function setModel(m) {
    S.model = m || '';
    $('#tb-model').textContent = shortModel(S.model);
    $('#tb-model').title = S.model || 'Automatic';
    $('#s-model').textContent = S.model || 'Automatic';
  }
  function renderUsage() {
    var total = S.usage.prompt + S.usage.completion;
    $('#tb-tokens').textContent = fmtTokens(total);
    $('#tb-cost').textContent = '$' + S.usage.cost.toFixed(4);
    $('#s-prompt').textContent = fmtTokens(S.usage.prompt);
    $('#s-completion').textContent = fmtTokens(S.usage.completion);
    $('#s-cost').textContent = '$' + S.usage.cost.toFixed(4);
    $('#s-turns').textContent = String(S.turns);
  }
  function updateVoiceReadouts() {
    $('#ro-in').textContent = listener.supported ? (S.voice.wake ? 'WAKE WORD' : 'PUSH-TO-TALK') : 'TEXT ONLY';
    var v = speaker.supported ? speaker.currentVoice() : null;
    $('#ro-out').textContent = !speaker.supported ? 'UNAVAILABLE' : !S.voice.speak ? 'MUTED' : v ? clip(v.name.replace(/^(Microsoft|Google)\s+/i, ''), 22).toUpperCase() : 'SYSTEM';
    var micBtn = $('#mic-btn');
    if (!listener.supported) {
      micBtn.setAttribute('aria-disabled', 'true');
      micBtn.title = 'Voice input isn’t supported in this browser';
      micBtn.classList.add('unsupported');
    }
  }

  // ------------------------------------------------------------------ transcript
  var tx = $('#transcript');
  var partEls = new WeakMap();
  var renderQueue = new Set();
  var renderRaf = 0;

  function fmtClock(ts) {
    var d = new Date(ts || Date.now());
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function nearBottom() { return tx.scrollHeight - tx.scrollTop - tx.clientHeight < 48; }
  function scrollToEnd(force) {
    if (force || S.stick) {
      tx.scrollTop = tx.scrollHeight;
      $('#jump-latest').hidden = true;
    } else {
      $('#jump-latest').hidden = false;
    }
  }
  tx.addEventListener('scroll', function () {
    S.stick = nearBottom();
    if (S.stick) $('#jump-latest').hidden = true;
  }, { passive: true });
  $('#jump-latest').addEventListener('click', function () { S.stick = true; scrollToEnd(true); });

  function setConvoMode() {
    var has = S.transcript.some(function (e) { return e.k === 'u' || e.k === 'a'; });
    document.body.classList.toggle('has-convo', has);
    $('#transcript-empty').hidden = has;
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
      node.innerHTML = MD.render(p.text || '');
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
      'app', 'name', 'url', 'path', 'message', 'fact', 'action', 'keys', 'text'];
    for (var i = 0; i < keys.length; i++) {
      var v = args[keys[i]];
      if (v == null || v === '') continue;
      if (Array.isArray(v)) v = v.join(', ');
      if (typeof v === 'object') continue;
      return clip(String(v).replace(/\s+/g, ' '), 42);
    }
    return '';
  }

  function renderTool(it) {
    var c = el('span', 'tool');
    c.dataset.state = it.state || 'run';
    c.dataset.id = it.id || '';
    var hint = it.hint ? '<span class="tool-hint">' + esc(it.hint) + '</span>' : '';
    c.innerHTML = '<i class="tool-ico" aria-hidden="true"></i><span class="tool-label">' + esc(it.label || it.name || 'Tool') + '</span>' + hint +
      '<span class="tool-sum">' + esc(it.summary || '') + '</span>';
    c.setAttribute('role', 'status');
    c.title = (it.label || it.name || '') + (it.hint ? ' — ' + it.hint : '') + (it.summary ? ' · ' + it.summary : '');
    return c;
  }

  function renderConfirm(p, entry) {
    var card = el('div', 'confirm');
    card.dataset.state = p.result === true ? 'approved' : p.result === false ? 'denied' : p.expired ? 'expired' : 'pending';
    var fields = (Array.isArray(p.fields) ? p.fields : []).filter(function (f) { return Array.isArray(f) && f.length >= 2; });
    var grid = fields.length ? '<dl class="confirm-fields">' + fields.map(function (f) {
      return '<div><dt>' + esc(f[0]) + '</dt><dd>' + esc(f[1]) + '</dd></div>';
    }).join('') + '</dl>' : '';
    var body = p.body ? '<pre class="confirm-body' + (p.code ? ' code' : '') + '">' + esc(p.body) + '</pre>' : '';
    card.innerHTML =
      '<div class="confirm-head"><span class="confirm-idx">AUTH // ' + esc(String(p.name || 'action').toUpperCase()) + '</span>' +
      '<span class="confirm-flag" aria-live="polite"></span></div>' +
      '<h4 class="confirm-title">' + esc(p.title || 'Approve this action?') + '</h4>' + grid + body +
      '<div class="confirm-actions">' +
      '<button type="button" class="btn btn-primary btn-sm" data-act="approve"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7"/></svg>Approve</button>' +
      '<button type="button" class="btn btn-danger btn-sm" data-act="deny"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>Deny</button>' +
      '<span class="confirm-hint">' + (listener.supported ? 'or say “approve” / “deny”' : 'or type “yes” / “no”') + '</span></div>' +
      '<div class="confirm-stamp" aria-hidden="true"></div>';
    var flag = card.querySelector('.confirm-flag');
    var stamp = card.querySelector('.confirm-stamp');
    function paint() {
      var s = card.dataset.state;
      flag.textContent = s === 'pending' ? 'AWAITING APPROVAL' : s === 'approved' ? 'APPROVED' : s === 'denied' ? 'DENIED' : 'EXPIRED';
      stamp.textContent = s === 'pending' ? '' : flag.textContent;
      $$('button', card).forEach(function (b) { b.disabled = s !== 'pending'; });
    }
    paint();
    card._paint = paint;
    $$('button[data-act]', card).forEach(function (b) {
      b.addEventListener('click', function () { resolveConfirm(p, b.dataset.act === 'approve'); });
    });
    return card;
  }

  function lockConfirm(p, state) {
    var card = partEls.get(p);
    if (state === 'approved') p.result = true;
    else if (state === 'denied') p.result = false;
    else p.expired = true;
    if (card) { card.dataset.state = state; if (card._paint) card._paint(); }
  }

  function resolveConfirm(p, approved) {
    if (!p || p.result != null || p.expired || p.sending) return;
    p.sending = true;
    var card = partEls.get(p);
    if (card) $$('button', card).forEach(function (b) { b.disabled = true; });
    api('/api/confirm/' + encodeURIComponent(p.id), { method: 'POST', body: { approved: !!approved } })
      .then(function () {
        lockConfirm(p, approved ? 'approved' : 'denied');
        V.chime(approved ? 'ok' : 'error');
      })
      .catch(function (e) {
        if (e.status === 404) { lockConfirm(p, 'expired'); toast('That approval request has expired.', { tone: 'warn' }); }
        else if (e.status !== 401) { toast('Couldn’t send your decision: ' + e.message); if (card) $$('button', card).forEach(function (b) { b.disabled = false; }); }
      })
      .then(function () {
        p.sending = false;
        if (p.result != null || p.expired) { if (S.pendingConfirm === p) S.pendingConfirm = null; refreshState(); saveTranscript(); }
      });
  }

  var YES = /\b(yes|yeah|yep|yup|approve[d]?|confirm(ed)?|send it|do it|go ahead|affirmative|proceed|run it|ok(ay)?)\b/i;
  var NO = /\b(no|nope|nah|deny|denied|decline|cancel|stop|don'?t|do not|negative|abort|reject)\b/i;
  function approvalFromPhrase(text) {
    var p = S.pendingConfirm;
    if (!p) return false;
    var t = String(text || '').trim();
    if (NO.test(t)) { resolveConfirm(p, false); return true; }
    if (YES.test(t)) { resolveConfirm(p, true); return true; }
    return false;
  }

  function appendEntry(entry) {
    S.transcript.push(entry);
    var node = entry.k === 'u' ? renderUser(entry) : entry.k === 's' ? renderSysLine(entry) : renderAssistantShell(entry);
    node.classList.add('enter');
    tx.appendChild(node);
    setConvoMode();
    scrollToEnd(entry.k === 'u');
    return node;
  }

  function systemLine(text, tone, tag, ephemeral) {
    var entry = { k: 's', text: text, tone: tone || 'info', tag: tag || 'SYSTEM', ts: Date.now(), ephemeral: !!ephemeral };
    var stick = nearBottom();
    appendEntry(entry);
    if (stick) scrollToEnd(true);
    if (!ephemeral) saveTranscript();
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
    if (renderRaf) return;
    renderRaf = requestAnimationFrame(function () {
      renderRaf = 0;
      var stick = S.stick;
      renderQueue.forEach(function (q) {
        var n = partEls.get(q);
        if (n) n.innerHTML = MD.render(q.text);
      });
      renderQueue.clear();
      if (stick) scrollToEnd(true); else $('#jump-latest').hidden = false;
    });
  }

  function restoreTranscript() {
    tx.querySelectorAll('.msg, .msg-sys').forEach(function (n) { n.remove(); });
    S.transcript.forEach(function (entry) {
      if (entry.k === 'a') {
        (entry.parts || []).forEach(function (p) {
          if (p.t === 'confirm' && p.result == null) p.expired = true;
          if (p.t === 'tools') (p.items || []).forEach(function (it) { if (it.state === 'run') { it.state = 'err'; it.summary = it.summary || 'interrupted'; } });
        });
      }
      var node = entry.k === 'u' ? renderUser(entry) : entry.k === 's' ? renderSysLine(entry) : renderAssistantShell(entry);
      tx.appendChild(node);
    });
    // Older sessions with history but no transcript: rebuild from the API history.
    if (!S.transcript.length && S.history.length) {
      S.history.forEach(function (m) {
        if (m.role === 'user' && typeof m.content === 'string') {
          var u = { k: 'u', text: m.content, ts: Date.now() };
          S.transcript.push(u); tx.appendChild(renderUser(u));
        } else if (m.role === 'assistant' && typeof m.content === 'string' && m.content) {
          var a = { k: 'a', parts: [{ t: 'text', text: m.content }], ts: Date.now() };
          S.transcript.push(a); tx.appendChild(renderAssistantShell(a));
        }
      });
    }
    setConvoMode();
    S.stick = true;
    requestAnimationFrame(function () { scrollToEnd(true); });
  }

  function clearConversation() {
    stopAll();
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

  function stopAll() {
    speaker.stop();
    if (S.abort) { try { S.abort.abort(); } catch (e) { /* ignore */ } }
  }

  function sendMessage(raw, via) {
    var text = String(raw || '').trim();
    if (!text) return;
    if (S.expired) { toast('Session expired — reload the page', { key: 'expired', cooldown: 5000 }); return; }
    if (S.pendingConfirm) {
      if (!approvalFromPhrase(text)) toast('Approve or deny the pending action first.', { tone: 'warn', key: 'pending', cooldown: 4000 });
      return;
    }
    if (S.streaming) { toast('Still working on the last request — press Esc to interrupt.', { tone: 'info', key: 'busy', cooldown: 4000 }); return; }
    if (S.st && S.st.has_key === false) { showSetup(); return; }

    speaker.stop();
    S.lastVia = via || 'text';
    S.stick = true;
    appendEntry({ k: 'u', text: text, ts: Date.now(), via: S.lastVia });
    S.history.push({ role: 'user', content: text });
    saveHistory();
    saveTranscript();
    runTurn();
  }

  function runTurn() {
    var entry = { k: 'a', parts: [], ts: Date.now(), model: S.model };
    var shell = appendEntry(entry);
    shell.classList.add('pending');
    var cur = null;       // current text part
    var tools = null;     // current tools group
    var toolIndex = {};   // id -> item
    var gotDone = false;
    var t0 = performance.now();
    var first = true;

    S.streaming = true;
    S.toolsRunning = 0;
    refreshState();
    var ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    S.abort = ctrl;

    function firstSignal() {
      if (!first) return;
      first = false;
      shell.classList.remove('pending');
      $('#ro-lat').textContent = Math.round(performance.now() - t0) + ' ms';
    }
    function closeText() {
      if (cur) { var n = partEls.get(cur); if (n) n.classList.remove('streaming'); }
      cur = null;
    }

    function onEvent(ev) {
      switch (ev.type) {
        case 'model':
          if (ev.model) {
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
          if (S.voice.speak) speaker.feed(ev.text);
          break;
        case 'segment_end':
          closeText();
          if (S.voice.speak) speaker.flush();
          break;
        case 'tool':
          firstSignal();
          if (ev.state === 'start') {
            closeText();
            if (!tools) { tools = { t: 'tools', items: [] }; addPart(entry, tools); }
            var it = { id: ev.id, name: ev.name, label: ev.label || ev.name, hint: toolHint(ev.args), state: 'run', summary: '' };
            tools.items.push(it);
            toolIndex[ev.id] = { item: it, group: tools };
            var gnode = partEls.get(tools);
            if (gnode) gnode.appendChild(renderTool(it));
            S.toolsRunning++;
          } else {
            var ref = toolIndex[ev.id];
            if (ref) {
              ref.item.state = ev.state === 'done' ? 'ok' : 'err';
              ref.item.summary = ev.summary || (ev.state === 'done' ? 'done' : 'failed');
              var g = partEls.get(ref.group);
              var chip = g && g.querySelector('.tool[data-id="' + (window.CSS && CSS.escape ? CSS.escape(String(ev.id)) : String(ev.id).replace(/"/g, '')) + '"]');
              if (chip) chip.replaceWith(renderTool(ref.item));
            }
            S.toolsRunning = Math.max(0, S.toolsRunning - 1);
          }
          scrollToEnd(false);
          refreshState();
          break;
        case 'confirm':
          firstSignal();
          closeText();
          tools = null;
          var cp = { t: 'confirm', id: ev.id, call_id: ev.call_id, name: ev.name, title: ev.title, fields: ev.fields, body: ev.body, code: !!ev.code, result: null };
          addPart(entry, cp);
          S.pendingConfirm = cp;
          S.awaitVoiceAnswer = S.lastVia !== 'text' || S.voice.wake;
          V.chime('wake');
          if (S.voice.speak && speaker.supported) speaker.say('Awaiting your approval.');
          else if (S.awaitVoiceAnswer && listener.supported) { S.awaitVoiceAnswer = false; setTimeout(startListening, 300); }
          scrollToEnd(true);
          saveTranscript();
          refreshState();
          var btn = partEls.get(cp) && partEls.get(cp).querySelector('[data-act="approve"]');
          if (btn && S.lastVia === 'text' && document.activeElement === $('#cmd-input')) { /* keep typing focus */ }
          break;
        case 'confirm_result':
          var target = S.pendingConfirm && S.pendingConfirm.id === ev.id ? S.pendingConfirm :
            entry.parts.filter(function (x) { return x.t === 'confirm' && x.id === ev.id; })[0];
          if (target) lockConfirm(target, ev.approved ? 'approved' : 'denied');
          if (S.pendingConfirm && S.pendingConfirm.id === ev.id) S.pendingConfirm = null;
          S.awaitVoiceAnswer = false;
          if (S.listening === 'command') listener.stop();
          refreshState();
          break;
        case 'usage':
          S.usage.prompt += num(ev.prompt_tokens) || 0;
          S.usage.completion += num(ev.completion_tokens) || 0;
          S.usage.cost += num(ev.cost) || 0;
          renderUsage();
          break;
        case 'refresh':
          if (ev.panel === 'reminders') loadReminders();
          if (ev.panel === 'memory' && isDrawerOpen()) loadMemory();
          break;
        case 'error':
          firstSignal();
          closeText();
          addPart(entry, { t: 'error', text: String(ev.text || 'Something went wrong.') });
          toast(String(ev.text || 'Something went wrong.'));
          V.chime('error');
          errorFlash();
          scrollToEnd(false);
          break;
        case 'done':
          gotDone = true;
          if (Array.isArray(ev.messages)) {
            ev.messages.forEach(function (m) { if (m && typeof m === 'object') S.history.push(m); });
          }
          S.turns++;
          renderUsage();
          saveHistory();
          break;
        default:
          break;
      }
    }

    var req = api('/api/chat', { method: 'POST', body: { messages: S.history }, signal: ctrl ? ctrl.signal : undefined, raw: true });
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
      closeText();
      if (S.voice.speak) speaker.flush();
      // Keep strict alternation when the turn ended without a 'done' (abort / failure).
      if (!gotDone) {
        var partial = entry.parts.filter(function (p) { return p.t === 'text'; }).map(function (p) { return p.text; }).join('\n\n').trim();
        if (partial) S.history.push({ role: 'assistant', content: partial });
        saveHistory();
      }
      if (S.pendingConfirm) { lockConfirm(S.pendingConfirm, 'expired'); S.pendingConfirm = null; }
      entry.parts.forEach(function (p) {
        if (p.t === 'tools') p.items.forEach(function (it) {
          if (it.state === 'run') { it.state = 'err'; it.summary = 'interrupted'; var g = partEls.get(p); if (g) { var c = g.querySelector('.tool[data-id="' + String(it.id).replace(/"/g, '') + '"]'); if (c) c.replaceWith(renderTool(it)); } }
        });
      });
      if (!entry.parts.length) { addPart(entry, { t: 'note', text: 'No response.' }); }
      S.streaming = false;
      S.abort = null;
      S.toolsRunning = 0;
      saveTranscript();
      refreshState();
      scrollToEnd(false);
    });
  }

  // ------------------------------------------------------------------ command bar
  var form = $('#cmdbar');
  var input = $('#cmd-input');
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (S.streaming && !S.pendingConfirm && !input.value.trim()) { stopAll(); return; }
    if (S.streaming && !S.pendingConfirm && input.value.trim()) {
      toast('Still working — press Esc or the stop button to interrupt.', { tone: 'info', key: 'busy', cooldown: 4000 });
      return;
    }
    var v = input.value;
    input.value = '';
    input.classList.remove('interim');
    sendMessage(v, 'text');
  });
  $('#send-btn').addEventListener('click', function (e) {
    if (S.streaming && !input.value.trim()) { e.preventDefault(); stopAll(); }
  });
  $('#mic-btn').addEventListener('click', function () { toggleListening(); });
  $('#core').addEventListener('click', function () {
    if (S.speaking) { speaker.stop(); return; }
    if (S.streaming) { stopAll(); return; }
    toggleListening();
  });

  var QUICK = {
    briefing: 'Give me my daily briefing: local weather, top headlines across world, US, business and tech, a markets snapshot, my unread email count if email is set up, and upcoming reminders. Keep it tight.',
    headlines: 'What are the top headlines right now?',
    markets: 'Give me a quick markets snapshot.',
    inbox: 'Check my inbox — how many unread emails do I have, and is anything important?',
    system: 'Run a quick system check on this computer.'
  };
  $$('#chips .chip').forEach(function (c) {
    c.addEventListener('click', function () { var q = QUICK[c.dataset.action]; if (q) sendMessage(q, 'text'); });
  });

  // ------------------------------------------------------------------ keyboard
  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    var typing = /INPUT|TEXTAREA|SELECT/.test(tag) || (e.target && e.target.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && (e.code === 'Space' || e.key === ' ')) {
      if (!S.engaged) return;
      e.preventDefault();
      toggleListening();
      return;
    }
    if (e.key === 'Escape') {
      if (!$('#settings').hidden) { closeDrawer(); return; }
      if (S.listening === 'command') listener.stop(true);
      if (S.speaking || S.streaming) { stopAll(); e.preventDefault(); }
      return;
    }
    if (e.key === '/' && !typing && S.engaged && $('#settings').hidden) { e.preventDefault(); input.focus(); }
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
  function offlineBlock(title, detail, retry) {
    var d = el('div', 'offline');
    d.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-5 3 10 2-5h7"/><path d="M3 3l18 18" class="slash"/></svg>' +
      '<b>' + esc(title) + '</b><span>' + esc(clip(detail || 'No data received.', 140)) + '</span>';
    if (retry) {
      var b = el('button', 'btn btn-ghost btn-xs', 'Retry');
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
      html += '<ol class="wx-days">' + days.map(function (f, i) {
        var dt = f.date ? new Date(String(f.date) + 'T12:00:00') : null;
        var dn = i === 0 ? 'TODAY' : dt && !isNaN(dt) ? dt.toLocaleDateString(undefined, { weekday: 'short' }).toUpperCase() : '—';
        var pc = Math.max(0, Math.min(100, num(f.precip_chance) || 0));
        return '<li><span class="wd-name">' + esc(dn) + '</span>' + wxSvg(wxKind(null, f.condition, true), 'wx-ico wx-ico-sm') +
          '<span class="wd-temp"><b>' + round(f.high) + '°</b><i>' + round(f.low) + '°</i></span>' +
          '<span class="wd-pp" title="' + pc + '% chance of precipitation"><i style="width:' + pc + '%"></i></span>' +
          '<span class="wd-ppv">' + pc + '%</span></li>';
      }).join('') + '</ol>';
    }
    html += '</div>';
    body.innerHTML = html;
  }

  function loadWeather() {
    return api('/api/weather').then(function (d) {
      if (!d || d.error || !d.current) {
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
      '<div><dt>Cores</dt><dd data-m="cores">—</dd></div><div><dt>OS</dt><dd data-m="os">—</dd></div></dl></div>';
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
    setMeter('cpu', cpu, cpu == null ? '—' : cpu.toFixed(1) + '%', '');
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
    meta.querySelector('[data-m="cores"]').textContent = d.cpu_cores != null ? String(d.cpu_cores) : '—';
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

  // ------------------------------------------------------------------ reminders
  var reminders = [];
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
  function dueLabel(d) {
    var now = new Date();
    var tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
    var hm = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
    if (d.toDateString() === now.toDateString()) return 'Today ' + hm;
    if (d.toDateString() === tomorrow.toDateString()) return 'Tomorrow ' + hm;
    return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' }) + ' ' + hm;
  }
  function renderReminders() {
    var body = $('#reminders-body');
    $('#reminder-count').textContent = String(reminders.length);
    if (!reminders.length) {
      body.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="13" r="7.5"/><path d="M12 9v4l2.5 2M9 2.5h6"/></svg>' +
        '<b>NO ACTIVE REMINDERS</b><span>Try “remind me to stretch in 20 minutes”.</span></div>';
      return;
    }
    var ul = el('ul', 'rem-list');
    reminders.slice(0, 20).forEach(function (r) {
      var d = parseDue(r.due);
      var li = el('li', 'rem');
      var soon = d && d - Date.now() < 10 * 60000;
      if (soon) li.classList.add('soon');
      li.innerHTML = '<span class="rem-time"><b>' + esc(d ? relTime(d) : '—') + '</b><i>' + esc(d ? dueLabel(d) : String(r.due || '')) + '</i></span>' +
        '<span class="rem-msg">' + esc(r.message || '') + '</span>';
      ul.appendChild(li);
    });
    body.replaceChildren(ul);
  }
  function loadReminders() {
    return api('/api/reminders').then(function (d) {
      if (!d || d.error || !Array.isArray(d.reminders)) {
        panelStatus('panel-reminders', 'offline');
        $('#reminders-body').replaceChildren(offlineBlock('REMINDERS OFFLINE', d && d.error, loadReminders));
        return;
      }
      reminders = d.reminders;
      renderReminders();
      panelStatus('panel-reminders', 'live');
    }).catch(function (e) {
      panelStatus('panel-reminders', 'offline');
      $('#reminders-body').replaceChildren(offlineBlock('REMINDERS OFFLINE', errText(e), loadReminders));
    });
  }
  function pollDue() {
    if (S.expired) return;
    api('/api/reminders/due').then(function (d) {
      var due = d && Array.isArray(d.due) ? d.due : [];
      if (!due.length) return;
      due.forEach(function (r) {
        var msg = String(r.message || 'Reminder');
        V.chime('reminder');
        if (core) core.pulse(1.2);
        systemLine(msg, 'warn', 'REMINDER');
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
      body.replaceChildren(offlineBlock(d && d.error ? 'FEED OFFLINE' : 'NO STORIES', (d && d.error) || 'Nothing in this section right now.', loadNews));
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
    body.replaceChildren(ol);
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
  function renderTicker(quotes) {
    var track = $('#ticker-track');
    var vp = $('#ticker-viewport');
    var items = quotes.map(function (q) {
      var ch = num(q.change_pct);
      var up = ch == null ? true : ch >= 0;
      var cls = ch == null ? 'flat' : up ? 'up' : 'down';
      return '<span class="tk ' + cls + '"><span class="tk-name">' + esc(q.name || q.symbol || '') + '</span>' +
        '<span class="tk-price">' + fmtPrice(q.price) + '</span>' +
        '<span class="tk-chg">' + (ch == null ? '—' : (up ? '▲ ' : '▼ ') + Math.abs(ch).toFixed(2) + '%') + '</span>' + sparkSvg(q.spark, up) + '</span>';
    }).join('<span class="tk-sep" aria-hidden="true">◆</span>');
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
        $('#ticker').dataset.status = 'offline';
        $('#ticker-track').classList.remove('run');
        $('#ticker-track').innerHTML = '<span class="ticker-msg off">MARKET FEED OFFLINE' + (d && d.error ? ' — ' + esc(clip(d.error, 90)) : '') + ' · RETRYING</span>';
        return;
      }
      $('#ticker').dataset.status = 'live';
      renderTicker(q);
    }).catch(function (e) {
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
    var up = Math.floor((Date.now() - S.sessionStart) / 1000);
    $('#session-uptime').textContent = pad2(Math.floor(up / 3600)) + ':' + pad2(Math.floor(up / 60) % 60) + ':' + pad2(up % 60);
  }

  // ------------------------------------------------------------------ settings drawer
  var drawer = $('#settings');
  var scrim = $('#scrim');
  var lastFocus = null;
  function isDrawerOpen() { return !drawer.hidden; }
  function openDrawer() {
    lastFocus = document.activeElement;
    drawer.hidden = false;
    scrim.hidden = false;
    requestAnimationFrame(function () { drawer.classList.add('open'); scrim.classList.add('open'); });
    $('#settings-btn').setAttribute('aria-expanded', 'true');
    populateSettings();
    setTimeout(function () { var f = drawer.querySelector('.drawer-body input, .drawer-body select'); if (f) f.focus({ preventScroll: true }); }, 60);
  }
  function closeDrawer() {
    drawer.classList.remove('open');
    scrim.classList.remove('open');
    $('#settings-btn').setAttribute('aria-expanded', 'false');
    setTimeout(function () { drawer.hidden = true; scrim.hidden = true; }, reduceMotion ? 0 : 280);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }
  $('#settings-btn').addEventListener('click', openDrawer);
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

  function setField(name, value) {
    var f = drawer.querySelector('[name="' + name + '"]');
    if (f) f.value = value == null ? '' : String(value);
  }
  function populateSettings() {
    var st = S.st || {};
    setField('ASSISTANT_NAME', st.assistant_name || '');
    setField('USER_NAME', st.user_name || '');
    setField('USER_ADDRESS_AS', st.address_as || '');
    setField('HOME_CITY', st.home_city || '');
    setField('UNITS', st.units === 'metric' ? 'metric' : 'imperial');
    setField('NEWS_REGION', st.news_region || 'US');
    setField('ATLAS_SEARCH_MODEL', st.search_model || '');
    setField('EMAIL_ADDRESS', st.email_address || '');
    setField('EMAIL_PASSWORD', '');
    setField('SMTP_HOST', st.smtp_host || '');
    setField('SMTP_PORT', st.smtp_port || '');
    setField('IMAP_HOST', st.imap_host || '');
    setField('IMAP_PORT', st.imap_port || '');
    setField('OPENROUTER_API_KEY', '');
    $('#key-hint').textContent = st.key_hint ? 'current ' + st.key_hint : st.has_key ? 'key set' : 'no key set';
    $('#email-note').textContent = st.email_configured ? 'configured · blank = keep' : 'not configured';
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
      sel.innerHTML = html;
      sel.value = cur;
      $('#model-list').innerHTML = models.map(function (m) { return m && m.id ? '<option value="' + esc(m.id) + '">' : ''; }).join('');
      note.textContent = d && d.error ? 'offline — ' + clip(d.error, 60) : models.length + ' tool-capable · $ in / out per M tokens';
    }).catch(function (e) {
      note.textContent = 'unavailable — ' + clip(errText(e), 60);
      if (cur) { sel.innerHTML = '<option value="">Automatic (recommended)</option><option value="' + esc(cur) + '">' + esc(cur) + '</option>'; sel.value = cur; }
    });
  }

  function loadMemory() {
    var ul = $('#memory-list');
    api('/api/memory').then(function (d) {
      var mems = d && Array.isArray(d.memories) ? d.memories : [];
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
  $('#speak-toggle').addEventListener('change', function (e) {
    S.voice.speak = e.target.checked; speaker.enabled = S.voice.speak; if (!S.voice.speak) speaker.stop(); saveVoice(); updateVoiceReadouts();
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

  $('#settings-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var values = {};
    $$('[name]', drawer).forEach(function (f) {
      if (!f.name || f.closest('.setup')) return;
      var v = String(f.value || '').trim();
      if ((f.name === 'OPENROUTER_API_KEY' || f.name === 'EMAIL_PASSWORD') && !v) return;
      if (f.name === 'NEWS_REGION') v = v.toUpperCase();
      values[f.name] = v;
    });
    var bad = ['SMTP_PORT', 'IMAP_PORT'].filter(function (k) { return values[k] && !/^\d{1,5}$/.test(values[k]); });
    if (bad.length) { toast('Ports must be numbers (' + bad.join(', ') + ').', { tone: 'warn' }); return; }
    var btn = $('#settings-save');
    btn.disabled = true;
    btn.textContent = 'Saving…';
    api('/api/setup', { method: 'POST', body: { values: values } }).then(function () {
      return api('/api/state');
    }).then(function (st) {
      S.model = '';
      applyState(st);
      setModel(st.model || '');
      toast('Configuration saved.', { tone: 'ok' });
      closeDrawer();
      loadWeather(); S.newsCache = {}; loadNews(); loadMarkets();
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
    var h = new Date().getHours();
    var part = h < 5 ? 'evening' : h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening';
    var who = userAddress();
    return 'Good ' + part + (who ? ', ' + who : '') + '. All systems online.';
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
      systemLine(g, 'info', assistantName().toUpperCase(), true);
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
    return setInterval(function () { if (skipHidden && document.hidden) return; if (!S.expired) fn(); }, ms);
  }

  restoreTranscript();
  updateVoiceReadouts();
  renderUsage();
  tickClock();
  setInterval(tickClock, 1000);
  refreshState();

  loadState().then(function () {
    loadNews();
  });
  loadWeather();
  loadSystem();
  loadReminders();
  loadMarkets();

  every(5000, loadSystem, true);
  every(10 * 60000, loadWeather, false);
  every(60000, loadReminders, false);
  every(30000, function () { if (reminders.length) renderReminders(); }, true);
  every(10000, pollDue, false);
  every(5 * 60000, function () { S.newsCache = {}; loadNews(); }, true);
  every(60000, loadMarkets, true);

  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && S.engaged) { loadSystem(); }
  });
  var resizeT = 0;
  window.addEventListener('resize', function () {
    clearTimeout(resizeT);
    resizeT = setTimeout(function () {
      if ($('#ticker').dataset.status === 'live') loadMarkets();
    }, 400);
  });

  setTimeout(function () { var b = $('#engage-btn'); if (b && !S.engaged) b.focus({ preventScroll: true }); }, 50);

  // Expose a tiny debug hook (no secrets).
  window.ATLAS_UI = { state: S, send: sendMessage, setView: setView, parseSSE: drainSSE };
})();
