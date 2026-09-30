/* Coin Audit chart: candlesticks, volume, EMAs and the audit's opportunity (entry zone, stop, targets,
   forming patterns, signals, pivots) drawn on the price. Vanilla canvas, no dependencies; it works from a
   file:// report and under the web app's strict Content-Security-Policy (no inline handlers, no eval).

   Usage: <div data-coin-chart="id-of-json-script"></div> + <script type="application/json" id="..."> — the
   chart mounts itself. window.CoinChart.mount(element, data) does the same by hand. */
(function () {
  "use strict";

  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var FUTURE_BARS = 14;          // empty space right of the last candle where the trade plan is drawn
  var MIN_BARS = 15;

  function pad2(n) { return (n < 10 ? "0" : "") + n; }
  function cssVar(el, name, fallback) {
    var v = getComputedStyle(el).getPropertyValue(name);
    return (v && v.trim()) || fallback;
  }
  function decimalsFor(p) {
    if (!(p > 0)) return 2;
    if (p >= 1000) return 2;
    if (p >= 1) return 4;
    if (p >= 0.01) return 6;
    return 8;
  }
  function fmtPrice(x, dec) {
    if (x === null || x === undefined || isNaN(x)) return "—";
    var s = Number(x).toFixed(dec);
    if (dec > 4) s = s.replace(/0+$/, "").replace(/\.$/, "");
    if (Math.abs(x) >= 1000) {
      var parts = s.split(".");
      parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      s = parts.join(".");
    }
    return s;
  }
  function fmtVol(v) {
    if (v >= 1e9) return (v / 1e9).toFixed(2) + "B";
    if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return v.toFixed(v >= 10 ? 0 : 2);
  }
  function fmtPct(x) { return (x >= 0 ? "+" : "") + x.toFixed(2) + "%"; }
  function fmtDate(ms, withTime) {
    var d = new Date(ms);
    var s = pad2(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()] + " " + d.getUTCFullYear();
    if (withTime) s += " " + pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes());
    return s + " UTC";
  }
  function niceStep(raw) {
    var p = Math.pow(10, Math.floor(Math.log(raw) / Math.LN10));
    var m = raw / p;
    var f = m >= 5 ? 5 : m >= 2 ? 2 : 1;
    if (m > 5) f = 10;
    return f * p;
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function Chart(root, d) {
    this.root = root;
    this.d = d;
    this.c = d.candles || [];
    this.n = this.c.length;
    this.tfMs = d.tf_ms || 14400000;
    var last = this.n ? this.c[this.n - 1][4] : 1;
    this.dec = typeof d.price_decimals === "number" ? d.price_decimals : decimalsFor(last);
    this.showEma = true; this.showVol = true; this.showPivots = true; this.showPlan = true;
    this.hover = null;
    this.drag = null;
    this.touch = null;
    this.build();
    this.resetView();
    this.bind();
    this.resize();
  }

  Chart.prototype.build = function () {
    var d = this.d, root = this.root, self = this;
    root.classList.add("cc-root");
    root.textContent = "";
    var top = el("div", "cc-top");
    var title = el("div", "cc-title");
    title.appendChild(el("b", null, d.symbol || ""));
    title.appendChild(el("span", "cc-tf", " " + (d.timeframe || "") + (d.exchange ? " · " + d.exchange : "")));
    top.appendChild(title);
    this.legend = el("div", "cc-legend");
    top.appendChild(this.legend);
    var tools = el("div", "cc-tools");
    var buttons = [["100", function () { self.zoomTo(100); }], ["250", function () { self.zoomTo(250); }],
                   ["All", function () { self.zoomTo(self.n); }],
                   ["EMA", function (b) { self.showEma = !self.showEma; b.classList.toggle("off", !self.showEma); self.draw(); }],
                   ["Vol", function (b) { self.showVol = !self.showVol; b.classList.toggle("off", !self.showVol); self.resize(); }],
                   ["Pivots", function (b) { self.showPivots = !self.showPivots; b.classList.toggle("off", !self.showPivots); self.draw(); }],
                   ["Plan", function (b) { self.showPlan = !self.showPlan; b.classList.toggle("off", !self.showPlan); self.draw(); }]];
    buttons.forEach(function (bd) {
      var b = el("button", "cc-btn", bd[0]);
      b.type = "button";
      b.addEventListener("click", function () { bd[1](b); });
      tools.appendChild(b);
    });
    top.appendChild(tools);
    root.appendChild(top);

    var body = el("div", "cc-body");
    this.wrap = el("div", "cc-canvas-wrap");
    this.wrap.tabIndex = 0;
    this.wrap.setAttribute("role", "img");
    this.wrap.setAttribute("aria-label", (d.symbol || "") + " " + (d.timeframe || "") + " candlestick chart with the trade plan");
    this.canvas = document.createElement("canvas");
    this.wrap.appendChild(this.canvas);
    this.tip = el("div", "cc-tip");
    this.wrap.appendChild(this.tip);
    body.appendChild(this.wrap);
    this.panel = el("aside", "cc-panel");
    this.buildPanel();
    body.appendChild(this.panel);
    root.appendChild(body);
    var help = el("div", "cc-help", "Scroll to zoom · drag to pan · double-click resets · hover a candle for its values. "
                  + "Dashed lines and the boxes right of the last candle are the audit's plan; they are reference levels, not orders.");
    root.appendChild(help);
    this.ctx = this.canvas.getContext("2d");
  };

  Chart.prototype.buildPanel = function () {
    var d = this.d, p = d.plan || {}, dec = this.dec, panel = this.panel;
    panel.textContent = "";
    var vcls = { FAVORABLE: "good", WATCHLIST: "warning", NEUTRAL: "muted", AVOID: "critical" }[d.verdict] || "muted";
    var icon = { FAVORABLE: "✔", WATCHLIST: "◐", NEUTRAL: "○", AVOID: "✖" }[d.verdict] || "○";
    var head = el("div", "cc-verdict " + vcls);
    head.appendChild(el("span", "cc-vicon", icon));
    head.appendChild(el("span", "cc-vname", d.verdict || "—"));
    head.appendChild(el("span", "cc-vscore", (d.score !== undefined ? d.score : "—") + "/100"));
    panel.appendChild(head);
    var meter = el("div", "cc-meter");
    var fill = el("i");
    fill.style.width = Math.max(0, Math.min(100, d.score || 0)) + "%";
    meter.appendChild(fill);
    panel.appendChild(meter);
    if (d.verdict_text) panel.appendChild(el("p", "cc-vtext", d.verdict_text));
    if (d.flags && d.flags.length) {
      d.flags.forEach(function (f) { panel.appendChild(el("p", "cc-flag", "⚠ " + f)); });
    }

    panel.appendChild(el("h4", null, "Trade plan (spot long, reference only)"));
    var t = el("table", "cc-plan");
    var price = p.price || 0;
    function row(k, v, cls) {
      var tr = el("tr");
      tr.appendChild(el("td", null, k));
      tr.appendChild(el("td", "cc-num " + (cls || ""), v));
      t.appendChild(tr);
    }
    function rel(x) { return price ? " (" + fmtPct((x / price - 1) * 100) + ")" : ""; }
    row("Last close", fmtPrice(price, dec));
    row("Entry zone", fmtPrice(p.entry_low, dec) + " – " + fmtPrice(p.entry_high, dec), "accent");
    row("Stop", fmtPrice(p.stop, dec) + rel(p.stop), "down");
    row("Target 1", fmtPrice(p.target1, dec) + rel(p.target1) + (p.rr1 ? " · " + p.rr1.toFixed(1) + "R" : ""), "up");
    row("Target 2", fmtPrice(p.target2, dec) + rel(p.target2) + (p.rr2 ? " · " + p.rr2.toFixed(1) + "R" : ""), "up");
    if (p.stop_distance_pct !== undefined) row("Risk per unit", p.stop_distance_pct + "% to the stop");
    if (p.position_size_pct_of_account !== undefined) row("Size for 1% account risk", p.position_size_pct_of_account + "% of the account");
    if (p.horizon) row("Horizon", p.horizon);
    if (p.resistance_20_bars) row("Resistance (20 bars / 90 d)", fmtPrice(p.resistance_20_bars, dec) + " / " + fmtPrice(p.resistance_90d, dec));
    panel.appendChild(t);

    var sig = d.signals || [];
    panel.appendChild(el("h4", null, "Strategy signals on the last closed candle"));
    if (!sig.length) {
      panel.appendChild(el("p", "cc-muted", "No strategy fired on the last closed candle."));
    } else {
      var ul = el("ul", "cc-list");
      sig.forEach(function (s) {
        var li = el("li", s.approved ? "ok" : "blocked");
        li.appendChild(el("b", null, (s.approved ? "▲ " : "□ ") + s.strategy));
        var txt = s.approved ? " APPROVED" : " " + s.decision;
        txt += " · confidence " + Math.round((s.confidence || 0) * 100) + "%";
        if (s.approved && s.risk) txt += " · risk " + (s.risk * 100).toFixed(2) + "% of account";
        if (s.stop) txt += " · stop " + fmtPrice(s.stop, dec) + " · target " + fmtPrice(s.target, dec);
        li.appendChild(document.createTextNode(txt));
        ul.appendChild(li);
      });
      panel.appendChild(ul);
    }

    var pats = d.patterns || [];
    var bull = pats.filter(function (x) { return x.kind === "bullish"; });
    var bear = pats.filter(function (x) { return x.kind === "bearish"; });
    panel.appendChild(el("h4", null, "Chart patterns"));
    if (!bull.length && !bear.length) {
      panel.appendChild(el("p", "cc-muted", "Nothing forming on the last closed candle" + (d.structure ? " · structure " + d.structure : "") + "."));
    } else {
      var pl = el("ul", "cc-list");
      bull.forEach(function (x) {
        var li = el("li", "forming");
        li.appendChild(el("b", null, "◇ " + x.name.replace(/_/g, " ")));
        li.appendChild(document.createTextNode(" forming: needs a close above " + fmtPrice(x.trigger, dec)
          + " with volume · invalid below " + fmtPrice(x.invalidation, dec) + " · " + x.expires_in_bars + " bars left"));
        pl.appendChild(li);
      });
      bear.forEach(function (x) {
        var li = el("li", "bear");
        li.appendChild(el("b", null, "⚠ " + x.name.replace(/_/g, " ")));
        li.appendChild(document.createTextNode(" completed " + x.bars_ago + " bars ago at " + fmtPrice(x.level, dec) + " — new longs blocked"));
        pl.appendChild(li);
      });
      panel.appendChild(pl);
      if (d.structure) panel.appendChild(el("p", "cc-muted", "Market structure: " + d.structure));
    }
    var cs = (d.markers || []).filter(function (m) { return m.kind === "candle"; });
    if (cs.length) {
      panel.appendChild(el("p", "cc-muted", "Candlesticks: " + cs.map(function (m) {
        return m.text + " (" + (this.n - 1 - m.i) + " bars ago)";
      }, this).join(", ")));
    }
    if (d.regime) {
      panel.appendChild(el("h4", null, "Regime"));
      var r = d.regime;
      panel.appendChild(el("p", "cc-muted", "Trend: " + r.trend + " · volatility: " + r.vol + " · BTC: " + r.btc
        + (r.market ? " · market: " + r.market : "")));
    }
    panel.appendChild(el("p", "cc-stamp", "Audited " + (d.audit_time_utc || "") + " UTC · candles to " + (d.data_until_utc || "") + " UTC"));
  };

  // ------------------------------------------------------------------ view
  Chart.prototype.resetView = function () {
    var span = Math.min(this.n, 160);
    this.view = { from: this.n - span, to: this.n };
  };
  Chart.prototype.zoomTo = function (bars) {
    bars = Math.max(MIN_BARS, Math.min(this.n, bars));
    this.view = { from: this.n - bars, to: this.n };
    this.draw();
  };
  Chart.prototype.clampView = function () {
    var v = this.view, span = v.to - v.from;
    span = Math.max(MIN_BARS, Math.min(this.n + FUTURE_BARS, span));
    if (v.from < 0) v.from = 0;
    if (v.from > this.n - MIN_BARS) v.from = Math.max(0, this.n - MIN_BARS);
    v.to = v.from + span;
    if (v.to > this.n + 1) { v.to = this.n + 1; v.from = Math.max(0, v.to - span); }
  };

  // ------------------------------------------------------------------ events
  Chart.prototype.bind = function () {
    var self = this, cv = this.canvas;
    cv.addEventListener("mousemove", function (e) {
      var r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
      if (self.drag) {
        var dx = x - self.drag.x;
        var bars = dx / self.barW;
        self.view.from = self.drag.from - bars; self.view.to = self.drag.to - bars;
        self.clampView();
        self.hover = null;
        self.draw();
        return;
      }
      self.hover = { x: x, y: y };
      self.draw();
    });
    cv.addEventListener("mouseleave", function () { self.hover = null; self.drag = null; self.draw(); });
    cv.addEventListener("mousedown", function (e) {
      var r = cv.getBoundingClientRect();
      self.drag = { x: e.clientX - r.left, from: self.view.from, to: self.view.to };
      e.preventDefault();
    });
    window.addEventListener("mouseup", function () { self.drag = null; });
    cv.addEventListener("dblclick", function () { self.resetView(); self.draw(); });
    cv.addEventListener("wheel", function (e) {
      var r = cv.getBoundingClientRect(), x = e.clientX - r.left;
      var factor = Math.exp((e.deltaMode === 1 ? e.deltaY * 20 : e.deltaY) * 0.0015);
      self.zoomAt(x, factor);
      e.preventDefault();
    }, { passive: false });
    cv.addEventListener("touchstart", function (e) {
      if (e.touches.length === 1) {
        var r = cv.getBoundingClientRect();
        self.touch = { x: e.touches[0].clientX - r.left, from: self.view.from, to: self.view.to };
      } else if (e.touches.length === 2) {
        self.touch = { dist: Math.abs(e.touches[0].clientX - e.touches[1].clientX), from: self.view.from, to: self.view.to };
      }
    }, { passive: true });
    cv.addEventListener("touchmove", function (e) {
      if (!self.touch) return;
      var r = cv.getBoundingClientRect();
      if (e.touches.length === 1 && self.touch.x !== undefined) {
        var dx = e.touches[0].clientX - r.left - self.touch.x;
        var bars = dx / self.barW;
        self.view.from = self.touch.from - bars; self.view.to = self.touch.to - bars;
        self.clampView(); self.draw();
        e.preventDefault();
      } else if (e.touches.length === 2 && self.touch.dist) {
        var dist = Math.abs(e.touches[0].clientX - e.touches[1].clientX) || 1;
        var span = (self.touch.to - self.touch.from) * self.touch.dist / dist;
        var mid = (self.touch.from + self.touch.to) / 2;
        self.view.from = mid - span / 2; self.view.to = mid + span / 2;
        self.clampView(); self.draw();
        e.preventDefault();
      }
    }, { passive: false });
    cv.addEventListener("touchend", function () { self.touch = null; }, { passive: true });
    this.wrap.addEventListener("keydown", function (e) {
      var span = self.view.to - self.view.from, step = Math.max(1, Math.round(span / 10));
      if (e.key === "ArrowLeft") { self.view.from -= step; self.view.to -= step; }
      else if (e.key === "ArrowRight") { self.view.from += step; self.view.to += step; }
      else if (e.key === "+" || e.key === "=") { self.zoomAt(self.plot.l + self.plot.w / 2, 0.8); return; }
      else if (e.key === "-") { self.zoomAt(self.plot.l + self.plot.w / 2, 1.25); return; }
      else return;
      self.clampView(); self.draw(); e.preventDefault();
    });
    if (window.ResizeObserver) {
      new ResizeObserver(function () { self.resize(); }).observe(this.wrap);
    } else {
      window.addEventListener("resize", function () { self.resize(); });
    }
  };
  Chart.prototype.zoomAt = function (x, factor) {
    var v = this.view, span = v.to - v.from;
    var cursor = v.from + (x - this.plot.l) / this.barW;
    var nspan = Math.max(MIN_BARS, Math.min(this.n + FUTURE_BARS, span * factor));
    var f = nspan / span;
    v.from = cursor - (cursor - v.from) * f;
    v.to = v.from + nspan;
    this.clampView();
    this.draw();
  };

  // ------------------------------------------------------------------ layout
  Chart.prototype.resize = function () {
    var w = Math.max(320, this.wrap.clientWidth || 800);
    var h = Math.max(300, Math.min(640, Math.round(w * 0.56)));
    var dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.canvas.style.width = w + "px";
    this.canvas.style.height = h + "px";
    this.W = w; this.H = h; this.dpr = dpr;
    this.draw();
  };
  Chart.prototype.layout = function () {
    var axisW = 12 + 7.2 * (fmtPrice(this.n ? this.c[this.n - 1][2] : 1, this.dec).length + 1);
    var timeH = 24, top = 8, gap = 6;
    var volH = this.showVol ? Math.round(this.H * 0.16) : 0;
    var plotH = this.H - top - timeH - volH - (this.showVol ? gap : 0);
    this.plot = { l: 8, t: top, w: this.W - 8 - axisW, h: plotH, r: this.W - axisW };
    this.vol = { t: top + plotH + gap, h: volH };
    this.timeY = this.H - timeH;
    var span = this.view.to - this.view.from + FUTURE_BARS;
    this.barW = this.plot.w / span;
  };
  Chart.prototype.x = function (i) { return this.plot.l + (i + 0.5 - this.view.from) * this.barW; };
  Chart.prototype.y = function (p) { return this.plot.t + (this.pmax - p) / (this.pmax - this.pmin) * this.plot.h; };

  Chart.prototype.priceRange = function () {
    var i0 = Math.max(0, Math.floor(this.view.from)), i1 = Math.min(this.n, Math.ceil(this.view.to));
    var lo = Infinity, hi = -Infinity, c = this.c;
    for (var i = i0; i < i1; i++) { if (c[i][3] < lo) lo = c[i][3]; if (c[i][2] > hi) hi = c[i][2]; }
    if (!(lo < hi)) { lo = c.length ? c[this.n - 1][3] * 0.95 : 0; hi = c.length ? c[this.n - 1][2] * 1.05 : 1; }
    var range = hi - lo;
    var p = this.d.plan || {};
    var extras = [];
    if (this.showPlan && i1 >= this.n - 2) {           // the plan sits at the right edge: keep it in view
      extras = [p.entry_low, p.entry_high, p.stop, p.target1, p.target2];
      (this.d.patterns || []).forEach(function (x) { if (x.kind === "bullish") extras.push(x.trigger); });
    }
    extras.forEach(function (v) {
      if (typeof v === "number" && v > lo - range * 0.6 && v < hi + range * 0.6) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    });
    var pad = (hi - lo) * 0.06 || hi * 0.01;
    this.pmin = lo - pad; this.pmax = hi + pad;
  };

  // ------------------------------------------------------------------ drawing
  Chart.prototype.colors = function () {
    var r = this.root;
    return {
      bg: cssVar(r, "--surface", "#1a1a19"), grid: cssVar(r, "--grid", "#2c2c2a"), ink: cssVar(r, "--ink", "#fff"),
      ink2: cssVar(r, "--ink2", "#c3c2b7"), muted: cssVar(r, "--muted", "#898781"), up: cssVar(r, "--up", "#0ca30c"),
      down: cssVar(r, "--down", "#d03b3b"), accent: cssVar(r, "--accent", "#3987e5"), warn: cssVar(r, "--warning", "#fab219"),
      ema20: "#f2a93b", ema50: "#3987e5", ema200: "#b06cf0", pivot: cssVar(r, "--muted", "#898781"), plane: cssVar(r, "--plane", "#0d0d0d")
    };
  };
  Chart.prototype.draw = function () {
    if (!this.ctx || !this.W) return;
    this.layout();
    this.priceRange();
    var g = this.ctx, C = this.colors(), P = this.plot, dpr = this.dpr;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, this.W, this.H);
    g.fillStyle = C.bg; g.fillRect(0, 0, this.W, this.H);
    g.font = "11px system-ui, -apple-system, 'Segoe UI', sans-serif";
    this.drawGrid(g, C);
    if (!this.n) {
      g.fillStyle = C.ink2; g.textAlign = "center";
      g.fillText("No candles to draw.", this.W / 2, this.H / 2);
      return;
    }
    g.save(); g.beginPath(); g.rect(P.l, P.t, P.w, P.h + (this.showVol ? this.vol.h + 6 : 0)); g.clip();
    if (this.showPlan) this.drawZones(g, C);
    if (this.showVol) this.drawVolume(g, C);
    this.drawCandles(g, C);
    if (this.showEma) this.drawEmas(g, C);
    if (this.showPivots) this.drawPivots(g, C);
    if (this.showPlan) this.drawLevels(g, C);
    this.drawMarkers(g, C);
    g.restore();
    this.drawAxes(g, C);
    this.drawHover(g, C);
    this.updateLegend(C);
  };
  Chart.prototype.drawGrid = function (g, C) {
    var P = this.plot;
    g.strokeStyle = C.grid; g.lineWidth = 1;
    var step = niceStep((this.pmax - this.pmin) / 6);
    this.priceTicks = [];
    for (var v = Math.ceil(this.pmin / step) * step; v <= this.pmax; v += step) {
      var y = Math.round(this.y(v)) + 0.5;
      g.beginPath(); g.moveTo(P.l, y); g.lineTo(P.r, y); g.stroke();
      this.priceTicks.push([v, y]);
    }
    this.timeTicks = this.pickTimeTicks();
    for (var k = 0; k < this.timeTicks.length; k++) {
      var x = Math.round(this.x(this.timeTicks[k][0])) + 0.5;
      g.beginPath(); g.moveTo(x, P.t); g.lineTo(x, this.timeY); g.stroke();
    }
  };
  Chart.prototype.pickTimeTicks = function () {
    var i0 = Math.max(0, Math.floor(this.view.from)), i1 = Math.min(this.n, Math.ceil(this.view.to));
    var out = [], minGap = 72, lastX = -Infinity, c = this.c;
    var intraday = this.tfMs < 86400000;
    var cand = [];
    for (var i = i0; i < i1; i++) {
      var d = new Date(c[i][0]), prev = i > 0 ? new Date(c[i - 1][0]) : null;
      var rank = 0, label = "";
      if (!prev || d.getUTCMonth() !== prev.getUTCMonth() || d.getUTCFullYear() !== prev.getUTCFullYear()) {
        rank = 2; label = MONTHS[d.getUTCMonth()] + (d.getUTCMonth() === 0 || !prev ? " " + d.getUTCFullYear() : "");
      } else if (intraday && d.getUTCDate() !== prev.getUTCDate()) {
        rank = 1; label = pad2(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()];
      } else if (!intraday && (d.getUTCDate() === 1 || d.getUTCDate() % 7 === 1)) {
        rank = 1; label = pad2(d.getUTCDate()) + " " + MONTHS[d.getUTCMonth()];
      } else if (intraday && this.barW > 26) {
        rank = 0; label = pad2(d.getUTCHours()) + ":" + pad2(d.getUTCMinutes());
      } else if (!intraday && this.barW > 26) {
        rank = 0; label = pad2(d.getUTCDate());
      }
      if (label) cand.push([i, label, rank]);
    }
    cand.sort(function (a, b) { return b[2] - a[2] || a[0] - b[0]; });
    var chosen = [];
    for (var k = 0; k < cand.length; k++) {
      var x = this.x(cand[k][0]), ok = true;
      for (var j = 0; j < chosen.length; j++) { if (Math.abs(chosen[j][3] - x) < minGap) { ok = false; break; } }
      if (ok) chosen.push([cand[k][0], cand[k][1], cand[k][2], x]);
    }
    chosen.sort(function (a, b) { return a[0] - b[0]; });
    lastX = lastX + 0;
    for (var m = 0; m < chosen.length; m++) out.push([chosen[m][0], chosen[m][1]]);
    return out;
  };
  Chart.prototype.drawZones = function (g, C) {
    var p = this.d.plan || {}, P = this.plot;
    if (typeof p.entry_low !== "number") return;
    var xLast = this.x(this.n - 1) + this.barW / 2, xEnd = Math.min(P.r, this.x(this.n - 1 + FUTURE_BARS - 1));
    if (xEnd <= xLast) return;
    var mid = (p.entry_low + p.entry_high) / 2;
    // position tool: reward boxes above the entry, risk box below (like a long-position drawing)
    g.fillStyle = C.up; g.globalAlpha = 0.10;
    g.fillRect(xLast, this.y(p.target2), xEnd - xLast, this.y(mid) - this.y(p.target2));
    g.globalAlpha = 0.14;
    g.fillRect(xLast, this.y(p.target1), xEnd - xLast, this.y(mid) - this.y(p.target1));
    g.fillStyle = C.down; g.globalAlpha = 0.16;
    g.fillRect(xLast, this.y(mid), xEnd - xLast, this.y(p.stop) - this.y(mid));
    g.fillStyle = C.accent; g.globalAlpha = 0.22;
    g.fillRect(P.l, this.y(p.entry_high), P.w, this.y(p.entry_low) - this.y(p.entry_high));
    g.globalAlpha = 1;
  };
  Chart.prototype.drawVolume = function (g, C) {
    var i0 = Math.max(0, Math.floor(this.view.from)), i1 = Math.min(this.n, Math.ceil(this.view.to));
    var vmax = 0, c = this.c;
    for (var i = i0; i < i1; i++) if (c[i][5] > vmax) vmax = c[i][5];
    if (!vmax) return;
    var V = this.vol, w = Math.max(1, Math.floor(this.barW * 0.7));
    g.globalAlpha = 0.55;
    for (var k = i0; k < i1; k++) {
      var h = c[k][5] / vmax * V.h;
      g.fillStyle = c[k][4] >= c[k][1] ? C.up : C.down;
      g.fillRect(Math.round(this.x(k) - w / 2), V.t + V.h - h, w, h);
    }
    g.globalAlpha = 1;
    this.vmax = vmax;
  };
  Chart.prototype.drawCandles = function (g, C) {
    var i0 = Math.max(0, Math.floor(this.view.from)), i1 = Math.min(this.n, Math.ceil(this.view.to));
    var w = Math.max(1, Math.floor(this.barW * 0.7)), c = this.c;
    for (var i = i0; i < i1; i++) {
      var o = c[i][1], h = c[i][2], l = c[i][3], cl = c[i][4];
      var up = cl >= o, x = Math.round(this.x(i));
      g.strokeStyle = g.fillStyle = up ? C.up : C.down;
      g.lineWidth = 1;
      g.beginPath(); g.moveTo(x + 0.5, this.y(h)); g.lineTo(x + 0.5, this.y(l)); g.stroke();
      var yo = this.y(o), yc = this.y(cl), top = Math.min(yo, yc), bh = Math.max(1, Math.abs(yc - yo));
      if (w <= 2) { g.fillRect(x, top, 1, bh); }
      else if (up) { g.fillStyle = C.bg; g.fillRect(x - Math.floor(w / 2), top, w, bh); g.strokeRect(x - Math.floor(w / 2) + 0.5, top + 0.5, w - 1, Math.max(0, bh - 1)); }
      else { g.fillRect(x - Math.floor(w / 2), top, w, bh); }
    }
  };
  Chart.prototype.drawEmas = function (g, C) {
    var e = this.d.ema || {}, keys = [["ema20", C.ema20], ["ema50", C.ema50], ["ema200", C.ema200]];
    var i0 = Math.max(0, Math.floor(this.view.from)), i1 = Math.min(this.n, Math.ceil(this.view.to));
    for (var k = 0; k < keys.length; k++) {
      var s = e[keys[k][0]];
      if (!s) continue;
      g.strokeStyle = keys[k][1]; g.lineWidth = 1.3; g.beginPath();
      var pen = false;
      for (var i = i0; i < i1; i++) {
        var v = s[i];
        if (v === null || v === undefined) { pen = false; continue; }
        var x = this.x(i), y = this.y(v);
        if (!pen) { g.moveTo(x, y); pen = true; } else g.lineTo(x, y);
      }
      g.stroke();
    }
  };
  Chart.prototype.drawPivots = function (g, C) {
    var pv = this.d.pivots || {}, i0 = Math.floor(this.view.from), i1 = Math.ceil(this.view.to);
    g.fillStyle = C.pivot;
    var s = Math.max(2, Math.min(4, this.barW * 0.35));
    (pv.highs || []).forEach(function (h) {
      if (h[0] < i0 || h[0] >= i1) return;
      var x = this.x(h[0]), y = this.y(h[1]) - 5;
      g.beginPath(); g.moveTo(x, y); g.lineTo(x - s, y - s * 1.6); g.lineTo(x + s, y - s * 1.6); g.closePath(); g.fill();
    }, this);
    (pv.lows || []).forEach(function (l) {
      if (l[0] < i0 || l[0] >= i1) return;
      var x = this.x(l[0]), y = this.y(l[1]) + 5;
      g.beginPath(); g.moveTo(x, y); g.lineTo(x - s, y + s * 1.6); g.lineTo(x + s, y + s * 1.6); g.closePath(); g.fill();
    }, this);
  };
  Chart.prototype.levelList = function () {
    var p = this.d.plan || {}, out = [], C = this.colors();
    if (typeof p.stop === "number") {
      var mid = (p.entry_low + p.entry_high) / 2;
      out.push({ price: p.target2, color: C.up, dash: [6, 4], label: "T2 " + (p.rr2 ? p.rr2.toFixed(1) + "R " : "") + fmtPct((p.target2 / p.price - 1) * 100) });
      out.push({ price: p.target1, color: C.up, dash: [6, 4], label: "T1 " + (p.rr1 ? p.rr1.toFixed(1) + "R " : "") + fmtPct((p.target1 / p.price - 1) * 100) });
      out.push({ price: mid, color: C.accent, dash: [2, 3], label: "Entry zone " + fmtPrice(p.entry_low, this.dec) + "–" + fmtPrice(p.entry_high, this.dec) });
      out.push({ price: p.stop, color: C.down, dash: [6, 4], label: "Stop " + fmtPct((p.stop / p.price - 1) * 100) });
    }
    (this.d.patterns || []).forEach(function (x) {
      if (x.kind === "bullish") {
        out.push({ price: x.trigger, color: C.warn, dash: [3, 3], label: x.name.replace(/_/g, " ") + " trigger", start: x.start });
        out.push({ price: x.invalidation, color: C.warn, dash: [1, 3], label: x.name.replace(/_/g, " ") + " invalid", faint: true, start: x.start });
      } else if (x.kind === "bearish") {
        out.push({ price: x.level, color: C.down, dash: [3, 3], label: "bearish " + x.name.replace(/_/g, " ") });
      }
    });
    (this.d.levels || []).forEach(function (l) { out.push({ price: l.price, color: C.muted, dash: [1, 4], label: l.label, faint: true }); });
    return out;
  };
  Chart.prototype.drawLevels = function (g, C) {
    var P = this.plot, levels = this.levelList(), self = this;
    g.textBaseline = "middle";
    levels.forEach(function (L) {
      if (typeof L.price !== "number" || L.price < self.pmin || L.price > self.pmax) return;
      var y = Math.round(self.y(L.price)) + 0.5;
      var x0 = L.start !== undefined ? Math.max(P.l, self.x(L.start)) : P.l;
      g.strokeStyle = L.color; g.globalAlpha = L.faint ? 0.6 : 0.95; g.lineWidth = 1;
      g.setLineDash(L.dash); g.beginPath(); g.moveTo(x0, y); g.lineTo(P.r, y); g.stroke(); g.setLineDash([]);
      g.globalAlpha = 1;
      var tw = g.measureText(L.label).width + 8;
      g.fillStyle = C.bg; g.globalAlpha = 0.85; g.fillRect(x0 + 4, y - 8, tw, 15); g.globalAlpha = 1;
      g.fillStyle = L.color; g.textAlign = "left"; g.fillText(L.label, x0 + 8, y);
    });
  };
  Chart.prototype.drawMarkers = function (g, C) {
    var ms = this.d.markers || [], i0 = Math.floor(this.view.from), i1 = Math.ceil(this.view.to);
    g.textAlign = "center"; g.textBaseline = "middle";
    ms.forEach(function (m) {
      if (m.i < i0 || m.i >= i1) return;
      var c = this.c[m.i], x = this.x(m.i);
      if (m.kind === "signal") {
        var y = this.y(c[3]) + 14;
        g.fillStyle = m.approved ? C.up : C.warn;
        g.beginPath(); g.moveTo(x, y - 6); g.lineTo(x - 5, y + 2); g.lineTo(x + 5, y + 2); g.closePath(); g.fill();
        g.fillText(m.text, x, y + 12);
      } else {
        var yy = this.y(c[2]) - 12;
        g.fillStyle = C.ink2;
        g.fillText(m.text.replace(/_/g, " "), x, yy);
      }
    }, this);
  };
  Chart.prototype.drawAxes = function (g, C) {
    var P = this.plot;
    g.fillStyle = C.bg; g.fillRect(P.r, 0, this.W - P.r, this.H); g.fillRect(0, this.timeY, this.W, this.H - this.timeY);
    g.strokeStyle = C.grid; g.beginPath(); g.moveTo(P.r + 0.5, P.t); g.lineTo(P.r + 0.5, this.timeY); g.stroke();
    g.beginPath(); g.moveTo(P.l, this.timeY + 0.5); g.lineTo(P.r, this.timeY + 0.5); g.stroke();
    g.fillStyle = C.ink2; g.textAlign = "left"; g.textBaseline = "middle";
    for (var i = 0; i < this.priceTicks.length; i++) g.fillText(fmtPrice(this.priceTicks[i][0], this.dec), P.r + 5, this.priceTicks[i][1]);
    g.textAlign = "center"; g.textBaseline = "top";
    for (var k = 0; k < this.timeTicks.length; k++) g.fillText(this.timeTicks[k][1], this.x(this.timeTicks[k][0]), this.timeY + 6);
    if (this.n) {                                             // last close tag
      var last = this.c[this.n - 1], y = this.y(last[4]);
      if (y >= P.t && y <= P.t + P.h) {
        g.fillStyle = last[4] >= last[1] ? C.up : C.down;
        g.fillRect(P.r + 1, y - 8, this.W - P.r - 1, 16);
        g.fillStyle = "#fff"; g.textAlign = "left"; g.textBaseline = "middle";
        g.fillText(fmtPrice(last[4], this.dec), P.r + 5, y);
      }
    }
    if (this.showVol && this.vmax) {
      g.fillStyle = C.muted; g.textAlign = "left"; g.textBaseline = "top";
      g.fillText("Vol " + fmtVol(this.vmax), P.l + 4, this.vol.t);
    }
  };
  Chart.prototype.hoverIndex = function () {
    if (!this.hover) return -1;
    var i = Math.floor(this.view.from + (this.hover.x - this.plot.l) / this.barW);
    return i >= 0 && i < this.n ? i : -1;
  };
  Chart.prototype.drawHover = function (g, C) {
    var i = this.hoverIndex(), P = this.plot;
    this.tip.style.display = "none";
    if (i < 0) return;
    var x = Math.round(this.x(i)) + 0.5, y = Math.round(this.hover.y) + 0.5;
    g.strokeStyle = C.ink2; g.setLineDash([3, 3]); g.lineWidth = 1;
    g.beginPath(); g.moveTo(x, P.t); g.lineTo(x, this.timeY); g.stroke();
    if (y >= P.t && y <= P.t + P.h) {
      g.beginPath(); g.moveTo(P.l, y); g.lineTo(P.r, y); g.stroke();
      var price = this.pmax - (y - P.t) / P.h * (this.pmax - this.pmin);
      g.setLineDash([]);
      g.fillStyle = C.ink; g.fillRect(P.r + 1, y - 8, this.W - P.r - 1, 16);
      g.fillStyle = C.bg; g.textAlign = "left"; g.textBaseline = "middle";
      g.fillText(fmtPrice(price, this.dec), P.r + 5, y);
    }
    g.setLineDash([]);
    var label = fmtDate(this.c[i][0], this.tfMs < 86400000), tw = g.measureText(label).width + 10;
    var lx = Math.max(P.l, Math.min(P.r - tw, x - tw / 2));
    g.fillStyle = C.ink; g.fillRect(lx, this.timeY + 2, tw, 18);
    g.fillStyle = C.bg; g.textAlign = "center"; g.textBaseline = "middle"; g.fillText(label, lx + tw / 2, this.timeY + 11);
  };
  Chart.prototype.updateLegend = function (C) {
    var i = this.hoverIndex();
    if (i < 0) i = this.n - 1;
    var L = this.legend; L.textContent = "";
    if (i < 0) return;
    var c = this.c[i], dec = this.dec, chg = c[1] ? (c[4] / c[1] - 1) * 100 : 0;
    function item(k, v, color) {
      var s = el("span", "cc-li"); s.appendChild(el("span", "cc-k", k));
      var val = el("span", "cc-v", v); if (color) val.style.color = color; s.appendChild(val); L.appendChild(s);
    }
    item("O", fmtPrice(c[1], dec)); item("H", fmtPrice(c[2], dec)); item("L", fmtPrice(c[3], dec));
    item("C", fmtPrice(c[4], dec), c[4] >= c[1] ? C.up : C.down); item("", fmtPct(chg), chg >= 0 ? C.up : C.down);
    item("Vol", fmtVol(c[5]));
    var e = this.d.ema || {};
    if (this.showEma) {
      if (e.ema20 && e.ema20[i] != null) item("EMA20", fmtPrice(e.ema20[i], dec), C.ema20);
      if (e.ema50 && e.ema50[i] != null) item("EMA50", fmtPrice(e.ema50[i], dec), C.ema50);
      if (e.ema200 && e.ema200[i] != null) item("EMA200", fmtPrice(e.ema200[i], dec), C.ema200);
    }
    item("", fmtDate(c[0], this.tfMs < 86400000));
  };

  function mount(root, data) { return new Chart(root, data); }
  function auto() {
    var nodes = document.querySelectorAll("[data-coin-chart]");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.getAttribute("data-mounted")) continue;
      var src = document.getElementById(node.getAttribute("data-coin-chart"));
      if (!src) continue;
      try {
        mount(node, JSON.parse(src.textContent));
        node.setAttribute("data-mounted", "1");
      } catch (e) {
        node.textContent = "Chart could not be drawn: " + e.message;
      }
    }
  }
  window.CoinChart = { mount: mount, auto: auto, version: 1 };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto); else auto();
})();
