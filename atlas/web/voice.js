/* ATLAS — voice I/O.
 * Speaker: sentence-streaming speechSynthesis queue (skips fenced code, strips markdown/URLs).
 * Listener: Web Speech recognition with push-to-talk and continuous wake-word modes.
 * MicMeter: getUserMedia + AnalyserNode -> 96 normalised bar levels.
 * chime(): small WebAudio cues. Every API is optional; nothing here throws when absent.
 */
(function (global) {
  'use strict';

  var noop = function () {};

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

  // ------------------------------------------------------------------ text -> speakable text
  /* Plain text for speech: no code, URLs or markdown symbols; list markers dropped and line
   * breaks turned into pauses so "1. Say brief me" reads as "Say brief me." */
  function cleanForSpeech(s) {
    if (!s) return '';
    var t = String(s);
    t = t.replace(/```[\s\S]*?(```|$)/g, ' ');
    t = t.replace(/~~~[\s\S]*?(~~~|$)/g, ' ');
    t = t.replace(/`([^`]*)`/g, '$1');
    t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');
    t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
    t = t.replace(/<https?:\/\/[^>\s]*>/gi, ' ');
    t = t.replace(/\bhttps?:\/\/\S+/gi, ' ');
    t = t.replace(/\bwww\.\S+/gi, ' ');
    t = t.replace(/^\s{0,3}#{1,6}\s*/gm, '');
    t = t.replace(/^\s*>\s?/gm, '');
    t = t.replace(/^\s*([-*+•]|\d{1,4}[.)])\s+/gm, '');
    t = t.replace(/^[ \t]*\|?[ \t]*:?-{3,}[-|: \t]*$/gm, '');
    t = t.replace(/^[ \t]*\|(.+?)\|?[ \t]*$/gm, function (_, row) {
      return row.split('|').map(function (c) { return c.trim(); }).filter(Boolean).join(', ');
    });
    t = t.replace(/\*\*|~~|\*/g, '');
    t = t.replace(/(^|[^A-Za-z0-9])_+/g, '$1').replace(/_+(?=[^A-Za-z0-9]|$)/g, '');
    t = t.replace(/\s*\|\s*/g, ', ');
    t = t.replace(/[#<>{}\[\]\\^]/g, ' ');
    t = t.replace(/\s*[—–]\s*/g, ', ');
    t = t.replace(/([^\s.!?:;,])[ \t]*\n+/g, '$1. '); // a line break (list item, heading) becomes a pause
    t = t.replace(/\s+/g, ' ').trim();
    t = t.replace(/^[,.;:\s]+/, '');
    return /[A-Za-z0-9]/.test(t) ? t : '';
  }

  // An ordered-list marker ("1.", "12)") at the start of a line is not a sentence end.
  function isListMarker(s, idx) {
    var lineStart = s.lastIndexOf('\n', idx) + 1;
    return /^\s*\d{1,4}$/.test(s.slice(lineStart, idx));
  }

  // Index just past the last sentence boundary in `s` (0 if none).
  function lastBoundary(s) {
    var re = /[.!?…]+["'”’)\]]*(?=\s)|\n/g;
    var end = 0, m;
    while ((m = re.exec(s))) {
      if (m[0] !== '\n' && isListMarker(s, m.index)) continue;
      end = m.index + m[0].length;
    }
    return end;
  }

  // Split on terminal punctuation followed by whitespace (so "2.5" and "U.S" stay whole) and merge
  // tiny fragments into their neighbour, so the engine never says "1." on its own.
  function splitSentences(text) {
    var parts = [];
    var re = /[.!?…]+["'”’)\]]*(?=\s|$)/g;
    var last = 0, m;
    while ((m = re.exec(text))) {
      var end = m.index + m[0].length;
      parts.push(text.slice(last, end));
      last = end;
    }
    if (last < text.length) parts.push(text.slice(last));
    var out = [];
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i].trim();
      if (!p) continue;
      var prev = out.length ? out[out.length - 1] : null;
      if (prev != null && (prev.length < 24 || p.length < 4 || !/[A-Za-z0-9]/.test(p))) out[out.length - 1] = prev + ' ' + p;
      else out.push(p);
    }
    return out.filter(function (x) { return /[A-Za-z0-9]/.test(x); });
  }

  // ------------------------------------------------------------------ speaker
  var PREFERRED = ['UK English Male', 'Ryan', 'Daniel', 'George', 'Arthur', 'Oliver', 'Thomas'];

  /* One utterance at a time: sentences queue up and the next one is handed to the engine only when
   * the previous one ends (or its watchdog fires, for engines that never report back). Live
   * utterances are kept in a Set so Chrome can't garbage-collect them and drop their events. */
  function Speaker() {
    this.supported = !!(global.speechSynthesis && typeof global.SpeechSynthesisUtterance === 'function');
    this.enabled = true;
    this.voiceURI = '';
    this.rate = 1;
    this.pitch = 1;
    this.q = [];
    this.cur = null;
    this.buf = '';
    this.inFence = false;
    this.speaking = false;
    this.gen = 0;
    this.live = new Set();
    this.onstate = noop;
    this.onboundary = noop;
    this.onvoices = noop;
    this._idleTimer = 0;
    this._watch = 0;
    var self = this;
    if (this.supported) {
      try {
        var fire = function () { try { self.onvoices(self.voices()); } catch (e) { /* ignore */ } };
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
      if (!this.speaking) { this.speaking = true; try { this.onstate(true); } catch (e) { /* ignore */ } }
    } else if (this.speaking) {
      // debounce the gap between streamed sentences
      this._idleTimer = setTimeout(function () {
        if (!self.cur && !self.q.length && self.speaking) { self.speaking = false; try { self.onstate(false); } catch (e) { /* ignore */ } }
      }, 260);
    }
  };

  Speaker.prototype._emit = function (raw) {
    var clean = cleanForSpeech(raw);
    if (!clean) return;
    var parts = splitSentences(clean);
    for (var i = 0; i < parts.length; i++) this.q.push(parts[i]);
    this._next();
  };

  Speaker.prototype._next = function () {
    if (this.cur || !this.q.length || !this.supported) return;
    var self = this;
    var text = this.q.shift();
    var u;
    try { u = new global.SpeechSynthesisUtterance(text); } catch (e) { this.q = []; this._setSpeaking(false); return; }
    var v = this.currentVoice();
    if (v) { u.voice = v; u.lang = v.lang || 'en-GB'; } else { u.lang = 'en-GB'; } // voices may still be loading
    u.rate = this.rate; u.pitch = this.pitch;
    this.cur = u;
    this.live.add(u);
    // Report "speaking" now (not on onstart) so a wake recogniser pauses before the first word.
    this._setSpeaking(true);
    var finished = false;
    var done = function () {
      if (finished) return;
      finished = true;
      self.live.delete(u);
      if (self.cur !== u) return;
      clearTimeout(self._watch);
      self.cur = null;
      if (self.q.length) self._next(); else self._setSpeaking(false);
    };
    u.onend = done;
    u.onerror = done;
    u.onboundary = function (e) { if (self.cur === u) { try { self.onboundary(e); } catch (x) { /* ignore */ } } };
    // Some engines never fire end/error (headless, missing voices): don't let the queue stall.
    clearTimeout(this._watch);
    this._watch = setTimeout(done, 4000 + text.length * 110 / Math.max(0.5, this.rate));
    try {
      if (global.speechSynthesis.paused) global.speechSynthesis.resume();
      global.speechSynthesis.speak(u);
    } catch (e) { done(); }
  };

  /** Stream assistant text in; whole sentences are queued as they complete. Fenced code is skipped. */
  Speaker.prototype.feed = function (delta) {
    if (!this.supported || !this.enabled || !delta) return;
    this.buf += delta;
    if (this.buf.length > 20000) this.buf = this.buf.slice(-20000);
    this._drain(false);
  };

  /** Speak whatever is left in the stream buffer. */
  Speaker.prototype.flush = function () {
    if (!this.supported) return;
    if (this.enabled) this._drain(true);
    this.buf = '';
    this.inFence = false;
  };

  Speaker.prototype._drain = function (final) {
    for (var guard = 0; guard < 200 && this.buf; guard++) {
      if (this.inFence) {
        var end = this.buf.indexOf('```');
        if (end === -1) {
          // keep a short tail so a closing fence split across tokens is still found
          this.buf = final ? '' : this.buf.slice(-2);
          if (final) this.inFence = false;
          return;
        }
        var nl = this.buf.indexOf('\n', end + 3);
        this.buf = nl === -1 ? this.buf.slice(end + 3) : this.buf.slice(nl + 1);
        this.inFence = false;
        continue;
      }
      var fence = this.buf.indexOf('```');
      var seg = fence === -1 ? this.buf : this.buf.slice(0, fence);
      if (fence !== -1) {
        this._emit(seg);
        this.buf = this.buf.slice(fence + 3);
        this.inFence = true;
        continue;
      }
      // hold back a trailing partial fence marker
      var holdM = !final && /`{1,2}$/.exec(seg);
      var hold = holdM ? holdM[0].length : 0;
      var usable = hold ? seg.slice(0, -hold) : seg;
      var cut = final ? usable.length : lastBoundary(usable);
      if (cut > 0) {
        this._emit(usable.slice(0, cut));
        this.buf = this.buf.slice(cut);
        if (final && !hold) this.buf = '';
        continue;
      }
      return;
    }
  };

  /** Speak a complete phrase (optionally interrupting what is playing). */
  Speaker.prototype.say = function (text, interrupt) {
    if (!this.supported || !this.enabled) return false;
    if (interrupt) this.stop();
    this._emit(text);
    return true;
  };

  /** True while something is playing or queued. */
  Speaker.prototype.busy = function () { return !!this.cur || this.q.length > 0; };

  Speaker.prototype.stop = function () {
    this.gen++;
    this.q = [];
    this.buf = '';
    this.inFence = false;
    clearTimeout(this._watch);
    this.cur = null;
    this.live.clear();
    if (this.supported) { try { global.speechSynthesis.cancel(); } catch (e) { /* ignore */ } }
    clearTimeout(this._idleTimer);
    if (this.speaking) { this.speaking = false; try { this.onstate(false); } catch (e) { /* ignore */ } }
  };

  // ------------------------------------------------------------------ wake word
  /* The assistant's name as a whole word, tolerant of how recognisers spell it out:
   * "Atlas", "A.T.L.A.S.", "a t l a s", "J-9". Built from the name's letters with optional
   * separators, bounded so "atlassian" or "matlas" never trigger it. */
  var wakeCache = { name: null, re: null };
  function escapeRe(c) { return c.replace(/[.*+?^${}()|[\]\\\/-]/g, '\\$&'); }
  function wakeRegex(name) {
    if (wakeCache.name === name) return wakeCache.re;
    var re = null;
    var chars = Array.from(String(name || '').normalize ? String(name).normalize('NFC') : String(name || ''));
    try {
      var letters = chars.filter(function (c) { return /[\p{L}\p{N}]/u.test(c); });
      if (letters.length) {
        var body = letters.map(escapeRe).join('[.\\s\\-_]{0,2}');
        re = new RegExp('(?<![\\p{L}\\p{N}])' + body + '\\.?(?![\\p{L}\\p{N}])[\\s,.:;!?\\-]*', 'iu');
      }
    } catch (e) {
      // engines without lookbehind / \p{}: ASCII word boundaries
      var ascii = chars.filter(function (c) { return /[A-Za-z0-9]/.test(c); });
      re = ascii.length ? new RegExp('\\b' + ascii.map(escapeRe).join('[.\\s\\-_]{0,2}') + '\\.?\\b[\\s,.:;!?\\-]*', 'i') : null;
    }
    wakeCache = { name: name, re: re };
    return re;
  }
  function wakeMatch(name, text) {
    var re = wakeRegex(name);
    if (!re) return null;
    var m = re.exec(String(text || ''));
    return m ? { index: m.index, end: m.index + m[0].length } : null;
  }

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
    var m = wakeMatch(name, text);
    if (!m) return;
    var rest = text.slice(m.end).trim();
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
    this.want = false;
    this._pending = null;
  }

  MicMeter.prototype.supported = function () {
    return !!(global.navigator && navigator.mediaDevices && navigator.mediaDevices.getUserMedia && (global.AudioContext || global.webkitAudioContext));
  };

  MicMeter.prototype.start = function () {
    var self = this;
    this.want = true; // re-checked when getUserMedia resolves: stop() while pending must win
    if (this.on) return Promise.resolve(true);
    if (this._pending) return this._pending;
    if (!this.supported()) return Promise.resolve(false);
    var p;
    try {
      p = navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (e) { return Promise.resolve(false); }
    this._pending = Promise.resolve(p).then(function (stream) {
      self._pending = null;
      var ac = self.want ? unlockAudio() : null;
      if (!self.want || !ac) { stopTracks(stream); return false; }
      try {
        self.stream = stream;
        self.source = ac.createMediaStreamSource(stream);
        self.analyser = ac.createAnalyser();
        self.analyser.fftSize = 512;
        self.analyser.smoothingTimeConstant = 0.6;
        self.source.connect(self.analyser);
        self.data = new Uint8Array(self.analyser.frequencyBinCount);
        self.on = true;
        return true;
      } catch (e) { self.stop(); return false; }
    }, function () { self._pending = null; return false; });
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
    this.want = false;
    this.on = false;
    try { if (this.source) this.source.disconnect(); } catch (e) { /* ignore */ }
    stopTracks(this.stream);
    this.stream = null; this.source = null; this.analyser = null;
  };

  function stopTracks(stream) {
    try { if (stream && stream.getTracks) stream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* ignore */ } }); } catch (e) { /* ignore */ }
  }

  global.AtlasVoice = {
    Speaker: Speaker,
    Listener: Listener,
    MicMeter: MicMeter,
    chime: chime,
    unlockAudio: unlockAudio,
    cleanForSpeech: cleanForSpeech,
    splitSentences: splitSentences,
    lastBoundary: lastBoundary,
    wakeMatch: wakeMatch
  };
})(window);
