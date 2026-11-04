/* ATLAS — voice I/O.
 * Speaker: sentence-streaming speechSynthesis queue (skips fenced code, strips markdown/URLs).
 * Listener: Web Speech recognition with push-to-talk and continuous wake-word modes.
 * MicMeter: getUserMedia + AnalyserNode -> 96 normalised bar levels.
 * chime(): small WebAudio cues. Every API is optional; nothing here throws when absent.
 */
(function (global) {
  'use strict';

  var noop = function () {};
  var toSpeech = (global.AtlasMD && global.AtlasMD.toSpeech) || function (s) { return String(s || ''); };

  // ------------------------------------------------------------------ audio context
  var audioCtx = null;
  function getAudio() {
    if (audioCtx) return audioCtx;
    try {
      var AC = global.AudioContext || global.webkitAudioContext;
      if (AC) audioCtx = new AC();
    } catch (e) { audioCtx = null; }
    return audioCtx;
  }
  function unlockAudio() {
    var ac = getAudio();
    if (ac && ac.state === 'suspended' && ac.resume) { try { ac.resume().catch(noop); } catch (e) { /* ignore */ } }
    return ac;
  }

  /** Short synthesized cue: 'wake' | 'reminder' | 'ok' | 'error' | 'engage'. */
  function chime(kind) {
    var ac = unlockAudio();
    if (!ac) return;
    var seqs = {
      wake: [[880, 0, 0.09], [1320, 0.09, 0.14]],
      ok: [[1046, 0, 0.08]],
      error: [[330, 0, 0.14], [247, 0.13, 0.2]],
      reminder: [[988, 0, 0.12], [1319, 0.16, 0.12], [1760, 0.32, 0.22]],
      engage: [[523, 0, 0.18], [784, 0.12, 0.2], [1046, 0.26, 0.42]]
    };
    var seq = seqs[kind] || seqs.ok;
    try {
      var now = ac.currentTime + 0.01;
      var master = ac.createGain();
      master.gain.value = 0.16;
      master.connect(ac.destination);
      seq.forEach(function (n) {
        var o = ac.createOscillator(), g = ac.createGain();
        o.type = 'sine';
        o.frequency.setValueAtTime(n[0], now + n[1]);
        g.gain.setValueAtTime(0.0001, now + n[1]);
        g.gain.exponentialRampToValueAtTime(1, now + n[1] + 0.015);
        g.gain.exponentialRampToValueAtTime(0.0001, now + n[1] + n[2]);
        o.connect(g); g.connect(master);
        o.start(now + n[1]);
        o.stop(now + n[1] + n[2] + 0.05);
      });
      setTimeout(function () { try { master.disconnect(); } catch (e) { /* ignore */ } }, 1500);
    } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ speaker
  var PREFERRED = ['UK English Male', 'Ryan', 'Daniel', 'George', 'Arthur', 'Oliver', 'Thomas'];

  function Speaker() {
    this.supported = !!(global.speechSynthesis && typeof global.SpeechSynthesisUtterance === 'function');
    this.enabled = true;
    this.voiceURI = '';
    this.rate = 1;
    this.pitch = 1;
    this.buffer = '';
    this.inFence = false;
    this.active = 0;
    this.speaking = false;
    this.gen = 0;
    this.live = new Set();
    this.onstate = noop;
    this.onboundary = noop;
    this.onvoices = noop;
    this._idleTimer = 0;
    var self = this;
    if (this.supported) {
      try {
        var fire = function () { self.onvoices(self.voices()); };
        if (global.speechSynthesis.addEventListener) global.speechSynthesis.addEventListener('voiceschanged', fire);
        else global.speechSynthesis.onvoiceschanged = fire;
      } catch (e) { /* ignore */ }
    }
  }

  Speaker.prototype.voices = function () {
    if (!this.supported) return [];
    try { return global.speechSynthesis.getVoices() || []; } catch (e) { return []; }
  };

  Speaker.prototype.defaultVoice = function () {
    var vs = this.voices();
    if (!vs.length) return null;
    var en = vs.filter(function (v) { return /^en/i.test(v.lang || ''); });
    var gb = en.filter(function (v) { return /en[-_]GB/i.test(v.lang || ''); });
    for (var i = 0; i < PREFERRED.length; i++) {
      var name = PREFERRED[i].toLowerCase();
      var hit = gb.filter(function (v) { return (v.name || '').toLowerCase().indexOf(name) !== -1; })[0] ||
        en.filter(function (v) { return (v.name || '').toLowerCase().indexOf(name) !== -1; })[0];
      if (hit) return hit;
    }
    var male = gb.filter(function (v) { return /male/i.test(v.name) && !/female/i.test(v.name); })[0];
    return male || gb[0] || en[0] || vs[0];
  };

  Speaker.prototype.currentVoice = function () {
    var uri = this.voiceURI;
    if (uri) {
      var v = this.voices().filter(function (x) { return x.voiceURI === uri; })[0];
      if (v) return v;
    }
    return this.defaultVoice();
  };

  Speaker.prototype._setSpeaking = function (on) {
    var self = this;
    clearTimeout(this._idleTimer);
    if (on) {
      if (!this.speaking) { this.speaking = true; this.onstate(true); }
    } else {
      // debounce the gap between queued sentences
      this._idleTimer = setTimeout(function () {
        if (self.active === 0 && self.speaking) { self.speaking = false; self.onstate(false); }
      }, 180);
    }
  };

  Speaker.prototype._utter = function (text) {
    if (!this.supported || !this.enabled) return;
    var clean = toSpeech(text);
    if (!/[A-Za-z0-9]/.test(clean)) return;
    var self = this;
    var gen = this.gen;
    var u;
    try { u = new global.SpeechSynthesisUtterance(clean); } catch (e) { return; }
    var v = this.currentVoice();
    if (v) { u.voice = v; u.lang = v.lang; } else { u.lang = 'en-GB'; }
    u.rate = this.rate; u.pitch = this.pitch;
    var done = false;
    var finish = function () {
      if (done) return;
      done = true;
      clearTimeout(watchdog);
      self.live.delete(u);
      if (gen !== self.gen) return;
      self.active = Math.max(0, self.active - 1);
      if (self.active === 0) self._setSpeaking(false);
    };
    // Some engines never fire end/error (e.g. headless); don't let state stick.
    var watchdog = setTimeout(finish, 4000 + clean.length * 110 / Math.max(0.5, this.rate));
    u.onstart = function () { if (gen === self.gen) self._setSpeaking(true); };
    u.onend = finish;
    u.onerror = finish;
    u.onboundary = function (e) { if (gen === self.gen) { try { self.onboundary(e); } catch (x) { /* ignore */ } } };
    this.live.add(u); // keep a reference: Chrome can GC utterances and drop their events
    this.active++;
    try {
      if (global.speechSynthesis.paused) global.speechSynthesis.resume();
      global.speechSynthesis.speak(u);
    } catch (e) { finish(); }
  };

  /** Stream assistant text in; whole sentences are spoken as they complete. */
  Speaker.prototype.feed = function (delta) {
    if (!this.supported || !this.enabled) return;
    this.buffer += delta;
    this._drain(false);
  };

  /** Speak whatever is left in the buffer. */
  Speaker.prototype.flush = function () {
    if (!this.supported) return;
    this._drain(true);
    this.inFence = false;
    this.buffer = '';
  };

  Speaker.prototype._drain = function (final) {
    var guard = 0;
    while (this.buffer && guard++ < 200) {
      if (this.inFence) {
        var close = this.buffer.indexOf('```');
        if (close === -1) {
          if (final) { this.buffer = ''; } else { this.buffer = this.buffer.slice(-2); }
          return;
        }
        this.buffer = this.buffer.slice(close + 3).replace(/^[^\n]*\n?/, '');
        this.inFence = false;
        continue;
      }
      var fence = this.buffer.indexOf('```');
      var scope = fence === -1 ? this.buffer : this.buffer.slice(0, fence);
      var m = scope.match(/[.!?…](?:["'”’)\]]*)(?=\s)|\n/);
      if (m) {
        var end = m.index + m[0].length;
        var sentence = scope.slice(0, end);
        this.buffer = this.buffer.slice(end);
        this._utter(sentence);
        continue;
      }
      if (fence !== -1) {
        if (scope.trim()) this._utter(scope);
        this.buffer = this.buffer.slice(fence + 3);
        this.inFence = true;
        continue;
      }
      if (final) { this._utter(this.buffer); this.buffer = ''; }
      // Hold back a trailing partial backtick run so a fence isn't missed.
      return;
    }
  };

  /** Speak a complete phrase (optionally interrupting). */
  Speaker.prototype.say = function (text, interrupt) {
    if (!this.supported || !this.enabled) return false;
    if (interrupt) this.stop();
    this._utter(text);
    return true;
  };

  Speaker.prototype.stop = function () {
    this.gen++;
    this.buffer = '';
    this.inFence = false;
    this.active = 0;
    this.live.clear();
    if (this.supported) { try { global.speechSynthesis.cancel(); } catch (e) { /* ignore */ } }
    clearTimeout(this._idleTimer);
    if (this.speaking) { this.speaking = false; this.onstate(false); }
  };

  // ------------------------------------------------------------------ listener
  function Listener() {
    var SR = global.SpeechRecognition || global.webkitSpeechRecognition;
    this.SR = SR || null;
    this.supported = !!SR;
    this.rec = null;
    this.mode = null; // 'command' | 'wake'
    this.wakeEnabled = false;
    this.wakeName = 'ATLAS';
    this.paused = false;
    this.armedUntil = 0;
    this.lang = (global.navigator && navigator.language) || 'en-US';
    this.oninterim = noop;
    this.onfinal = noop;   // (text, via) command text
    this.onwake = noop;    // name heard alone
    this.onstate = noop;   // (mode|null)
    this.onerror = noop;
    this.onheard = noop;   // every final phrase (for approvals)
    this._restartTimer = 0;
    this._fastEnds = 0;
  }

  Listener.prototype._build = function (mode) {
    var self = this;
    var rec;
    try { rec = new this.SR(); } catch (e) { return null; }
    rec.lang = this.lang;
    rec.interimResults = true;
    rec.continuous = mode === 'wake';
    rec.maxAlternatives = 1;
    var startedAt = Date.now();
    rec.onresult = function (ev) {
      var interim = '', finals = [];
      for (var i = ev.resultIndex; i < ev.results.length; i++) {
        var r = ev.results[i];
        var txt = (r[0] && r[0].transcript) || '';
        if (r.isFinal) finals.push(txt); else interim += txt;
      }
      if (mode === 'command' && interim) self.oninterim(interim.trim());
      if (mode === 'wake' && interim && Date.now() < self.armedUntil) self.oninterim(interim.trim());
      finals.forEach(function (f) { self._final(f.trim(), mode); });
    };
    rec.onerror = function (ev) {
      var err = ev && ev.error;
      if (err === 'no-speech' || err === 'aborted') return;
      if (err === 'not-allowed' || err === 'service-not-allowed') {
        self.wakeEnabled = false;
      }
      self.onerror(err || 'unknown');
    };
    rec.onend = function () {
      if (self.rec !== rec) return;
      self.rec = null;
      var wasMode = self.mode;
      self.mode = null;
      self.onstate(null);
      // keep the wake loop alive (with backoff if the engine keeps ending immediately)
      if (self.wakeEnabled && !self.paused) {
        self._fastEnds = Date.now() - startedAt < 1500 ? self._fastEnds + 1 : 0;
        var delay = Math.min(8000, 250 * Math.pow(2, self._fastEnds));
        clearTimeout(self._restartTimer);
        self._restartTimer = setTimeout(function () {
          if (self.wakeEnabled && !self.paused && !self.rec) self.start('wake');
        }, wasMode === 'command' ? 150 : delay);
      }
    };
    return rec;
  };

  Listener.prototype._final = function (text, mode) {
    if (!text) return;
    if (this.onheard(text, mode) === true) return; // consumed (e.g. an approval)
    if (mode === 'command') { this.onfinal(text, 'voice'); return; }
    // wake mode
    var name = (this.wakeName || '').trim();
    if (Date.now() < this.armedUntil) {
      this.armedUntil = 0;
      this.onfinal(text, 'voice');
      return;
    }
    if (!name) return;
    // "A.T.L.A.S." or "Atlas" in settings should both match the spoken word.
    var letters = name.replace(/[^A-Za-z0-9]/g, '');
    if (!letters) return;
    var pattern = letters.split('').join('[.\\s-]?');
    var re = new RegExp('(^|\\b)' + pattern + '\\.?\\b[\\s,.:!?-]*', 'i');
    var m = text.match(re);
    if (!m) return;
    var rest = text.slice(m.index + m[0].length).trim();
    var before = text.slice(0, m.index).replace(/\b(hey|ok|okay|hi|yo)\b/gi, '').trim();
    var command = (rest || before).replace(/^[,.\s]+|[,.\s]+$/g, '');
    if (command.split(/\s+/).filter(Boolean).length >= 1 && /[a-z0-9]/i.test(command)) {
      this.onfinal(command, 'wake');
    } else {
      this.armedUntil = Date.now() + 8000;
      this.onwake();
    }
  };

  Listener.prototype.start = function (mode) {
    if (!this.supported) return false;
    mode = mode || 'command';
    if (this.rec) {
      if (this.mode === mode) return true;
      this._abortCurrent();
    }
    var rec = this._build(mode);
    if (!rec) return false;
    this.rec = rec;
    this.mode = mode;
    try { rec.start(); } catch (e) { this.rec = null; this.mode = null; return false; }
    this.onstate(mode);
    return true;
  };

  Listener.prototype._abortCurrent = function () {
    var rec = this.rec;
    this.rec = null;
    this.mode = null;
    if (rec) { try { rec.onend = null; rec.abort(); } catch (e) { /* ignore */ } }
    this.onstate(null);
  };

  /** Stop listening; in wake mode the loop resumes unless `hard`. */
  Listener.prototype.stop = function (hard) {
    clearTimeout(this._restartTimer);
    var rec = this.rec;
    if (!rec) return;
    if (hard) this._abortCurrent();
    else { try { rec.stop(); } catch (e) { this._abortCurrent(); } }
  };

  Listener.prototype.setWake = function (on) {
    this.wakeEnabled = !!on && this.supported;
    clearTimeout(this._restartTimer);
    if (this.wakeEnabled && !this.rec && !this.paused) this.start('wake');
    if (!this.wakeEnabled && this.mode === 'wake') this._abortCurrent();
  };

  /** Called while TTS speaks so the assistant doesn't hear itself. */
  Listener.prototype.pause = function () {
    this.paused = true;
    clearTimeout(this._restartTimer);
    if (this.rec) this._abortCurrent();
  };

  Listener.prototype.resume = function () {
    this.paused = false;
    if (this.wakeEnabled && !this.rec) {
      var self = this;
      clearTimeout(this._restartTimer);
      this._restartTimer = setTimeout(function () { if (!self.rec && !self.paused && self.wakeEnabled) self.start('wake'); }, 300);
    }
  };

  // ------------------------------------------------------------------ mic meter
  function MicMeter(bars) {
    this.bars = bars || 96;
    this.out = new Float32Array(this.bars);
    this.stream = null;
    this.analyser = null;
    this.source = null;
    this.data = null;
    this.on = false;
    this._pending = null;
  }

  MicMeter.prototype.supported = function () {
    return !!(global.navigator && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && (global.AudioContext || global.webkitAudioContext));
  };

  MicMeter.prototype.start = function () {
    var self = this;
    if (this.on || this._pending) return this._pending || Promise.resolve(true);
    if (!this.supported()) return Promise.resolve(false);
    this._pending = navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      .then(function (stream) {
        self._pending = null;
        var ac = unlockAudio();
        if (!ac) { stream.getTracks().forEach(function (t) { t.stop(); }); return false; }
        self.stream = stream;
        self.source = ac.createMediaStreamSource(stream);
        self.analyser = ac.createAnalyser();
        self.analyser.fftSize = 512;
        self.analyser.smoothingTimeConstant = 0.6;
        self.source.connect(self.analyser);
        self.data = new Uint8Array(self.analyser.frequencyBinCount);
        self.on = true;
        return true;
      })
      .catch(function () { self._pending = null; return false; });
    return this._pending;
  };

  /** Fill and return normalised levels (mirrored so the ring is symmetric). */
  MicMeter.prototype.levels = function () {
    if (!this.on || !this.analyser) return null;
    try { this.analyser.getByteFrequencyData(this.data); } catch (e) { return null; }
    var half = this.bars / 2;
    var bins = this.data.length;
    for (var j = 0; j < half; j++) {
      // log-ish spread across ~80 Hz .. 6 kHz
      var lo = Math.floor(2 + Math.pow(j / half, 1.6) * (bins * 0.28));
      var hi = Math.max(lo + 1, Math.floor(2 + Math.pow((j + 1) / half, 1.6) * (bins * 0.28)));
      var sum = 0;
      for (var k = lo; k < hi && k < bins; k++) sum += this.data[k];
      var v = sum / (hi - lo) / 255;
      v = Math.min(1, Math.pow(v, 1.4) * 1.5);
      this.out[j] = v;
      this.out[this.bars - 1 - j] = v;
    }
    return this.out;
  };

  MicMeter.prototype.stop = function () {
    this.on = false;
    try { if (this.source) this.source.disconnect(); } catch (e) { /* ignore */ }
    try { if (this.stream) this.stream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) { /* ignore */ }
    this.stream = null; this.source = null; this.analyser = null;
  };

  global.AtlasVoice = {
    Speaker: Speaker,
    Listener: Listener,
    MicMeter: MicMeter,
    chime: chime,
    unlockAudio: unlockAudio
  };
})(window);
