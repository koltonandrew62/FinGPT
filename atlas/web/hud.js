/* ATLAS — HALO core renderer.
 * A layered, concentric-ring instrument drawn on a DPR-aware <canvas>:
 * reticle wings, bracket ring, dense tick ring with bearings, segmented arcs,
 * 96 radial spectrum bars with peak-hold, dashed/sweep rings, a glowing nucleus
 * and small orbiting markers. Colour, speed and energy ease between states.
 */
(function (global) {
  'use strict';

  var TAU = Math.PI * 2;
  var BARS = 96;

  var STATES = {
    dormant:   { rgb: [95, 225, 255],  speed: 0.35, energy: 0.10, glow: 0.55 },
    standby:   { rgb: [95, 225, 255],  speed: 1.00, energy: 0.25, glow: 0.80 },
    listening: { rgb: [150, 242, 255], speed: 1.55, energy: 0.70, glow: 1.15 },
    thinking:  { rgb: [255, 181, 71],  speed: 2.60, energy: 0.50, glow: 1.00 },
    speaking:  { rgb: [118, 232, 255], speed: 1.25, energy: 0.75, glow: 1.10 },
    error:     { rgb: [255, 90, 106],  speed: 0.50, energy: 0.40, glow: 0.95 }
  };

  // Angular velocities (rad/s at speed 1) for each rotating layer.
  var SPIN = [0.030, 0.022, -0.065, 0.045, 0.16, -0.11, 0.85, 0.32, -0.55, 0.12, -0.028];

  // Segmented arc ring pattern: [start°, length°]
  var SEGMENTS = [[0, 64], [72, 38], [118, 96], [222, 18], [248, 84], [340, 12]];
  var INNER_SEGMENTS = [[8, 104], [128, 104], [248, 104]];

  function clamp(v, a, b) { return v < a ? a : v > b ? b : v; }
  function easeOut(p) { p = clamp(p, 0, 1); return 1 - Math.pow(1 - p, 3); }
  function deg(d) { return d * Math.PI / 180; }

  var reduceMQ = null;
  try { reduceMQ = global.matchMedia ? global.matchMedia('(prefers-reduced-motion: reduce)') : null; } catch (e) { reduceMQ = null; }

  function Core(canvas, opts) {
    opts = opts || {};
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.state = opts.state || 'standby';
    this.compact = !!opts.compact;
    this.labels = opts.labels !== false;
    this.wings = opts.wings !== false;
    var s = STATES[this.state] || STATES.standby;
    this.rgb = s.rgb.slice();
    this.speed = s.speed;
    this.energy = s.energy;
    this.glow = s.glow;
    this.levels = new Float32Array(BARS);
    this.peaks = new Float32Array(BARS);
    this.input = null;
    this.inputAt = 0;
    this.pulseV = 0;
    this.ripples = [];
    this.t = 0;
    this.angles = new Float32Array(SPIN.length);
    for (var i = 0; i < SPIN.length; i++) this.angles[i] = Math.random() * TAU;
    this.boot = opts.booted ? 1 : 0;
    this.booting = !!opts.booted;
    this.reduced = !!(reduceMQ && reduceMQ.matches);
    this.running = false;
    this.raf = 0;
    this.last = 0;
    this.w = 0; this.h = 0; this.dpr = 1;
    this.onGeometry = opts.onGeometry || null;
    this._frame = this._frame.bind(this);
    this._onVis = this._visibility.bind(this);
    this._onResize = this.resize.bind(this);
    document.addEventListener('visibilitychange', this._onVis);
    var self = this;
    if (reduceMQ) {
      var onRM = function (e) { self.reduced = e.matches; if (self.reduced) self.boot = Math.max(self.boot, self.booting ? 1 : 0); self.draw(); };
      if (reduceMQ.addEventListener) reduceMQ.addEventListener('change', onRM);
      else if (reduceMQ.addListener) reduceMQ.addListener(onRM);
    }
    if (typeof ResizeObserver === 'function') {
      this.ro = new ResizeObserver(function () { self.resize(); });
      this.ro.observe(canvas);
    } else {
      global.addEventListener('resize', this._onResize);
    }
    this.resize();
  }

  Core.STATES = STATES;

  Core.prototype.setState = function (state) {
    if (STATES[state]) this.state = state;
  };

  /** Feed normalised (0..1) per-bar levels, e.g. from a mic analyser. */
  Core.prototype.setInput = function (arr) {
    this.input = arr;
    this.inputAt = performance.now();
  };

  /** A speech boundary or other transient: brief swell plus a ripple. */
  Core.prototype.pulse = function (strength) {
    var s = strength == null ? 1 : strength;
    this.pulseV = Math.min(1.4, this.pulseV + 0.55 * s);
    if (!this.reduced && this.ripples.length < 6) this.ripples.push({ t: 0, s: s });
  };

  /** Play the power-up sequence. */
  Core.prototype.powerUp = function () {
    this.booting = true;
    this.boot = this.reduced ? 1 : 0;
  };

  Core.prototype.geometry = function () {
    var cx = this.w / 2, cy = this.h / 2;
    var R = Math.max(0, Math.min(this.w / 2, this.h / 2) - 6);
    return { cx: cx, cy: cy, R: R };
  };

  Core.prototype.resize = function () {
    var rect = this.canvas.getBoundingClientRect();
    var dpr = clamp(global.devicePixelRatio || 1, 1, 3);
    var bw = Math.max(1, Math.round(rect.width * dpr));
    var bh = Math.max(1, Math.round(rect.height * dpr));
    if (this.canvas.width !== bw || this.canvas.height !== bh) {
      this.canvas.width = bw;
      this.canvas.height = bh;
    }
    this.w = rect.width; this.h = rect.height; this.dpr = dpr;
    this._watchDpr(dpr);
    if (this.onGeometry) { try { this.onGeometry(this.geometry()); } catch (e) { /* ignore */ } }
    this.draw();
  };

  Core.prototype._watchDpr = function (dpr) {
    if (this._dprWatched === dpr || !global.matchMedia) return;
    this._dprWatched = dpr;
    var self = this;
    try {
      var mq = global.matchMedia('(resolution: ' + dpr + 'dppx)');
      var fn = function () { self._dprWatched = null; self.resize(); };
      if (mq.addEventListener) mq.addEventListener('change', fn, { once: true });
      else if (mq.addListener) mq.addListener(fn);
    } catch (e) { /* ignore */ }
  };

  Core.prototype.start = function () {
    if (this.running) return;
    this.running = true;
    this.last = performance.now();
    if (!document.hidden && !this.raf) this.raf = requestAnimationFrame(this._frame);
  };

  Core.prototype.stop = function () {
    this.running = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.raf = 0;
  };

  Core.prototype.destroy = function () {
    this.stop();
    document.removeEventListener('visibilitychange', this._onVis);
    if (this.ro) this.ro.disconnect();
    global.removeEventListener('resize', this._onResize);
  };

  Core.prototype._visibility = function () {
    if (document.hidden) {
      if (this.raf) cancelAnimationFrame(this.raf);
      this.raf = 0;
    } else if (this.running && !this.raf) {
      this.last = performance.now();
      this.raf = requestAnimationFrame(this._frame);
    }
  };

  Core.prototype._frame = function (now) {
    this.raf = 0;
    if (!this.running) return;
    var elapsed = now - this.last;
    // Reduced motion: a calm ~15 fps with no rotation.
    if (this.reduced && elapsed < 66) { this.raf = requestAnimationFrame(this._frame); return; }
    var dt = clamp(elapsed / 1000, 0, 0.1);
    this.last = now;
    this.update(dt);
    this.draw();
    if (this.running && !document.hidden) this.raf = requestAnimationFrame(this._frame);
  };

  Core.prototype.update = function (dt) {
    var target = STATES[this.state] || STATES.standby;
    var k = 1 - Math.exp(-dt * 3.0);
    for (var c = 0; c < 3; c++) this.rgb[c] += (target.rgb[c] - this.rgb[c]) * k;
    this.speed += (target.speed - this.speed) * k;
    this.energy += (target.energy - this.energy) * k;
    this.glow += (target.glow - this.glow) * k;
    this.t += dt;

    var sp = this.reduced ? 0 : this.speed;
    for (var i = 0; i < SPIN.length; i++) this.angles[i] = (this.angles[i] + SPIN[i] * sp * dt) % TAU;

    this.pulseV *= Math.exp(-dt * 4.2);
    for (var r = this.ripples.length - 1; r >= 0; r--) {
      this.ripples[r].t += dt / 1.3;
      if (this.ripples[r].t >= 1) this.ripples.splice(r, 1);
    }
    if (this.booting && this.boot < 1) this.boot = this.reduced ? 1 : Math.min(1, this.boot + dt / 1.9);

    // Spectrum targets
    var t = this.t, st = this.state;
    var live = this.input && (performance.now() - this.inputAt < 300);
    var head = t * 2.3;
    for (var b = 0; b < BARS; b++) {
      var a = (b + 0.5) / BARS * TAU;
      var m = Math.min(b, BARS - 1 - b); // mirror index (symmetry about vertical axis)
      var idle = 0.06 + 0.11 * Math.pow((0.5 + 0.5 * Math.sin(t * 1.6 + m * 0.52)) * (0.5 + 0.5 * Math.sin(t * 0.83 - m * 0.23)), 1.3);
      var v;
      if (live) {
        v = 0.04 + (this.input[b] || 0);
      } else if (st === 'thinking') {
        var d1 = Math.max(0, Math.cos(a - head));
        var d2 = Math.max(0, Math.cos(a - head + Math.PI));
        v = idle + 0.5 * Math.pow(d1, 12) + 0.22 * Math.pow(d2, 12);
      } else if (st === 'speaking') {
        var n = 0.5 + 0.5 * Math.sin(m * 1.7 + t * 9.0) * Math.sin(m * 0.61 - t * 5.3);
        v = idle + (0.12 + this.pulseV * 0.55) * (0.25 + 0.75 * n);
      } else if (st === 'listening') {
        v = idle + 0.06 * (0.5 + 0.5 * Math.sin(m * 0.9 + t * 6));
      } else if (st === 'error') {
        v = idle * 0.7 + 0.08 * (Math.sin(t * 23 + m) > 0.6 ? 1 : 0);
      } else {
        v = idle;
      }
      v = clamp(v, 0, 1);
      var cur = this.levels[b];
      this.levels[b] = v > cur ? cur + (v - cur) * Math.min(1, dt * 20) : cur + (v - cur) * Math.min(1, dt * 5.5);
      if (this.levels[b] > this.peaks[b]) this.peaks[b] = this.levels[b];
      else this.peaks[b] = Math.max(this.levels[b], this.peaks[b] - dt * 0.22);
    }
  };

  Core.prototype.draw = function () {
    var ctx = this.ctx;
    if (!ctx || !this.w) return;
    var dpr = this.dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.w, this.h);
    var g = this.geometry();
    var cx = g.cx, cy = g.cy, R = g.R;
    if (R < 24) return;

    var r0 = Math.round(this.rgb[0]), g0 = Math.round(this.rgb[1]), b0 = Math.round(this.rgb[2]);
    var pre = 'rgba(' + r0 + ',' + g0 + ',' + b0 + ',';
    function col(a) { return pre + clamp(a, 0, 1).toFixed(3) + ')'; }
    var hair = dpr >= 2 ? 0.75 : 1;
    var boot = this.booting ? this.boot : 0;
    var A = this.angles;
    var t = this.t;
    var flicker = this.state === 'error' ? 0.82 + 0.18 * Math.sin(t * 31) : 1;
    var small = R < 100;

    function layer(delay) { return easeOut((boot - delay) / 0.42); }

    ctx.lineCap = 'butt';

    // ---- ambient haze
    var haze = ctx.createRadialGradient(cx, cy, R * 0.1, cx, cy, R * 1.25);
    haze.addColorStop(0, col(0.10 * this.glow * Math.max(boot, 0.25)));
    haze.addColorStop(0.45, col(0.035 * this.glow));
    haze.addColorStop(1, col(0));
    ctx.fillStyle = haze;
    ctx.fillRect(0, 0, this.w, this.h);

    // ---- reticle wings
    var pw = layer(0.55);
    if (this.wings && pw > 0) {
      var gapIn = R * 1.04;
      var avail = Math.min(cx - gapIn - 8, R * 1.15);
      if (avail > 30) {
        ctx.save();
        ctx.globalAlpha = pw * flicker;
        ctx.lineWidth = hair;
        for (var side = -1; side <= 1; side += 2) {
          var x1 = cx + side * gapIn, x2 = cx + side * (gapIn + avail * pw);
          var grad = ctx.createLinearGradient(x1, 0, x2, 0);
          grad.addColorStop(0, col(0.45));
          grad.addColorStop(1, col(0));
          ctx.strokeStyle = grad;
          ctx.beginPath(); ctx.moveTo(x1, cy + 0.5); ctx.lineTo(x2, cy + 0.5); ctx.stroke();
          // ticks along the wing
          ctx.beginPath();
          for (var k = 1; k * 9 < avail * pw; k++) {
            var xx = Math.round(x1 + side * k * 9) + 0.5;
            var len = k % 5 === 0 ? 6 : 2.5;
            ctx.moveTo(xx, cy - len); ctx.lineTo(xx, cy);
          }
          ctx.strokeStyle = col(0.22);
          ctx.stroke();
          // end bracket
          var bx = cx + side * (gapIn + 4);
          ctx.strokeStyle = col(0.75);
          ctx.lineWidth = 1.25;
          ctx.beginPath();
          ctx.moveTo(bx + side * 6, cy - 9); ctx.lineTo(bx, cy - 9); ctx.lineTo(bx, cy + 9); ctx.lineTo(bx + side * 6, cy + 9);
          ctx.stroke();
          ctx.lineWidth = hair;
        }
        // live bearing readouts
        if (!small) {
          ctx.font = '500 9px "JetBrains Mono", ui-monospace, monospace';
          ctx.fillStyle = col(0.55);
          ctx.textBaseline = 'bottom';
          ctx.textAlign = 'left';
          var brg = ((A[1] * 180 / Math.PI) % 360 + 360) % 360;
          ctx.fillText('BRG ' + brg.toFixed(1).padStart(5, '0') + '°', cx + gapIn + 14, cy - 8);
          ctx.textAlign = 'right';
          var lv = 0; for (var q = 0; q < BARS; q++) lv += this.levels[q];
          ctx.fillText('AMP ' + (lv / BARS * 100).toFixed(1).padStart(4, '0'), cx - gapIn - 14, cy - 8);
          ctx.textBaseline = 'top';
          ctx.fillStyle = col(0.32);
          ctx.fillText('SPD ×' + this.speed.toFixed(2), cx - gapIn - 14, cy + 6);
          ctx.textAlign = 'left';
          ctx.fillText('NRG ' + Math.round(this.energy * 100) + '%', cx + gapIn + 14, cy + 6);
        }
        ctx.restore();
      }
    }

    // ---- outer ring + rotating brackets
    var p0 = layer(0);
    if (p0 > 0) {
      var ro = R * (0.86 + 0.14 * p0) * (1 + 0.012 * this.pulseV);
      ctx.globalAlpha = p0 * flicker;
      ctx.strokeStyle = col(0.20);
      ctx.lineWidth = hair;
      ctx.beginPath(); ctx.arc(cx, cy, ro, 0, TAU); ctx.stroke();
      ctx.lineWidth = Math.max(2, R * 0.011);
      ctx.strokeStyle = col(0.78);
      for (var qb = 0; qb < 4; qb++) {
        var ab = A[0] + qb * Math.PI / 2;
        ctx.beginPath(); ctx.arc(cx, cy, ro, ab - deg(7), ab + deg(7)); ctx.stroke();
      }
      // fine end-caps on brackets
      ctx.lineWidth = hair;
      ctx.strokeStyle = col(0.5);
      for (var qc = 0; qc < 4; qc++) {
        var ac = A[0] + qc * Math.PI / 2;
        for (var e = -1; e <= 1; e += 2) {
          var ae = ac + e * deg(9.5);
          ctx.beginPath();
          ctx.moveTo(cx + Math.cos(ae) * (ro - 4), cy + Math.sin(ae) * (ro - 4));
          ctx.lineTo(cx + Math.cos(ae) * (ro + 4), cy + Math.sin(ae) * (ro + 4));
          ctx.stroke();
        }
      }
      // orbiting chevron outside the ring
      var ao = A[7];
      var rr = ro + 7;
      ctx.fillStyle = col(0.95);
      ctx.beginPath();
      ctx.moveTo(cx + Math.cos(ao) * (rr - 4), cy + Math.sin(ao) * (rr - 4));
      ctx.lineTo(cx + Math.cos(ao + 0.035) * (rr + 3), cy + Math.sin(ao + 0.035) * (rr + 3));
      ctx.lineTo(cx + Math.cos(ao - 0.035) * (rr + 3), cy + Math.sin(ao - 0.035) * (rr + 3));
      ctx.closePath(); ctx.fill();
      ctx.globalAlpha = 1;
    }

    // ---- dense tick ring + bearings
    var p1 = layer(0.08);
    if (p1 > 0) {
      var rt = R * (0.80 + 0.165 * p1);
      var n = small ? 120 : 240;
      var minor = new Path2D(), major = new Path2D();
      for (var i = 0; i < n; i++) {
        var at = A[1] + i / n * TAU;
        var isMajor = i % (n / 12) === 0, isMid = i % (n / 48) === 0;
        var tl = isMajor ? R * 0.055 : isMid ? R * 0.03 : R * 0.014;
        var cs = Math.cos(at), sn = Math.sin(at);
        var path = isMajor || isMid ? major : minor;
        path.moveTo(cx + cs * rt, cy + sn * rt);
        path.lineTo(cx + cs * (rt - tl), cy + sn * (rt - tl));
      }
      ctx.globalAlpha = p1 * flicker;
      ctx.lineWidth = hair;
      ctx.strokeStyle = col(0.30);
      ctx.stroke(minor);
      ctx.strokeStyle = col(0.72);
      ctx.stroke(major);

      if (this.labels && !small) {
        var fs = clamp(R * 0.036, 8, 11);
        ctx.font = '500 ' + fs.toFixed(1) + 'px "JetBrains Mono", ui-monospace, monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = col(0.55);
        var rl = rt - R * 0.09;
        for (var L = 0; L < 12; L++) {
          var al = A[1] + L / 12 * TAU;
          ctx.save();
          ctx.translate(cx + Math.cos(al) * rl, cy + Math.sin(al) * rl);
          ctx.rotate(al + Math.PI / 2);
          ctx.fillText(String(L * 30).padStart(3, '0'), 0, 0);
          ctx.restore();
        }
      }
      ctx.globalAlpha = 1;
    }

    // ---- segmented arc ring (counter-rotating), with glow pass
    var p2 = layer(0.18);
    if (p2 > 0) {
      var rs = R * (0.70 + 0.10 * p2);
      var lw = Math.max(2.2, R * 0.016);
      ctx.globalAlpha = p2 * flicker;
      var passes = [[lw * 4, 0.06 * this.glow], [lw, 0.82]];
      for (var pp = 0; pp < passes.length; pp++) {
        ctx.lineWidth = passes[pp][0];
        ctx.strokeStyle = col(passes[pp][1]);
        ctx.beginPath();
        for (var sg = 0; sg < SEGMENTS.length; sg++) {
          var s0 = A[2] + deg(SEGMENTS[sg][0]);
          var s1 = s0 + deg(SEGMENTS[sg][1]) * p2;
          ctx.moveTo(cx + Math.cos(s0) * rs, cy + Math.sin(s0) * rs);
          ctx.arc(cx, cy, rs, s0, s1);
        }
        ctx.stroke();
      }
      // companion hairline arcs just inside
      ctx.lineWidth = hair;
      ctx.strokeStyle = col(0.42);
      var rc = rs - lw - 3;
      ctx.beginPath();
      ctx.arc(cx, cy, rc, A[2] + deg(118), A[2] + deg(214));
      ctx.moveTo(cx + Math.cos(A[2] + deg(248)) * rc, cy + Math.sin(A[2] + deg(248)) * rc);
      ctx.arc(cx, cy, rc, A[2] + deg(248), A[2] + deg(332));
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // ---- thin ring with rotating node dots
    var p3 = layer(0.24);
    if (p3 > 0) {
      var rn = R * 0.655;
      ctx.globalAlpha = p3 * flicker;
      ctx.lineWidth = hair;
      ctx.strokeStyle = col(0.14);
      ctx.beginPath(); ctx.arc(cx, cy, rn, 0, TAU); ctx.stroke();
      ctx.fillStyle = col(0.55);
      for (var nd = 0; nd < 24; nd++) {
        var an = A[3] + nd / 24 * TAU;
        var sz = nd % 6 === 0 ? 2.2 : 1.2;
        ctx.fillRect(cx + Math.cos(an) * rn - sz / 2, cy + Math.sin(an) * rn - sz / 2, sz, sz);
      }
      // orbiting square marker
      var aq = A[9];
      ctx.fillStyle = col(0.95);
      ctx.fillRect(cx + Math.cos(aq) * rn - 2.5, cy + Math.sin(aq) * rn - 2.5, 5, 5);
      ctx.globalAlpha = 1;
    }

    // ---- radial spectrum bars (additive) with peak-hold
    var p4 = layer(0.3);
    if (p4 > 0) {
      var rb = R * 0.43;
      var maxLen = R * 0.19;
      var bw = Math.max(1.3, (TAU * rb / BARS) * 0.46);
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      ctx.globalAlpha = p4 * flicker;
      ctx.lineWidth = bw;
      for (var bi = 0; bi < BARS; bi++) {
        var ang = -Math.PI / 2 + (bi + 0.5) / BARS * TAU;
        var lvl = this.levels[bi] * p4;
        var L0 = R * 0.012 + maxLen * clamp(lvl, 0, 1);
        var cs2 = Math.cos(ang), sn2 = Math.sin(ang);
        ctx.strokeStyle = col(0.30 + 0.62 * clamp(lvl * 1.7, 0, 1));
        ctx.beginPath();
        ctx.moveTo(cx + cs2 * rb, cy + sn2 * rb);
        ctx.lineTo(cx + cs2 * (rb + L0), cy + sn2 * (rb + L0));
        ctx.stroke();
        var pk = rb + R * 0.012 + maxLen * clamp(this.peaks[bi] * p4, 0, 1) + 3;
        ctx.strokeStyle = col(0.38);
        ctx.beginPath();
        ctx.moveTo(cx + cs2 * pk, cy + sn2 * pk);
        ctx.lineTo(cx + cs2 * (pk + 1.6), cy + sn2 * (pk + 1.6));
        ctx.stroke();
      }
      ctx.restore();
    }

    // ---- dashed ring
    var p5 = layer(0.36);
    if (p5 > 0) {
      var rd = R * 0.405;
      var dash = TAU * rd / 120;
      ctx.globalAlpha = p5 * flicker;
      ctx.lineWidth = Math.max(1.5, R * 0.012);
      ctx.strokeStyle = col(0.34);
      ctx.setLineDash([dash * 0.45, dash * 0.55]);
      ctx.lineDashOffset = -A[4] * rd;
      ctx.beginPath(); ctx.arc(cx, cy, rd, 0, TAU * p5); ctx.stroke();
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
    }

    // ---- inner segmented ring with markers
    var p6 = layer(0.42);
    if (p6 > 0) {
      var ri = R * 0.35;
      ctx.globalAlpha = p6 * flicker;
      ctx.lineWidth = Math.max(1.2, hair * 1.6);
      ctx.strokeStyle = col(0.62);
      ctx.beginPath();
      for (var is = 0; is < INNER_SEGMENTS.length; is++) {
        var i0 = A[5] + deg(INNER_SEGMENTS[is][0]);
        var i1 = i0 + deg(INNER_SEGMENTS[is][1]) * p6;
        ctx.moveTo(cx + Math.cos(i0) * ri, cy + Math.sin(i0) * ri);
        ctx.arc(cx, cy, ri, i0, i1);
      }
      ctx.stroke();
      ctx.fillStyle = col(0.85);
      for (var im = 0; im < INNER_SEGMENTS.length; im++) {
        var am = A[5] + deg(INNER_SEGMENTS[im][0]);
        var x0 = cx + Math.cos(am) * (ri - 2), y0 = cy + Math.sin(am) * (ri - 2);
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(cx + Math.cos(am - 0.08) * (ri - 8), cy + Math.sin(am - 0.08) * (ri - 8));
        ctx.lineTo(cx + Math.cos(am + 0.02) * (ri - 8), cy + Math.sin(am + 0.02) * (ri - 8));
        ctx.closePath(); ctx.fill();
      }
      // orbiting dot with trail
      var ad = A[8];
      var rdot = R * 0.3775;
      for (var tr = 0; tr < 14; tr++) {
        var at2 = ad + tr * 0.03;
        ctx.fillStyle = col(0.6 * (1 - tr / 14));
        ctx.beginPath(); ctx.arc(cx + Math.cos(at2) * rdot, cy + Math.sin(at2) * rdot, 1.6 - tr * 0.07, 0, TAU); ctx.fill();
      }
      ctx.globalAlpha = 1;
    }

    // ---- sweep ring
    var p7 = layer(0.48);
    if (p7 > 0) {
      var rw = R * 0.305;
      ctx.globalAlpha = p7 * flicker;
      ctx.lineWidth = hair;
      ctx.strokeStyle = col(0.16);
      ctx.beginPath(); ctx.arc(cx, cy, rw, 0, TAU); ctx.stroke();
      if (!this.reduced) {
        var steps = 18, span = deg(70);
        ctx.lineWidth = Math.max(1.6, R * 0.009);
        for (var sw = 0; sw < steps; sw++) {
          var a1 = A[6] - sw / steps * span, a0 = a1 - span / steps;
          ctx.strokeStyle = col(0.75 * Math.pow(1 - sw / steps, 1.6));
          ctx.beginPath(); ctx.arc(cx, cy, rw, a0, a1 + 0.004); ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
    }

    // ---- ripples (speech pulses)
    if (this.ripples.length) {
      ctx.lineWidth = hair;
      for (var rp = 0; rp < this.ripples.length; rp++) {
        var rpt = this.ripples[rp].t;
        ctx.strokeStyle = col(0.42 * (1 - rpt) * this.ripples[rp].s);
        ctx.beginPath(); ctx.arc(cx, cy, R * (0.27 + 0.5 * easeOut(rpt)), 0, TAU); ctx.stroke();
      }
    }

    // ---- nucleus
    var p8 = layer(0.52);
    if (p8 > 0) {
      var breathe = this.reduced ? 0 : 0.018 * Math.sin(t * 1.9);
      var rnu = R * 0.255 * (0.6 + 0.4 * p8) * (1 + breathe + 0.05 * this.pulseV);
      ctx.save();
      ctx.globalCompositeOperation = 'lighter';
      var ng = ctx.createRadialGradient(cx, cy, 0, cx, cy, rnu * 1.35);
      var gl = this.glow * p8 * flicker;
      ng.addColorStop(0, col(0.16 * gl + 0.10 * this.pulseV));
      ng.addColorStop(0.55, col(0.07 * gl));
      ng.addColorStop(1, col(0));
      ctx.fillStyle = ng;
      ctx.beginPath(); ctx.arc(cx, cy, rnu * 1.35, 0, TAU); ctx.fill();
      ctx.restore();
      ctx.globalAlpha = p8 * flicker;
      ctx.lineWidth = hair;
      ctx.strokeStyle = col(0.55);
      ctx.beginPath(); ctx.arc(cx, cy, rnu, 0, TAU); ctx.stroke();
      ctx.strokeStyle = col(0.2);
      ctx.setLineDash([2, 4]);
      ctx.beginPath(); ctx.arc(cx, cy, rnu * 0.86, 0, TAU); ctx.stroke();
      ctx.setLineDash([]);
      // cardinal notches
      ctx.strokeStyle = col(0.8);
      ctx.beginPath();
      for (var cn = 0; cn < 4; cn++) {
        var ca = A[10] + cn * Math.PI / 2;
        ctx.moveTo(cx + Math.cos(ca) * rnu, cy + Math.sin(ca) * rnu);
        ctx.lineTo(cx + Math.cos(ca) * (rnu - 6), cy + Math.sin(ca) * (rnu - 6));
      }
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
  };

  global.AtlasHUD = { Core: Core, STATES: STATES, BARS: BARS };
})(window);
