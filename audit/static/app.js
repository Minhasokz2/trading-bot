/* Coin Audit web app: theme switch, live job page, report filter. No frameworks, no inline handlers (strict CSP). */
(function () {
  "use strict";

  // ---- theme: follows the OS until the user picks one (stored in this browser only)
  var themeBtn = document.getElementById("theme");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme");
      var dark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
      var next = dark ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("coin-audit-theme", next); } catch (e) { /* private mode */ }
      if (window.CoinChart && window.CoinChart.redraw) window.CoinChart.redraw();
    });
  }

  // ---- reports page: filter rows as you type
  var filter = document.getElementById("filter");
  if (filter) {
    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      var rows = document.querySelectorAll("#reports tbody tr[data-key]");
      for (var i = 0; i < rows.length; i++) {
        rows[i].style.display = !q || rows[i].getAttribute("data-key").indexOf(q) >= 0 ? "" : "none";
      }
    });
  }

  // ---- job page: poll the job, show the stage, the elapsed time and the report links
  var el = document.getElementById("job");
  if (!el) return;
  var id = el.dataset.id, log = document.getElementById("log"), st = document.getElementById("status"),
      links = document.getElementById("links"), cancel = document.getElementById("cancel"), err = document.getElementById("err"),
      stage = document.getElementById("stage"), elapsed = document.getElementById("elapsed");
  var terminal = ["done", "failed", "cancelled", "timeout"];
  var label = { queued: "⏳ queued", running: "▶ running", done: "✔ done", failed: "✖ failed",
                cancelled: "⊘ cancelled", timeout: "⏱ timed out" };
  var STAGES = [["fetching ", "1/6 fetching candles"], ["market checks + regime", "2/6 market checks + regime"],
                ["scanning chart patterns", "3/6 chart patterns + candlesticks"], ["building crypto-wide", "3/6 crypto-wide + macro regime"],
                ["validating ", "4/6 validating strategies (walk-forward, 12 gates)"], ["training meta-labeler", "5/6 training the meta-labeler"],
                ["Report:", "6/6 report written"], ["Scanning ", "scanning the market"], ["Dashboard:", "dashboard rebuilt"]];
  function stageOf(text) {
    var best = "", pos = -1;
    for (var i = 0; i < STAGES.length; i++) {
      var p = text.lastIndexOf(STAGES[i][0]);
      if (p > pos) { pos = p; best = STAGES[i][1]; }
    }
    var m = text.match(/Auditing ([A-Z0-9]+) on (\w+)/g);
    if (m && best) best = m[m.length - 1].replace("Auditing ", "") + " · " + best;
    return best;
  }
  function parseUtc(s) { return s ? Date.parse(s.replace(" ", "T") + "Z") : NaN; }
  function fmtElapsed(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    return s < 120 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s";
  }
  var started = NaN, finished = NaN, status = el.dataset.status;
  function setBadge(s) {
    st.innerHTML = "";
    var b = document.createElement("span"); b.className = "badge " + s;
    if (s === "running") { var sp = document.createElement("span"); sp.className = "spin"; b.appendChild(sp); }
    b.appendChild(document.createTextNode(label[s] || s));
    st.appendChild(b);
  }
  function link(href, text, cls) {
    var a = document.createElement("a"); a.href = href; a.textContent = text; a.className = cls; return a;
  }
  function render(j) {
    var atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    log.textContent = j.log; if (atBottom) log.scrollTop = log.scrollHeight;
    status = j.status; setBadge(j.status);
    started = parseUtc(j.started); finished = parseUtc(j.finished);
    if (j.error) err.textContent = j.error;
    stage.textContent = terminal.indexOf(j.status) >= 0 ? "" : stageOf(j.log || "");
    if (j.reports && j.reports.length) {
      links.textContent = "";
      j.reports.forEach(function (n) { links.appendChild(link("/reports/" + encodeURIComponent(n), "📄 " + n.replace(/\.md$/, ""), "btn small")); links.appendChild(document.createTextNode(" ")); });
      (j.charts || []).forEach(function (n) { links.appendChild(link("/reports/" + encodeURIComponent(n), "📈 chart", "btn secondary small")); links.appendChild(document.createTextNode(" ")); });
    }
    if (terminal.indexOf(j.status) >= 0 && cancel) cancel.style.display = "none";
  }
  function tickClock() {
    if (!isNaN(started)) {
      var end = !isNaN(finished) ? finished : Date.now();
      elapsed.textContent = (terminal.indexOf(status) >= 0 ? "took " : "running for ") + fmtElapsed(end - started);
    } else if (status === "queued") elapsed.textContent = "waiting for a free slot";
    if (terminal.indexOf(status) < 0) setTimeout(tickClock, 1000);
  }
  function poll() {
    fetch("/api/jobs/" + id, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (j) {
      render(j);
      if (terminal.indexOf(j.status) < 0) setTimeout(poll, 2000);
    }).catch(function () { setTimeout(poll, 4000); });
  }
  poll();
  tickClock();
})();
