/* ATLAS — safe mini-markdown, built as DOM nodes.
 *
 * Safe by construction: model text is never turned into HTML by string concatenation and nothing
 * is ever re-expanded. Blocks and inline spans are parsed from the raw text and emitted with
 * createElement + Text nodes, so every character of the source ends up in a Text node or in an
 * attribute set through setAttribute. The only attributes ever written are a fixed class name,
 * a sanitised data-lang, a numeric start, and href/target/rel on links. An href is produced only
 * for an http(s) URL that survives the WHATWG URL parser (no credentials), and every link opens in
 * a new tab with rel="noopener noreferrer".
 *
 * API: fragment(md) -> DocumentFragment, renderInto(el, md), render(md) -> HTML string (serialised
 * from the DOM, for tests), stableCut(md) -> index up to which the text forms complete blocks (lets
 * a streaming bubble re-render only its tail), escapeHtml, safeUrl.
 */
(function (global) {
  'use strict';

  var doc = global.document;

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }

  var MAX_SOURCE = 400000;   // characters rendered per message (the rest is cut with a note)
  var MAX_DEPTH = 6;         // nested emphasis levels
  var PUNCT = /[!-\/:-@\[-`{-~]/;
  var WORDCH = /[\p{L}\p{N}]/u;

  function badUrlChar(s) {
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      // controls, spaces, C1, line/paragraph separators, bidi embedding/override/isolate marks
      if (c <= 0x20 || (c >= 0x7f && c <= 0x9f) || c === 0x2028 || c === 0x2029 ||
        (c >= 0x202a && c <= 0x202e) || (c >= 0x2066 && c <= 0x2069)) return true;
    }
    return /[<>"'`\\]/.test(s);
  }

  /** Normalise an http(s) URL, or return null. Never returns javascript:, data:, etc. */
  function safeUrl(raw) {
    if (typeof raw !== 'string') return null;
    var s = raw.trim();
    if (!s || s.length > 2048) return null;
    if (!/^https?:\/\/[^\/\\]/i.test(s)) return null;
    if (badUrlChar(s)) return null;
    var u;
    try { u = new URL(s); } catch (e) { return null; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    if (!u.hostname || u.username || u.password) return null;
    return u.href;
  }

  function el(tag, cls) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    return e;
  }
  function text(s) { return doc.createTextNode(s); }

  function anchor(href) {
    var a = el('a');
    a.setAttribute('href', href);
    a.setAttribute('target', '_blank');
    a.setAttribute('rel', 'noopener noreferrer');
    return a;
  }

  function shortUrl(url) {
    var shown = url.replace(/^https?:\/\//i, '');
    return shown.length > 48 ? shown.slice(0, 45) + '…' : shown;
  }

  function runLen(s, i, ch) {
    var j = i;
    while (s.charAt(j) === ch) j++;
    return j - i;
  }

  // ------------------------------------------------------------------ inline parser
  /* One parser per inline string. Atomic spans (code, links, URLs) are recognised first wherever
   * an emphasis closer is searched for, so emphasis markers inside them never pair up. Failed
   * searches are memoised, which keeps pathological input (thousands of unmatched markers) linear. */
  function Inline(s, noLinks, depth) {
    this.s = s;
    this.n = s.length;
    this.noLinks = !!noLinks;
    this.depth = depth || 0;
    this.noCode = {};   // backtick run length -> position after which no closer exists
    this.noClose = {};  // delimiter -> position after which no closer exists
  }

  Inline.prototype.codeSpan = function (i) {
    var s = this.s;
    var len = runLen(s, i, '`');
    if (this.noCode[len] != null && i >= this.noCode[len]) return null;
    var j = i + len;
    while ((j = s.indexOf('`', j)) !== -1) {
      var m = runLen(s, j, '`');
      if (m === len) {
        var body = s.slice(i + len, j);
        if (body.length > 1 && body.charAt(0) === ' ' && body.charAt(body.length - 1) === ' ' && body.trim()) body = body.slice(1, -1);
        return { code: body, end: j + m };
      }
      j += m;
    }
    this.noCode[len] = i;
    return null;
  };

  Inline.prototype.link = function (i) {
    // [label](url "optional title")
    var s = this.s;
    var limit = Math.min(this.n, i + 1 + 600);
    var depth = 0, j;
    for (j = i + 1; j < limit; j++) {
      var c = s.charAt(j);
      if (c === '\\') { j++; continue; }
      if (c === '[') depth++;
      else if (c === ']') { if (depth === 0) break; depth--; }
    }
    if (j >= limit || s.charAt(j) !== ']' || s.charAt(j + 1) !== '(') return null;
    var label = s.slice(i + 1, j);
    var k = j + 2;
    while (s.charAt(k) === ' ' || s.charAt(k) === '\t') k++;
    var start = k, par = 0;
    var uLimit = Math.min(this.n, start + 2048);
    for (; k < uLimit; k++) {
      var ch = s.charAt(k);
      if (/\s/.test(ch)) break;
      if (ch === '(') par++;
      else if (ch === ')') { if (par === 0) break; par--; }
    }
    var url = s.slice(start, k);
    while (s.charAt(k) === ' ' || s.charAt(k) === '\t') k++;
    var q = s.charAt(k);
    if ((q === '"' || q === "'") && k > start + url.length) {
      var qe = s.indexOf(q, k + 1);
      if (qe === -1) return null;
      k = qe + 1;
      while (s.charAt(k) === ' ' || s.charAt(k) === '\t') k++;
    }
    if (s.charAt(k) !== ')' || !url) return null;
    return { label: label, href: safeUrl(url), end: k + 1 };
  };

  Inline.prototype.autolink = function (i) {
    // <https://example.com>
    var m = /^<(https?:\/\/[^\s<>]{1,2048})>/i.exec(this.s.slice(i, i + 2060));
    if (!m) return null;
    var href = safeUrl(m[1]);
    return href ? { href: href, text: m[1], end: i + m[0].length } : null;
  };

  Inline.prototype.urlBoundary = function (i) {
    if (i === 0) return true;
    var p = this.s.charAt(i - 1);
    return !WORDCH.test(p) && p !== '/' && p !== '@' && p !== '.' && p !== '-' && p !== '&';
  };

  Inline.prototype.bareUrl = function (i) {
    var m = /^https?:\/\/[^\s<>"'`\[\]{}|\\^]+/i.exec(this.s.slice(i, i + 2100));
    if (!m) return null;
    var url = m[0];
    for (;;) {
      var last = url.charAt(url.length - 1);
      if (/[.,;:!?*_~]/.test(last)) { url = url.slice(0, -1); continue; }
      if (last === ')' && (url.split(')').length > url.split('(').length)) { url = url.slice(0, -1); continue; }
      break;
    }
    var href = safeUrl(url);
    return href ? { href: href, text: url, end: i + url.length } : null;
  };

  Inline.prototype.isUrlStart = function (i) {
    var c = this.s.charAt(i);
    return (c === 'h' || c === 'H') && this.urlBoundary(i) && /^https?:\/\//i.test(this.s.substr(i, 8));
  };

  /** Index of the closing delimiter run for an emphasis opened before `from`, or -1. */
  Inline.prototype.findCloser = function (from, ch, len) {
    var s = this.s;
    var limit = Math.min(this.n, from + 6000);
    var j = from;
    while (j < limit) {
      var c = s.charAt(j);
      if (c === '\\') { j += 2; continue; }
      if (c === '`') { var cs = this.codeSpan(j); j = cs ? cs.end : j + runLen(s, j, '`'); continue; }
      if (!this.noLinks) {
        if (c === '[') { var lk = this.link(j); if (lk) { j = lk.end; continue; } }
        if (c === '<') { var al = this.autolink(j); if (al) { j = al.end; continue; } }
        if (this.isUrlStart(j)) { var bu = this.bareUrl(j); if (bu) { j = bu.end; continue; } }
      }
      if (c === ch) {
        var run = runLen(s, j, ch);
        var prev = s.charAt(j - 1);
        var after = s.charAt(j + run);
        var ok = run >= len && j > from && !/\s/.test(prev);
        if (ok && ch === '_' && after && WORDCH.test(after)) ok = false;
        if (ok && ch === '~' && run !== 2) ok = false;
        if (ok && len === 1 && run === 2) ok = false; // a ** pair inside *em*: leave it for the inner pass
        if (ok) return j + (run - len);
        j += run;
        continue;
      }
      j++;
    }
    return -1;
  };

  Inline.prototype.emphasis = function (i) {
    var s = this.s;
    var ch = s.charAt(i);
    var run = runLen(s, i, ch);
    var len;
    if (ch === '~') { if (run !== 2) return null; len = 2; }
    else len = run >= 2 ? 2 : 1;
    var next = s.charAt(i + len);
    if (!next || /\s/.test(next)) return null;
    if (ch === '_' && i > 0 && WORDCH.test(s.charAt(i - 1))) return null;
    var key = ch + len;
    if (this.noClose[key] != null && i >= this.noClose[key]) return null;
    var close = this.findCloser(i + len, ch, len);
    if (close < 0) { this.noClose[key] = i; return null; }
    return { tag: ch === '~' ? 'del' : len === 2 ? 'strong' : 'em', inner: s.slice(i + len, close), end: close + len };
  };

  /** Parse the whole string into `parent`. */
  Inline.prototype.into = function (parent) {
    var s = this.s, n = this.n;
    var buf = '';
    var i = 0;
    function flush() { if (buf) { parent.appendChild(text(buf)); buf = ''; } }
    while (i < n) {
      var c = s.charAt(i);
      if (c === '\\' && i + 1 < n && PUNCT.test(s.charAt(i + 1))) { buf += s.charAt(i + 1); i += 2; continue; }
      if (c === '`') {
        var cs = this.codeSpan(i);
        if (cs) {
          flush();
          var code = el('code');
          code.textContent = cs.code;
          parent.appendChild(code);
          i = cs.end;
        } else {
          var bt = runLen(s, i, '`');
          buf += s.substr(i, bt);
          i += bt;
        }
        continue;
      }
      if (!this.noLinks) {
        if (c === '[') {
          var lk = this.link(i);
          if (lk) {
            flush();
            if (lk.href) {
              var a = anchor(lk.href);
              if (lk.label.trim()) new Inline(lk.label, true, this.depth + 1).into(a);
              else a.textContent = shortUrl(lk.href);
              parent.appendChild(a);
            } else if (lk.label.trim()) {
              new Inline(lk.label, true, this.depth + 1).into(parent); // unsafe target: keep the words only
            }
            i = lk.end;
            continue;
          }
        } else if (c === '<') {
          var al = this.autolink(i);
          if (al) {
            flush();
            var a2 = anchor(al.href);
            a2.textContent = shortUrl(al.text);
            parent.appendChild(a2);
            i = al.end;
            continue;
          }
        } else if (this.isUrlStart(i)) {
          var bu = this.bareUrl(i);
          if (bu) {
            flush();
            var a3 = anchor(bu.href);
            a3.textContent = shortUrl(bu.text);
            parent.appendChild(a3);
            i = bu.end;
            continue;
          }
        }
      }
      if (c === '*' || c === '_' || c === '~') {
        var em = this.depth < MAX_DEPTH ? this.emphasis(i) : null;
        if (em) {
          flush();
          var node = el(em.tag);
          new Inline(em.inner, this.noLinks, this.depth + 1).into(node);
          parent.appendChild(node);
          i = em.end;
        } else {
          var r = runLen(s, i, c);
          buf += s.substr(i, r);
          i += r;
        }
        continue;
      }
      buf += c;
      i++;
    }
    flush();
    return parent;
  };

  function inlineInto(parent, s) { return new Inline(String(s), false, 0).into(parent); }

  // ------------------------------------------------------------------ block parser
  var RE_FENCE = /^\s{0,3}(`{3,}|~{3,})(.*)$/;
  var RE_HEAD = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var RE_HR = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
  var RE_UL = /^(\s*)[-*+•]\s+(.*)$/;
  var RE_OL = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;
  var RE_QUOTE = /^\s{0,3}>\s?(.*)$/;
  var RE_ROW = /^\s*\|.*\|\s*$/;
  var RE_SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;

  function fenceOpen(line) {
    var m = RE_FENCE.exec(line);
    if (!m) return null;
    var marker = m[1];
    var info = m[2] || '';
    if (marker.charAt(0) === '`' && info.indexOf('`') !== -1) return null; // ```inline``` code, not a fence
    var lang = (info.trim().split(/\s+/)[0] || '').replace(/[^\w+#.\-]/g, '').slice(0, 20);
    return { ch: marker.charAt(0), len: marker.length, lang: lang };
  }
  function fenceCloses(line, f) {
    var m = /^\s{0,3}(`{3,}|~{3,})\s*$/.exec(line);
    return !!(m && m[1].charAt(0) === f.ch && m[1].length >= f.len);
  }

  function splitRow(line) {
    var t = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    var cells = [], cur = '';
    for (var i = 0; i < t.length; i++) {
      var c = t.charAt(i);
      if (c === '\\' && t.charAt(i + 1) === '|') { cur += '|'; i++; continue; }
      if (c === '|') { cells.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  // Private-use code points have no meaning in model text (they render as boxes): drop them.
  var PRIVATE_USE = new RegExp('[' + String.fromCharCode(0xE000) + '-' + String.fromCharCode(0xF8FF) + ']', 'g');

  function stripInvisible(t) { return t.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '').replace(PRIVATE_USE, ''); }

  function cleanSource(md) {
    var src = String(md == null ? '' : md);
    var cut = false;
    if (src.length > MAX_SOURCE) { src = src.slice(0, MAX_SOURCE); cut = true; }
    src = stripInvisible(src.replace(/\r\n?/g, '\n'));
    return { src: src, cut: cut };
  }

  /** Build a DocumentFragment from markdown. */
  function fragment(md) {
    var c = cleanSource(md);
    var lines = c.src.split('\n');
    var out = doc.createDocumentFragment();
    var para = [];
    var list = null; // {node, tag}
    var quote = [];

    function lines2(parent, arr) {
      for (var k = 0; k < arr.length; k++) {
        if (k) parent.appendChild(el('br'));
        inlineInto(parent, arr[k]);
      }
      return parent;
    }
    function flushPara() { if (para.length) { out.appendChild(lines2(el('p'), para)); para = []; } }
    function flushList() { list = null; }
    function flushQuote() { if (quote.length) { out.appendChild(lines2(el('blockquote'), quote)); quote = []; } }
    function flushAll() { flushPara(); flushList(); flushQuote(); }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      var f = fenceOpen(line);
      if (f) {
        flushAll();
        var body = [];
        var closed = false;
        for (i = i + 1; i < lines.length; i++) {
          if (fenceCloses(lines[i], f)) { closed = true; break; }
          body.push(lines[i]);
        }
        var pre = el('pre', 'md-code' + (closed ? '' : ' open'));
        if (f.lang) pre.setAttribute('data-lang', f.lang);
        var code = el('code');
        code.textContent = body.join('\n');
        pre.appendChild(code);
        out.appendChild(pre);
        continue;
      }

      if (!line.trim()) { flushAll(); continue; }

      var h = RE_HEAD.exec(line);
      if (h) {
        flushAll();
        var level = Math.min(h[1].length + 2, 6); // # -> h3, ## -> h4, ### -> h5
        out.appendChild(inlineInto(el('h' + level, 'md-h'), h[2]));
        continue;
      }

      if (RE_HR.test(line)) { flushAll(); out.appendChild(el('hr')); continue; }

      if (RE_ROW.test(line) && i + 1 < lines.length && RE_SEP.test(lines[i + 1])) {
        flushAll();
        var wrap = el('div', 'md-table');
        var table = el('table');
        var thead = el('thead'), htr = el('tr');
        splitRow(line).forEach(function (cell) { htr.appendChild(inlineInto(el('th'), cell)); });
        thead.appendChild(htr);
        table.appendChild(thead);
        var tbody = el('tbody');
        for (i = i + 2; i < lines.length && RE_ROW.test(lines[i]); i++) {
          var tr = el('tr');
          splitRow(lines[i]).forEach(function (cell) { tr.appendChild(inlineInto(el('td'), cell)); });
          tbody.appendChild(tr);
        }
        i--;
        table.appendChild(tbody);
        wrap.appendChild(table);
        out.appendChild(wrap);
        continue;
      }

      var q = RE_QUOTE.exec(line);
      if (q) { flushPara(); flushList(); quote.push(q[1]); continue; }

      var ul = RE_UL.exec(line);
      var ol = ul ? null : RE_OL.exec(line);
      if (ul || ol) {
        flushPara(); flushQuote();
        var tag = ul ? 'ul' : 'ol';
        var indent = (ul ? ul[1] : ol[1]).replace(/\t/g, '    ').length;
        var itemText = ul ? ul[2] : ol[3];
        if (list && list.tag !== tag && indent === 0) flushList();
        if (!list) {
          var ln = el(tag);
          if (ol) {
            var start = parseInt(ol[2], 10);
            if (isFinite(start) && start !== 1) ln.setAttribute('start', String(Math.min(start, 999999999)));
          }
          out.appendChild(ln);
          list = { node: ln, tag: tag };
        }
        var li = el('li', indent >= 2 ? 'sub' : '');
        if (!li.className) li.removeAttribute('class');
        li._src = itemText;
        inlineInto(li, itemText);
        list.node.appendChild(li);
        continue;
      }

      // indented continuation of a list item
      if (list && /^\s{2,}\S/.test(line)) {
        var last = list.node.lastChild;
        if (last) {
          last._src = (last._src || '') + ' ' + line.trim();
          last.textContent = '';
          inlineInto(last, last._src);
        }
        continue;
      }

      flushList(); flushQuote();
      para.push(line);
    }
    flushAll();
    if (c.cut) {
      var note = el('p', 'md-cut');
      note.textContent = '… (message truncated for display)';
      out.appendChild(note);
    }
    return out;
  }

  /** Replace el's children with the rendered markdown. */
  function renderInto(node, md) {
    node.textContent = '';
    node.appendChild(fragment(md));
    return node;
  }

  /** HTML string (serialised from the DOM built above). */
  function render(md) {
    var box = el('div');
    box.appendChild(fragment(md));
    return box.innerHTML;
  }

  /**
   * Largest index such that md.slice(0, i) consists only of finished blocks: the position just
   * after a blank line outside a code fence, or just after a closing fence line. Rendering
   * slice(0, i) and slice(i) separately yields exactly the same DOM as rendering md whole.
   */
  function stableCut(md) {
    var s = String(md == null ? '' : md);
    if (s.length > MAX_SOURCE || /\r/.test(s)) return 0;
    var pos = 0, cut = 0, fence = null;
    while (pos < s.length) {
      var nl = s.indexOf('\n', pos);
      if (nl === -1) break; // the last line is still being written
      var line = stripInvisible(s.slice(pos, nl)); // judge lines exactly as fragment() will
      if (fence) {
        if (fenceCloses(line, fence)) { fence = null; cut = nl + 1; }
      } else {
        var f = fenceOpen(line);
        if (f) fence = f;
        else if (!line.trim()) cut = nl + 1;
      }
      pos = nl + 1;
    }
    return cut;
  }

  global.AtlasMD = {
    render: render,
    fragment: fragment,
    renderInto: renderInto,
    stableCut: stableCut,
    escapeHtml: escapeHtml,
    safeUrl: safeUrl
  };
})(window);
