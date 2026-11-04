/* ATLAS — safe mini-markdown.
 * The whole input is HTML-escaped FIRST; only then are a small set of markdown
 * constructs turned into tags. Links are emitted only for http(s) URLs.
 */
(function (global) {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }

  // Placeholders use private-use code points that are stripped from input up front.
  var PH_OPEN = '', PH_CLOSE = '';

  // An escaped URL must not swallow escaped quotes/brackets that follow it.
  var URL_BODY = '(?:(?!&quot;|&#39;|&lt;|&gt;)[^\\s<>])+';
  var BARE_URL = new RegExp('(^|[\\s(\\[])(https?:\\/\\/' + URL_BODY + ')', 'gi');
  var MD_LINK = /\[([^\]\n]{1,400})\]\(\s*(https?:\/\/[^\s)<>]+)\s*\)/gi;
  var SAFE_URL = /^https?:\/\/[^\s<>"']+$/i;

  function trimUrl(url) {
    // Trailing punctuation is almost never part of the link.
    var tail = '';
    var m = url.match(/(?:[.,;:!?]|&#39;|&quot;)+$/);
    if (m) { tail = m[0]; url = url.slice(0, -tail.length); }
    if (/\)$/.test(url) && url.indexOf('(') === -1) { tail = ')' + tail; url = url.slice(0, -1); }
    return [url, tail];
  }

  function anchor(url, text) {
    if (!SAFE_URL.test(url.replace(/&amp;/g, '&'))) return text;
    return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + '</a>';
  }

  function emphasis(s) {
    s = s.replace(/\*\*(?=\S)([^*\n]*?\S)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^\w])__(?=\S)([^_\n]*?\S)__(?!\w)/g, '$1<strong>$2</strong>');
    s = s.replace(/(^|[^*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?!\*)/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^_\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/~~(?=\S)([^~\n]*?\S)~~/g, '<del>$1</del>');
    return s;
  }

  /** Inline formatting on ALREADY-ESCAPED text. */
  function inline(s) {
    var slots = [];
    function hold(html) { slots.push(html); return PH_OPEN + (slots.length - 1) + PH_CLOSE; }

    s = s.replace(/`([^`\n]+)`/g, function (_, code) { return hold('<code>' + code + '</code>'); });
    s = s.replace(MD_LINK, function (_, text, url) { return hold(anchor(url, emphasis(text))); });
    s = s.replace(BARE_URL, function (_, lead, url) {
      var parts = trimUrl(url);
      var shown = parts[0].replace(/^https?:\/\//i, '');
      if (shown.length > 48) shown = shown.slice(0, 45) + '…';
      return lead + hold(anchor(parts[0], shown)) + parts[1];
    });
    s = emphasis(s);
    var re = new RegExp(PH_OPEN + '(\\d+)' + PH_CLOSE, 'g');
    // Placeholders can nest (a link held inside another slot), so resolve until stable.
    for (var guard = 0; guard < 3 && re.test(s); guard++) {
      re.lastIndex = 0;
      s = s.replace(re, function (_, i) { return slots[+i] || ''; });
    }
    return s;
  }

  function isTableSep(line) { return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(line); }
  function splitRow(line) {
    var t = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return t.split('|').map(function (c) { return c.trim(); });
  }

  /** Render markdown to safe HTML. */
  function render(md) {
    var src = String(md == null ? '' : md)
      .replace(/\r\n?/g, '\n')
      .replace(/[]/g, '');
    var lines = escapeHtml(src).split('\n'); // escape FIRST

    var html = '';
    var para = [];
    var list = null; // {tag, start, items: []}
    var quote = [];

    function flushPara() {
      if (para.length) { html += '<p>' + para.map(inline).join('<br>') + '</p>'; para = []; }
    }
    function flushList() {
      if (!list) return;
      var open = '<' + list.tag + (list.tag === 'ol' && list.start !== 1 ? ' start="' + list.start + '"' : '') + '>';
      html += open + list.items.map(function (it) {
        return '<li' + (it.sub ? ' class="sub"' : '') + '>' + inline(it.text) + '</li>';
      }).join('') + '</' + list.tag + '>';
      list = null;
    }
    function flushQuote() {
      if (quote.length) { html += '<blockquote>' + quote.map(inline).join('<br>') + '</blockquote>'; quote = []; }
    }
    function flushAll() { flushPara(); flushList(); flushQuote(); }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];

      // fenced code (``` or ~~~), possibly still open while streaming
      var fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.\-]*)\s*$/);
      if (fence) {
        flushAll();
        var marker = fence[1].charAt(0);
        var body = [];
        var closed = false;
        for (i = i + 1; i < lines.length; i++) {
          if (new RegExp('^\\s*\\' + marker + '{3,}\\s*$').test(lines[i])) { closed = true; break; }
          body.push(lines[i]);
        }
        var lang = fence[2] ? ' data-lang="' + fence[2].slice(0, 20) + '"' : '';
        html += '<pre class="md-code' + (closed ? '' : ' open') + '"' + lang + '><code>' + body.join('\n') + '</code></pre>';
        continue;
      }

      if (/^\s*$/.test(line)) { flushAll(); continue; }

      var h = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (h) {
        flushAll();
        var level = Math.min(h[1].length + 2, 6); // # -> h3, ## -> h4, ### -> h5
        html += '<h' + level + ' class="md-h">' + inline(h[2]) + '</h' + level + '>';
        continue;
      }

      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flushAll(); html += '<hr>'; continue; }

      // simple pipe tables
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        flushAll();
        var head = splitRow(line);
        var rows = [];
        for (i = i + 2; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i++) rows.push(splitRow(lines[i]));
        i--;
        html += '<div class="md-table"><table><thead><tr>' + head.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' + rows.map(function (r) {
            return '<tr>' + r.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
          }).join('') + '</tbody></table></div>';
        continue;
      }

      var q = line.match(/^\s*&gt;\s?(.*)$/);
      if (q) { flushPara(); flushList(); quote.push(q[1]); continue; }

      var ul = line.match(/^(\s*)[-*+•]\s+(.*)$/);
      var ol = line.match(/^(\s*)(\d{1,3})[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara(); flushQuote();
        var tag = ul ? 'ul' : 'ol';
        var indent = (ul ? ul[1] : ol[1]).length;
        var text = ul ? ul[2] : ol[3];
        if (list && list.tag !== tag && indent === 0) flushList();
        if (!list) list = { tag: tag, start: ol ? parseInt(ol[2], 10) || 1 : 1, items: [] };
        list.items.push({ text: text, sub: indent >= 2 });
        continue;
      }

      // indented continuation of a list item
      if (list && /^\s{2,}\S/.test(line)) {
        list.items[list.items.length - 1].text += ' ' + line.trim();
        continue;
      }

      flushList(); flushQuote();
      para.push(line);
    }
    flushAll();
    return html;
  }

  /** Plain text suitable for speech: no code, symbols or URLs. */
  function toSpeech(text) {
    return String(text || '')
      .replace(/```[\s\S]*?(```|$)/g, ' ')
      .replace(/`([^`]*)`/g, '$1')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/gi, ' ')
      .replace(/www\.\S+/gi, ' ')
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')
      .replace(/^\s*[-*+•]\s+/gm, '')
      .replace(/^\s*>\s?/gm, '')
      .replace(/\|/g, ', ')
      .replace(/(\*\*|__|~~)/g, '')
      .replace(/(^|\s)[*_]+(?=\S)/g, '$1')
      .replace(/(\S)[*_]+(?=[\s.,;:!?)]|$)/g, '$1')
      .replace(/[#*_~^<>{}\[\]\\]/g, ' ')
      .replace(/\s*—\s*/g, ', ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  global.AtlasMD = { render: render, escapeHtml: escapeHtml, toSpeech: toSpeech };
})(window);
