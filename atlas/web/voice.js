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
  /* Automatic voice: natural neural British male voices first (Edge/Windows "Microsoft Ryan Online (Natural)",
   * "Thomas Online (Natural)"), then the better-known en-GB male system voices, then any en-GB, then any English.
   * Each test runs on the voice's lower-cased name; `gb` restricts it to en-GB voices. */
  var PREFERRED = [
    { gb: true, test: function (n) { return /\bryan\b/.test(n) && /natural|online/.test(n); } },
    { gb: true, test: function (n) { return /\bthomas\b/.test(n) && /natural|online/.test(n); } },
    { gb: false, test: function (n) { return n.indexOf('google uk english male') !== -1; } },
    { gb: true, test: function (n) { return /\bdaniel\b/.test(n); } },
    { gb: true, test: function (n) { return /\barthur\b/.test(n); } },
    { gb: true, test: function (n) { return /\bgeorge\b/.test(n); } },
    { gb: false, test: function (n) { return /\bryan\b/.test(n) && /natural|online/.test(n); } }
  ];

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
    this.onutter = noop;   // (text) a sentence is handed to the engine (captions); (null) speech stopped
    this.recent = [];      // [{text, at}] the last few sentences spoken (so the listener can ignore its own voice)
    this.endedAt = 0;      // when speech last went quiet
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
      var rule = PREFERRED[i];
      var hit = (rule.gb ? gb : en).filter(function (v) { return rule.test(String(v.name || '').toLowerCase()); })[0];
      if (hit) return hit;
    }
    var male = gb.filter(function (v) { return /\bmale\b/i.test(v.name) && !/female/i.test(v.name); })[0];
    return male || gb[0] || en[0] || vs[0];
  };

  /** The sentences spoken in the last `ms` milliseconds (the one playing included), joined. */
  Speaker.prototype.recentText = function (ms) {
    var since = Date.now() - (ms || 8000);
    return this.recent.filter(function (r) { return r.at >= since || r.live; }).map(function (r) { return r.text; }).join(' ');
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

  /* Chrome's network voices (the Google ones) stop about 15 s into a single utterance, often without an end
   * event, so the rest of a long sentence is lost and the page waits on the watchdog. A sentence longer than
   * `max` characters is spoken in clause-sized pieces instead (at a comma, semicolon, colon or dash; else a space). */
  var CHUNK_MAX = 180;
  function chunkLong(s, max) {
    var out = [];
    max = max || CHUNK_MAX;
    for (var guard = 0; s.length > max && guard < 50; guard++) {
      var win = s.slice(0, max + 1);
      var cut = -1, re = /[,;:](?=\s)|\s[\u2013\u2014-](?=\s)/g, m;
      while ((m = re.exec(win))) if (m.index >= 40) cut = m.index + m[0].length;
      if (cut < 0) { cut = win.lastIndexOf(' '); if (cut < 40) cut = max; }
      out.push(s.slice(0, cut).trim());
      s = s.slice(cut).trim();
    }
    if (s) out.push(s);
    return out.filter(function (x) { return /[\p{L}\p{N}]/u.test(x); });
  }

  Speaker.prototype._emit = function (raw) {
    var clean = cleanForSpeech(raw);
    if (!clean) return;
    var parts = splitSentences(clean);
    for (var i = 0; i < parts.length; i++) {
      var bits = parts[i].length > CHUNK_MAX ? chunkLong(parts[i]) : [parts[i]];
      for (var j = 0; j < bits.length; j++) this.q.push(bits[j]);
    }
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
    var rec = { text: text, at: Date.now(), live: true };
    this.recent.push(rec);
    if (this.recent.length > 6) this.recent.shift();
    // Report "speaking" now (not on onstart) so a wake recogniser pauses before the first word.
    this._setSpeaking(true);
    try { this.onutter(text); } catch (e) { /* ignore */ }
    var finished = false;
    var done = function () {
      if (finished) return;
      finished = true;
      rec.live = false;
      rec.at = Date.now();
      self.live.delete(u);
      if (self.cur !== u) return;
      clearTimeout(self._watch);
      self.cur = null;
      self.endedAt = Date.now();
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

  /** Drop the unfinished sentence still in the stream buffer (a reply that broke off mid-sentence). */
  Speaker.prototype.discard = function () {
    this.buf = '';
    this.inFence = false;
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
    var was = !!this.cur;
    this.cur = null;
    this.live.clear();
    var now = Date.now();
    this.recent.forEach(function (r) { if (r.live) { r.live = false; r.at = now; } });
    if (was) this.endedAt = now;
    if (this.supported) { try { global.speechSynthesis.cancel(); } catch (e) { /* ignore */ } }
    clearTimeout(this._idleTimer);
    if (was) { try { this.onutter(null); } catch (e) { /* ignore */ } }
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

  // ------------------------------------------------------------------ text helpers for recognition
  /* Lower-case words only, for comparing what was heard (punctuation and spacing vary between engines). */
  function norm(s) {
    return String(s || '').toLowerCase().replace(/[’`]/g, "'").replace(/[^\p{L}\p{N}' ]+/gu, ' ').replace(/\s+/g, ' ').trim();
  }
  function words(s) {
    return String(s || '').split(/\s+/).filter(Boolean).map(function (w) { return { raw: w, n: norm(w) }; }).filter(function (w) { return w.n; });
  }
  /* Join two pieces of one utterance. Recognisers send several final segments per sentence; some (Android)
   * re-send the whole phrase so far each time, others repeat a few words across the seam. Neither may
   * produce "what is the what is the weather". */
  function mergeText(a, b) {
    a = String(a || '').trim(); b = String(b || '').trim();
    if (!a) return b;
    if (!b) return a;
    var na = norm(a), nb = norm(b);
    if (!nb) return a;
    if (!na) return b;
    if (nb === na || nb.indexOf(na + ' ') === 0) return b;     // cumulative: b already contains a
    if (na.slice(-nb.length) === nb && (na.length === nb.length || na.charAt(na.length - nb.length - 1) === ' ')) return a; // b repeats a's tail
    var wa = words(a), wb = words(b);
    for (var k = Math.min(wa.length, wb.length - 1, 5); k >= 2; k--) {
      var tail = wa.slice(-k).map(function (w) { return w.n; }).join(' ');
      var head = wb.slice(0, k).map(function (w) { return w.n; }).join(' ');
      if (tail === head) return a + ' ' + wb.slice(k).map(function (w) { return w.raw; }).join(' ');
    }
    return a + ' ' + b;
  }
  /* True when `heard` is (most likely) the assistant's own voice coming back through the microphone.
   * `after`: the phrase began once speech had ended. Then only the end of what was said can still be coming
   * back (the recogniser's late version of the last words), so a quick reply that repeats words of the question
   * ("Shall I set a reminder for the dentist?" → "set a reminder") is not mistaken for the assistant's voice. */
  function isEchoOf(heard, spoken, after) {
    var h = norm(heard), s = norm(spoken);
    if (!h || !s) return false;
    var hw = h.split(' ');
    var sw = s.split(' ');
    if (after) {
      if (s === h || s.slice(-(h.length + 1)) === ' ' + h) return true;
      if (hw.length < 3 || hw[hw.length - 1] !== sw[sw.length - 1]) return false;
    } else {
      if ((' ' + s + ' ').indexOf(' ' + h + ' ') !== -1) return true;
      if (hw.length < 3) return false;
    }
    var bag = {};
    sw.forEach(function (w) { bag[w] = 1; });
    var hit = hw.filter(function (w) { return bag[w]; }).length;
    return hit / hw.length >= 0.7;
  }
  // Interrupt words: the whole phrase must be one of these (optionally after the name or "hey"/"ok").
  var CONTROL_RE = /^(?:(?:hey|ok|okay|oh|um+|uh+)\s+)*(?:stop|stop it|stop that|stop talking|please stop|cancel|cancel that|cancel it|abort|never ?mind|forget it|be quiet|quiet|silence|enough|that's enough|shut up|hush)(?:\s+(?:please|now|thanks|thank you))*$/;
  /** The phrase as an interrupt command ('stop'), or '' when it is anything else. */
  function controlOf(name, text) {
    var t = String(text || '');
    var m = wakeMatch(name, t);
    if (m && /^[\s,.!?]*(?:(?:hey|ok|okay|hi|yo)[\s,.!?]+)?$/i.test(t.slice(0, m.index))) t = t.slice(m.end);
    else if (m && /^[\s,.!?]*$/.test(t.slice(m.end))) t = t.slice(0, m.index);
    return CONTROL_RE.test(norm(t)) ? 'stop' : '';
  }
  /* The name only wakes the assistant when it opens a phrase ("Atlas, …", "Hey Atlas …", "Good morning, Atlas …"):
   * said in passing ("I told my sister about Atlas yesterday", "the Atlas mountains", "see you on Friday" for an
   * assistant called Friday) it is just a word. These may come before it. */
  var LEAD_RE = /^[\s,.!?]*(?:(?:hey|hi|hello|there|ok|okay|oh|so|um+|uh+|er+m?|right|alright|well|and|yo|excuse me|sorry|please|good (?:morning|afternoon|evening|day)|morning|afternoon|evening)[\s,.!?]+)*$/i;
  /** True when the name opens `text` (after at most a greeting or filler words). */
  function wakeLeads(name, text) {
    var t = String(text || '');
    var m = wakeMatch(name, t);
    return !!m && LEAD_RE.test(t.slice(0, m.index));
  }
  /** What follows the wake word ("Atlas, what's the time" → "what's the time"); '' for the name alone. */
  function afterWake(name, text) {
    var m = wakeMatch(name, text);
    if (!m) return String(text || '').trim();
    var rest = text.slice(m.end).trim();
    var before = text.slice(0, m.index).replace(/\b(hey|ok|okay|hi|yo|so|um+|uh+)\b/gi, '').trim();
    return (rest || before).replace(/^[,.\s]+|[,.\s]+$/g, '');
  }

  // ------------------------------------------------------------------ listener
  /* One continuous recogniser, steered by a phase the page sets:
   *   'wake' — waiting for the wake word (with wakeRequired off, any phrase is a request);
   *   'open' — taking a request: push-to-talk, after the wake word, the follow-up window, an approval answer;
   *   'busy' — a reply is being prepared or spoken: only interrupt words ("stop", "cancel", "never mind") and the
   *            wake word (barge-in) count, and anything matching what the assistant is saying is ignored;
   *   null   — recognition off.
   * Recognisers emit several final segments for one sentence. They are merged into one utterance that is only
   * handed on after `eouMs` of silence (or when a held talk key is released), so a request is never sent in
   * fragments and the assistant never answers half a sentence. */
  function Listener() {
    var SR = global.SpeechRecognition || global.webkitSpeechRecognition;
    this.SR = SR || null;
    this.supported = !!SR;
    this.rec = null;
    this.phase = null;
    this.wakeName = 'ATLAS';
    this.wakeRequired = true;
    this.eouMs = 1200;
    this.denied = false;
    this.lang = (global.navigator && navigator.language) || 'en-US';
    // (startedAt) what the assistant was saying when this utterance began: a string, or {text, after} where `after`
    // means speech had already ended by then
    this.echo = function () { return ''; };
    this.oninterim = noop;    // (text) the words of the utterance so far, while taking a request
    this.onutterance = noop;  // (text, woke) a finished request
    this.onheard = noop;      // (text, phase, woke) every finished utterance first; return true to consume it
    this.onwake = noop;       // (phase) the wake word was heard ('wake', or 'busy' for a barge-in)
    this.oncontrol = noop;    // ('stop') an interrupt word
    this.onactivity = noop;   // speech is coming in (keeps listening windows open)
    this.onnothing = noop;    // (reason) an utterance ended with nothing to hand on
    this.onstate = noop;      // (running)
    this.onerror = noop;
    this._reset();
    this._lastAct = 0;
    this._restartTimer = 0;
    this._fastEnds = 0;
    this._startedAt = 0;
    this._swallow = null;
  }

  Listener.prototype._reset = function () {
    clearTimeout(this._eou);
    this._eou = 0;
    this._buf = '';
    this._interim = '';
    this._woke = false;
    this._ctl = false;
    this._waits = 0;
    this._hold = false;
    this._uttAt = 0;
  };

  /** Set the phase. A phase change drops a half-heard utterance, except a wake phrase continuing into 'open'. */
  Listener.prototype.setPhase = function (p) {
    if (!this.supported || this.denied) p = null;
    if (p === this.phase) { if (p && !this._restartTimer) this._ensure(); return; }
    var keep = p === 'open' && this._woke;
    var hold = this._hold;
    this.phase = p;
    if (!keep) { this._reset(); if (p === 'open') this._hold = hold; }
    if (!p) { this._halt(); return; }
    this._ensure();
  };

  Listener.prototype.running = function () { return !!this.rec; };

  /** Speech is in progress (words not yet handed on, or heard within the last moment). */
  Listener.prototype.hasSpeech = function () {
    return !!(this._buf || this._interim) || Date.now() - this._lastAct < 1500;
  };

  Listener.prototype._ensure = function () {
    if (this.rec || !this.phase || this.denied) return;
    clearTimeout(this._restartTimer);
    this._restartTimer = 0;
    var rec = this._build();
    if (!rec) return;
    this.rec = rec;
    this._startedAt = Date.now();
    try { rec.start(); } catch (e) { this.rec = null; this._scheduleRestart(); return; }
    this.onstate(true);
  };

  Listener.prototype._halt = function () {
    clearTimeout(this._restartTimer);
    this._restartTimer = 0;
    var rec = this.rec;
    this.rec = null;
    if (rec) {
      try { rec.onend = null; rec.onresult = null; rec.abort(); } catch (e) { /* ignore */ }
      this.onstate(false);
    }
  };

  Listener.prototype._scheduleRestart = function () {
    var self = this;
    this._fastEnds = Date.now() - this._startedAt < 1500 ? this._fastEnds + 1 : 0;
    var delay = Math.min(8000, 150 * Math.pow(2, Math.min(this._fastEnds, 6)));
    clearTimeout(this._restartTimer);
    this._restartTimer = setTimeout(function () {
      self._restartTimer = 0;
      if (self.phase && !self.rec) self._ensure();
    }, delay);
  };

  Listener.prototype._build = function () {
    var self = this;
    var rec;
    try { rec = new this.SR(); } catch (e) { return null; }
    rec.lang = this.lang;
    rec.interimResults = true;
    rec.continuous = true;
    rec.maxAlternatives = 1;
    rec.onresult = function (ev) { if (self.rec === rec) { try { self._results(ev); } catch (e) { /* ignore */ } } };
    rec.onerror = function (ev) {
      var err = ev && ev.error;
      if (err === 'no-speech' || err === 'aborted') return;
      if (err === 'not-allowed' || err === 'service-not-allowed') { self.denied = true; }
      self.onerror(err || 'unknown');
    };
    rec.onend = function () {
      if (self.rec !== rec) return;
      self.rec = null;
      // Engines end continuous sessions on their own (silence, ~60 s, network): keep listening (with a
      // back-off when they keep ending at once). A half-heard utterance survives the restart.
      if (self.phase && !self.denied) self._scheduleRestart();
      self.onstate(false);
    };
    return rec;
  };

  Listener.prototype._isSwallowed = function (t) {
    var s = this._swallow;
    if (!s) return false;
    if (Date.now() > s.until) { this._swallow = null; return false; }
    var n = norm(t);
    // the engine's late final version of words that were already used while still interim
    if (n && (s.text === n || s.text.indexOf(n) === 0 || n.indexOf(s.text) === 0 || (' ' + s.sent + ' ').indexOf(' ' + n + ' ') !== -1)) {
      this._swallow = null;
      return true;
    }
    return false;
  };

  Listener.prototype._isEcho = function (text) {
    var e = '';
    try { e = this.echo(this._uttAt || Date.now()) || ''; } catch (x) { e = ''; }
    var spoken = typeof e === 'string' ? e : String(e.text || '');
    return !!spoken && isEchoOf(text, spoken, typeof e === 'object' && !!e.after);
  };

  Listener.prototype._results = function (ev) {
    if (!this.phase) return;
    var phase = this.phase;
    // Waiting for the name: a recogniser segment that opens with it starts the utterance afresh, so the name
    // said after other speech (a TV, a conversation) still counts and the words before it are dropped.
    var watch = (phase === 'busy' || phase === 'wake') && !this._woke;
    var interim = '', gotFinal = false, seg = '';
    for (var i = ev.resultIndex || 0; i < ev.results.length; i++) {
      var r = ev.results[i];
      var t = ((r && r[0] && r[0].transcript) || '').trim();
      if (!t) continue;
      if (r.isFinal) {
        if (this._isSwallowed(t)) continue;
        if (watch && this._buf && wakeLeads(this.wakeName, t)) this._buf = '';
        this._buf = mergeText(this._buf, t);
        gotFinal = true;
        seg = t;
      } else {
        interim = interim ? interim + ' ' + t : t;
      }
    }
    if (watch && this._buf && interim && wakeLeads(this.wakeName, interim)) this._buf = '';
    if (interim) seg = interim;
    this._interim = interim;
    var now = mergeText(this._buf, interim);
    if (!now) return;
    if (!this._uttAt) { this._uttAt = Date.now(); this._ctl = false; }
    this._lastAct = Date.now();

    if (phase === 'busy' || phase === 'wake') {
      if (this._isEcho(now)) {
        // the assistant hearing itself: drop it entirely
        if (gotFinal || !interim) { this._buf = ''; this._interim = ''; this._uttAt = 0; }
        this._arm();
        return;
      }
    }
    if (phase === 'busy') {
      // An interrupt word counts as the whole phrase, or as the latest segment on its own ("…the bus stop" | "stop").
      // (Words still interim count only when they are an interrupt by themselves.)
      var ctlNow = (controlOf(this.wakeName, now) && (gotFinal || controlOf(this.wakeName, interim || now))) ||
        (seg !== now && controlOf(this.wakeName, seg) && !this._isEcho(seg));
      if (!this._ctl && ctlNow) {
        this._ctl = true;
        clearTimeout(this._eou); this._eou = 0;
        this._swallow = interim ? { text: norm(interim), sent: norm(now), until: Date.now() + 4000 } : null;
        this._buf = ''; this._interim = ''; this._uttAt = 0;
        this.oncontrol('stop');
        return;
      }
      if (!this._woke && wakeLeads(this.wakeName, now)) {
        this._woke = true;
        this.onwake('busy');
      }
    } else if (phase === 'wake') {
      if (!this._woke && wakeLeads(this.wakeName, now)) {
        this._woke = true;
        this.onwake('wake');
      }
    }
    // Taking a request just after speech ended: the late tail of the assistant's own words neither shows as
    // your caption nor holds the window open (_finish drops it).
    if (phase === 'open' && this._isEcho(now)) { this._arm(); return; }
    this.onactivity();
    if (this.phase === 'open' || (this.phase === 'wake' && !this.wakeRequired)) this.oninterim(now);
    this._arm();
  };

  /* End-of-utterance timer: restarted by every result. Words still marked interim get two short extra waits
   * for the engine's final version before they are used as they are. */
  Listener.prototype._arm = function () {
    clearTimeout(this._eou);
    this._eou = 0;
    if (this._hold) return; // a held talk key decides when the utterance ends
    var self = this;
    this._waits = 0;
    this._eou = setTimeout(function () { self._silence(); }, this.eouMs);
  };

  Listener.prototype._silence = function () {
    this._eou = 0;
    if (this._interim && this._waits < 2) {
      var self = this;
      this._waits++;
      this._eou = setTimeout(function () { self._silence(); }, 600);
      return;
    }
    this._finish();
  };

  Listener.prototype._finish = function () {
    clearTimeout(this._eou);
    this._eou = 0;
    var text = mergeText(this._buf, this._interim).trim();
    if (this._interim) this._swallow = { text: norm(this._interim), sent: norm(text), until: Date.now() + 4000 };
    var woke = this._woke, phase = this.phase, ctl = this._ctl;
    var echo = !!text && this._isEcho(text);
    this._buf = ''; this._interim = ''; this._woke = false; this._ctl = false; this._waits = 0; this._uttAt = 0;
    if (!phase) return;
    if (!text || ctl) { this.onnothing('empty'); return; }
    if (echo) { this.onnothing('echo'); return; }
    if (woke) {
      if (!wakeLeads(this.wakeName, text)) { this.onnothing('nowake'); return; } // "Atlas…" became "Atlassian"
      text = afterWake(this.wakeName, text);
      if (!text) { this.onnothing('name'); return; } // the name alone: the window stays open for the request
    } else if (wakeLeads(this.wakeName, text)) {
      // "Atlas, and tomorrow?" in a follow-up window: the name is a greeting, not part of the request
      text = afterWake(this.wakeName, text);
      if (!text) { this.onnothing('name'); return; }
    }
    if (this.onheard(text, phase, woke) === true) return;
    if (phase === 'open' || (phase === 'wake' && !this.wakeRequired)) this.onutterance(text, woke);
    else this.onnothing('ignored');
  };

  /** Push-to-talk key: hold(true) on press (no silence cut-off), hold(false) on release (send what was said). */
  Listener.prototype.hold = function (on) {
    var self = this;
    if (on) { this._hold = true; clearTimeout(this._eou); this._eou = 0; return; }
    if (!this._hold) return;
    this._hold = false;
    clearTimeout(this._eou);
    // give the engine a moment to turn its last words into a final result
    this._eou = setTimeout(function () {
      self._eou = 0;
      if (self._buf || self._interim) self._finish(); else self.onnothing('empty');
    }, 450);
  };

  /** Hand on what has been said so far right now (a second tap). False if nothing was heard. */
  Listener.prototype.flush = function () {
    if (!(this._buf || this._interim)) return false;
    this._finish();
    return true;
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
    chunkLong: chunkLong,
    lastBoundary: lastBoundary,
    wakeMatch: wakeMatch,
    wakeLeads: wakeLeads,
    mergeText: mergeText,
    isEchoOf: isEchoOf,
    controlOf: controlOf,
    afterWake: afterWake,
    norm: norm
  };
})(window);
