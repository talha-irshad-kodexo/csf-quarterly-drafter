/* Progressive enhancement for the server-rendered build.
 *
 * index.html renders everything from JavaScript; here the server renders the
 * same DOM and this file supplies only the behaviour that markup cannot:
 * delegated actions, dialogs, override panels, the dropzone, live counters,
 * the citation flash, the progress stream and the theme toggle.
 *
 * The action names are index.html's, so the two builds can be read together.
 */

(function () {
  "use strict";

  var THEME_KEY = "csf-theme";
  var SIDEBAR_KEY = "csf-sidebar";
  var lastFocus = null;

  var $ = function (s, el) { return (el || document).querySelector(s); };
  var $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };

  /* ---- toasts ---------------------------------------------------------- */

  function toast(message, isError) {
    var host = $("#toasts");
    if (!host) return;
    var el = document.createElement("div");
    el.className = "toast" + (isError ? " err" : "");
    el.textContent = message;
    host.appendChild(el);
    setTimeout(function () { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3600);
    setTimeout(function () { el.remove(); }, 4000);
  }

  function fadeExistingToasts() {
    $$("#toasts .toast").forEach(function (el) {
      setTimeout(function () { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3600);
      setTimeout(function () { el.remove(); }, 4000);
    });
  }

  /* ---- theme ----------------------------------------------------------- */

  function currentTheme() {
    if (document.documentElement.classList.contains("dark")) return "dark";
    try {
      var stored = localStorage.getItem(THEME_KEY);
      if (stored === "dark" || stored === "light") return stored;
    } catch (e) {}
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function applyTheme(mode) {
    document.documentElement.classList.toggle("dark", mode === "dark");
    try { localStorage.setItem(THEME_KEY, mode); } catch (e) {}
    var btn = $('[data-action="toggle-theme"]');
    if (btn) btn.textContent = mode === "dark" ? "☾ Dark" : "☀ Light";
  }

  /* ---- the sidebar ------------------------------------------------------
   *
   * Collapsed is a rail of icons, not an empty column: the sidebar is how you
   * know where you are, and a version of it that vanishes takes that with it.
   * The state is written before first paint by the inline script in base.html
   * — this only has to flip and remember it.
   */

  function railed() {
    return document.documentElement.classList.contains("rail");
  }

  function applyRail(on) {
    document.documentElement.classList.toggle("rail", on);
    try { localStorage.setItem(SIDEBAR_KEY, on ? "rail" : "open"); } catch (e) {}
    var btn = $('[data-action="toggle-sidebar"]');
    if (btn) btn.setAttribute("aria-expanded", on ? "false" : "true");
  }

  /* ---- tabs -------------------------------------------------------------
   *
   * Every panel is in the DOM; this only chooses which one is shown. The
   * chosen tab goes in the URL hash so a reload, a bookmark and a link into
   * the audit trail all land on the same ledger.
   */

  function showTab(set, name) {
    var chosen = null;
    $$("[data-tab]", set).forEach(function (tab) {
      var on = tab.dataset.tab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      if (on) chosen = tab;
    });
    $$("[data-panel]", set).forEach(function (panel) {
      panel.classList.toggle("active", panel.dataset.panel === name);
    });
    return chosen;
  }

  function wireTabs() {
    $$(".tabset").forEach(function (set) {
      var wanted = (window.location.hash || "").replace("#tab-", "");
      var first = $("[data-tab]", set);
      if (!first) return;
      var name = $('[data-tab="' + wanted + '"]', set) ? wanted : first.dataset.tab;
      showTab(set, name);
    });
  }

  /* ---- dialogs --------------------------------------------------------- */

  function openDialog(id) {
    var overlay = document.getElementById(id);
    if (!overlay) return;
    lastFocus = document.activeElement;
    overlay.hidden = false;
    var first = overlay.querySelector("input, textarea, button");
    if (first) first.focus();
  }

  function closeDialogs() {
    $$(".overlay").forEach(function (o) { o.hidden = true; });
    if (lastFocus && lastFocus.focus) lastFocus.focus();
  }

  /* ---- override panels -------------------------------------------------
   *
   * index.html renders the panel into a slot on demand and keeps Change
   * disabled until a reason is typed. Same rule here: the reasoning has to
   * travel with the row, so an override without one cannot be submitted.
   */

  function overridePanel(field) {
    return $('[data-override-for="' + field + '"]');
  }

  function syncOverrideConfirm(panel) {
    var reason = panel.querySelector("[data-override-reason]");
    var confirm = panel.querySelector("[data-override-confirm]");
    if (confirm) confirm.disabled = !(reason && reason.value.trim());
  }

  function toggleOverride(field, show) {
    var panel = overridePanel(field);
    if (!panel) return;
    panel.hidden = show === undefined ? !panel.hidden : !show;
    if (panel.hidden) return;
    syncOverrideConfirm(panel);
    var reason = panel.querySelector("[data-override-reason]");
    if (reason) reason.focus();
  }

  /* ---- narrative counters ---------------------------------------------- */

  function autosize(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.max(96, ta.scrollHeight + 2) + "px";
  }

  function updateCounter(ta) {
    var max = Number(ta.dataset.max || 200);
    var counter = $('[data-counter-for="' + ta.dataset.narrative + '"]');
    if (!counter) return;
    var n = ta.value.length;
    counter.textContent = n + " / " + max;
    counter.classList.toggle("danger", n > max);
    counter.classList.toggle("warn", n <= max && n >= max - 20);
  }

  function wireNarratives() {
    $$("[data-narrative]").forEach(function (ta) { autosize(ta); updateCounter(ta); });
  }

  /* ---- citation arrival ------------------------------------------------
   *
   * index.html scrolls to the first cited line and flashes the range. Landing
   * on the right document is not the same as landing on the right lines.
   */

  function revealCitedLines() {
    // Whichever view is on top. The rendered document is the default tab, and
    // scrolling the hidden source instead would leave the reader looking at
    // the top of a document whose cited paragraph is somewhere below.
    var marked = $$(".md-block.hl").filter(function (el) { return el.offsetParent !== null; });
    if (!marked.length) marked = $$(".codeline.hl");
    if (!marked.length) return;
    marked[0].scrollIntoView({ block: "center" });
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var flash = getComputedStyle(document.documentElement).getPropertyValue("--flash").trim();
    marked.forEach(function (el) {
      el.animate([{ backgroundColor: flash }, { backgroundColor: "transparent" }],
        { duration: 1300, easing: "ease-out" });
    });
  }

  /* ---- the dropzone ---------------------------------------------------- */

  function submitFiles(files) {
    var input = $("#filepick");
    var form = $("#dropzone-form");
    if (!input || !form || !files || !files.length) return;
    input.files = files;
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  }

  function wireDropzone() {
    var zone = $("#dropzone");
    var input = $("#filepick");
    if (!zone || !input) return;
    input.addEventListener("change", function () { submitFiles(input.files); });
    ["dragenter", "dragover"].forEach(function (n) {
      zone.addEventListener(n, function (e) { e.preventDefault(); zone.classList.add("drag"); });
    });
    ["dragleave", "drop"].forEach(function (n) {
      zone.addEventListener(n, function (e) { e.preventDefault(); zone.classList.remove("drag"); });
    });
    zone.addEventListener("drop", function (e) {
      submitFiles(e.dataTransfer && e.dataTransfer.files);
    });
  }

  /* ---- the progress stream ---------------------------------------------
   *
   * Events mean a node just finished. Reading fires once per document, so
   * that row stays active until reconciliation starts. The log types the
   * method as it happens — the same sequence, read as a stream rather than
   * a frozen list.
   */

  var STAGE_ORDER = ["load", "read_document", "reconcile", "assess", "compose", "validate", "review"];
  var STAGE_LINES = {
    load: [
      "Opening the evidence folder.",
      "Classifying each file before anything is compared."
    ],
    read_document: [
      "Reading each document on its own — no comparison yet.",
      "A sentence is kept only when it cites a line."
    ],
    reconcile: [
      "Where two accounts disagree, recency and first-hand win.",
      "Silences are recorded as gaps, not filled in."
    ],
    assess: [
      "Matching what was found to the success measure.",
      "The traffic light is a closed vocabulary: Red, Amber, Green, Blue."
    ],
    compose: [
      "Writing the three narrative fields to two hundred characters.",
      "Every sentence still has to cite."
    ],
    validate: [
      "Checking the row against the CSF schema.",
      "A value outside the vocabulary is rejected, not guessed."
    ],
    review: [
      "The draft is ready. Nothing has been submitted.",
      "Opening the review."
    ]
  };

  function reduceMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  function setStageIco(row, state) {
    var ico = row.querySelector(".stage-ico");
    if (!ico) return;
    if (state === "done") ico.textContent = "✓";
    else if (state === "active") ico.innerHTML = '<span class="stage-spin"></span>';
    else if (state === "fail") ico.textContent = "✕";
    else ico.textContent = "○";
  }

  function setStageState(row, state) {
    row.className = "stage-row " + state;
    setStageIco(row, state);
  }

  function updateMeter(list) {
    var rows = $$(".stage-row", list).filter(function (r) { return r.dataset.stage !== "failed"; });
    var done = rows.filter(function (r) { return r.classList.contains("done"); }).length;
    var fill = $("#run-meter-fill");
    var label = $("#run-meter-label");
    if (fill && rows.length) fill.style.width = Math.round((done / rows.length) * 100) + "%";
    if (label && rows.length) label.textContent = done + " of " + rows.length + " passes";
  }

  function makeStream(log) {
    var queue = [];
    var busy = false;
    var caret = document.createElement("span");
    caret.className = "stream-caret";
    caret.setAttribute("aria-hidden", "true");
    log.appendChild(caret);

    function paint(text, cls, animate, done) {
      var line = document.createElement("div");
      line.className = "stream-line" + (cls ? " " + cls : "");
      log.insertBefore(line, caret);
      if (!animate || reduceMotion()) {
        line.textContent = text;
        log.scrollTop = log.scrollHeight;
        done();
        return;
      }
      var i = 0;
      (function tick() {
        i += 1;
        line.textContent = text.slice(0, i);
        log.scrollTop = log.scrollHeight;
        if (i < text.length) {
          var ch = text.charAt(i - 1);
          setTimeout(tick, ch === "." ? 90 : 14);
        } else {
          setTimeout(done, 160);
        }
      })();
    }

    function pump() {
      if (busy || !queue.length) return;
      busy = true;
      var item = queue.shift();
      paint(item.text, item.cls, item.animate, function () {
        busy = false;
        pump();
      });
    }

    return {
      push: function (text, cls) {
        queue.push({ text: text, cls: cls || "", animate: true });
        pump();
      },
      dump: function (text, cls) {
        queue.push({ text: text, cls: cls || "", animate: false });
        pump();
      },
      idle: function () { return !busy && !queue.length; }
    };
  }

  function followRun(list) {
    var log = $("#run-stream");
    var stream = log ? makeStream(log) : null;
    var started = {};
    var source = new EventSource("/runs/" + list.dataset.thread + "/events");

    function startStage(key, instant) {
      if (!stream || started[key]) return;
      started[key] = true;
      var lines = STAGE_LINES[key] || [];
      var row = list.querySelector('[data-stage="' + key + '"]');
      var name = row ? (row.querySelector(".stage-name") || {}).textContent : key;
      if (name) (instant ? stream.dump : stream.push).call(stream, name, "stage");
      lines.forEach(function (line) {
        (instant ? stream.dump : stream.push).call(stream, line, "");
      });
    }

    $$(".stage-row", list).forEach(function (row) {
      var key = row.dataset.stage;
      if (row.classList.contains("done")) {
        startStage(key, true);
        var detail = row.querySelector(".stage-detail");
        if (stream && detail && detail.textContent.trim() && !detail.querySelector(".ellipsis"))
          stream.dump(detail.textContent.trim(), "result");
      } else if (row.classList.contains("active")) {
        startStage(key, false);
      }
    });

    function markDoneThrough(idx) {
      STAGE_ORDER.forEach(function (key, here) {
        if (here > idx) return;
        var row = list.querySelector('[data-stage="' + key + '"]');
        if (row) setStageState(row, "done");
      });
    }

    source.onmessage = function (message) {
      var event = JSON.parse(message.data);
      if (event.stage === "done") {
        markDoneThrough(STAGE_ORDER.length - 1);
        updateMeter(list);
        source.close();
        var go = function () { window.location.reload(); };
        if (stream && !reduceMotion()) setTimeout(go, 700);
        else go();
        return;
      }
      if (event.stage === "failed") {
        source.close();
        window.location.reload();
        return;
      }

      var idx = STAGE_ORDER.indexOf(event.stage);
      if (event.stage === "read_document") {
        markDoneThrough(idx - 1);
        startStage("read_document", false);
        var reading = list.querySelector('[data-stage="read_document"]');
        if (reading) {
          setStageState(reading, "active");
          var detail = reading.querySelector(".stage-detail");
          if (detail && event.detail) detail.textContent = event.detail;
        }
      } else if (idx >= 0) {
        if (!started[event.stage]) startStage(event.stage, true);
        markDoneThrough(idx);
        var nextKey = STAGE_ORDER[idx + 1];
        if (nextKey) {
          var next = list.querySelector('[data-stage="' + nextKey + '"]');
          if (next) setStageState(next, "active");
          startStage(nextKey, false);
        }
      }
      if (stream && event.detail) stream.push(event.detail, "result");
      updateMeter(list);
    };

    source.onerror = function () { source.close(); window.location.reload(); };
  }

  /* ---- delegated actions ------------------------------------------------ */

  document.addEventListener("click", function (event) {
    var target = event.target.closest("[data-action]");

    // Row navigation, but never when the click landed on a real control.
    var row = event.target.closest('tr.rowlink[data-href]');
    if (row && !event.target.closest("a, button, form")) {
      window.location.href = row.getAttribute("data-href");
      return;
    }
    if (!target) return;

    var action = target.dataset.action;

    if (action === "overlay-close") { if (event.target === target) closeDialogs(); return; }

    switch (action) {
      case "toggle-theme":
        applyTheme(currentTheme() === "dark" ? "light" : "dark");
        break;
      case "toggle-sidebar":
        applyRail(!railed());
        break;
      case "tab": {
        var set = target.closest(".tabset");
        if (!set) break;
        showTab(set, target.dataset.tab);
        // replaceState, not a hash assignment: choosing a tab is not a
        // navigation, and filling the back button with them makes leaving the
        // page take one press per tab looked at.
        if (window.history && window.history.replaceState)
          window.history.replaceState(null, "", "#tab-" + target.dataset.tab);
        break;
      }
      case "open-settings":
        openDialog("settings-dialog");
        break;
      case "close-dialog":
        closeDialogs();
        break;
      case "open-approve":
        if (!target.disabled) openDialog("approve-dialog");
        break;
      case "open-delete":
        openDialog("delete-dialog");
        break;
      case "confirm-delete":
        if (pendingDelete) pendingDelete.submit();
        break;
      case "open-run":
        window.location.href = target.dataset.href;
        break;
      case "toggle-attn": {
        var group = target.closest(".attn-group");
        var open = group.classList.toggle("open");
        target.setAttribute("aria-expanded", open ? "true" : "false");
        break;
      }
      case "jump-attn": {
        event.preventDefault();
        var head = $(target.getAttribute("href"));
        if (head) {
          head.closest(".attn-group").classList.add("open");
          head.scrollIntoView({ block: "center" });
        }
        break;
      }
      case "jump-field": {
        event.preventDefault();
        var fieldId = (target.getAttribute("href") || "").replace(/^#/, "");
        var field = fieldId && document.getElementById(fieldId);
        if (field) field.scrollIntoView({ block: "center", behavior: "smooth" });
        break;
      }
      case "open-override":
        toggleOverride(target.dataset.field);
        break;
      case "cancel-override":
        toggleOverride(target.dataset.field, false);
        break;
      case "pick-files":
        if (!event.target.closest("button")) $("#filepick").click();
        break;
      case "dismiss-cite": {
        event.preventDefault();
        event.stopPropagation();
        var chip = target.closest(".chip");
        var body = new FormData();
        body.append("field", target.dataset.field);
        body.append("ref", target.dataset.ref);
        fetch("/runs/" + target.dataset.thread + "/citation", { method: "POST", body: body })
          .then(function (r) { return r.json(); })
          .then(function (data) {
            chip.classList.toggle("dismissed", data.dismissed);
            toast(data.dismissed
              ? "Citation " + target.dataset.ref + " marked irrelevant"
              : "Citation " + target.dataset.ref + " restored");
          })
          .catch(function () { toast("Could not record that", true); });
        break;
      }
    }
  });

  document.addEventListener("input", function (event) {
    var el = event.target;
    if (el.dataset && el.dataset.narrative) { autosize(el); updateCounter(el); return; }
    if (el.hasAttribute && el.hasAttribute("data-override-reason")) {
      syncOverrideConfirm(el.closest(".override-panel"));
    }
  });

  document.addEventListener("change", function (event) {
    var input = event.target;

    // Changing a file's role moves the file, so the select submits its form.
    if (input.classList && input.classList.contains("role-sel")) {
      var form = input.closest("form");
      if (form) { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
      return;
    }

    // The traffic-light picker is radio inputs styled as buttons: keep the
    // selected ring in step with the checked input.
    if (input.type !== "radio" && input.type !== "checkbox") return;
    var group = input.closest(".tl-pick");
    if (!group) return;
    if (input.type === "radio") {
      $$(".tl-opt", group).forEach(function (o) { o.classList.remove("sel"); });
      input.closest(".tl-opt").classList.add("sel");
    } else {
      input.closest(".tl-opt").classList.toggle("sel", input.checked);
    }
  });

  var pendingDelete = null;

  document.addEventListener("submit", function (event) {
    var form = event.target;

    // Deleting is a real form post; the dialog is the enhancement on top.
    if (form.hasAttribute("data-confirm-delete")) {
      event.preventDefault();
      pendingDelete = form;
      openDialog("delete-dialog");
      return;
    }

    // Its own dialog, because the sentence it has to say is a different one:
    // this removes runs still in progress, and every trail with them.
    if (form.hasAttribute("data-confirm-delete-all")) {
      event.preventDefault();
      pendingDelete = form;
      openDialog("delete-all-dialog");
      return;
    }

    var busy = form.getAttribute("data-busy");
    if (!busy) return;
    var button = form.querySelector("button[type=submit]");
    if (button) { button.disabled = true; button.textContent = busy; }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { closeDialogs(); return; }
    if (event.key === "Enter") {
      var row = event.target.closest && event.target.closest('tr.rowlink[data-href]');
      if (row && event.target === row) {
        event.preventDefault();
        window.location.href = row.getAttribute("data-href");
        return;
      }
      if (event.target.id === "dropzone") $("#filepick").click();
    }
    var overlay = $$(".overlay").filter(function (o) { return !o.hidden; })[0];
    if (!overlay || event.key !== "Tab") return;
    var focusables = overlay.querySelectorAll("button, [href], input, select, textarea");
    if (!focusables.length) return;
    var first = focusables[0], last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });

  function wireMarkdownPreview() {
    var source = $("[data-md-source]");
    var preview = $("#md-preview");
    if (!source || !preview) return;
    var timer = null;
    function renderPreview() {
      var body = new FormData();
      body.append("text", source.value);
      fetch("/evidence/preview", { method: "POST", body: body })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          preview.innerHTML = data.html || '<p class="muted">Nothing to preview yet.</p>';
        })
        .catch(function () {});
    }
    source.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(renderPreview, 280);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(currentTheme());
    applyRail(railed());
    fadeExistingToasts();
    wireNarratives();
    wireTabs();
    revealCitedLines();
    wireDropzone();
    wireMarkdownPreview();
    var progress = $("#progress");
    if (progress && progress.dataset.thread) followRun(progress);
  });
})();
