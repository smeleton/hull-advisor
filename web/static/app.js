// Hull Value Advisor: tabs, live recalculation, buyer rows, and the report request.
// The server does every calculation; this file only posts the form and swaps HTML in.
(function () {
  "use strict";
  const form = document.getElementById("advisor");
  const status = document.getElementById("status");
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));

  // ---------- tabs ----------
  function showTab(id, focus) {
    tabs.forEach((t) => {
      const on = t.id === "tab-" + id;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
      if (on && focus) t.focus();
    });
    try { history.replaceState(null, "", "#" + id); } catch (e) { /* file:// or sandbox */ }
  }
  tabs.forEach((t, i) => {
    t.addEventListener("click", () => showTab(t.id.slice(4)));
    t.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (d) showTab(tabs[(i + d + tabs.length) % tabs.length].id.slice(4), true);
    });
  });
  const initial = location.hash.slice(1);
  showTab(tabs.some((t) => t.id === "tab-" + initial) ? initial : "compare");
  window.addEventListener("hashchange", () => {
    const id = location.hash.slice(1);
    if (tabs.some((t) => t.id === "tab-" + id)) showTab(id);
  });

  // ---------- swapping server fragments ----------
  function swap(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll("[data-swap]").forEach((src) => {
      const target = document.getElementById(src.dataset.swap);
      if (target) target.innerHTML = src.innerHTML;
    });
    const hash = doc.querySelector("[data-current-hash]");
    if (hash) {
      document.getElementById("current-hash").dataset.currentHash = hash.dataset.currentHash;
    }
    markStaleReport();
  }

  function markStaleReport() {
    const rep = document.querySelector("[data-report-hash]");
    const note = document.getElementById("report-stale");
    if (!rep || !note) return;
    const current = document.getElementById("current-hash").dataset.currentHash;
    note.hidden = rep.dataset.reportHash === current;
  }

  // ---------- live recalculation ----------
  let timer = null;
  let inflight = null;
  function recalc() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (inflight) inflight.abort();
      inflight = new AbortController();
      status.textContent = "Updating…";
      try {
        const res = await fetch("/calculate", {
          method: "POST", body: new FormData(form), signal: inflight.signal,
        });
        if (!res.ok) throw new Error("HTTP " + res.status);
        swap(await res.text());
        status.textContent = "";
      } catch (e) {
        if (e.name !== "AbortError") status.textContent = "Couldn't update. Check your entries.";
      }
    }, 250);
  }
  // Feedback fields (fb_*) don't change the results, so they don't trigger a recalculation.
  const isFeedback = (e) => (e.target.name || "").startsWith("fb_");
  form.addEventListener("input", (e) => { if (!isFeedback(e)) recalc(); });
  form.addEventListener("change", (e) => { if (!isFeedback(e)) recalc(); });
  form.addEventListener("submit", (e) => { e.preventDefault(); recalc(); });

  // ---------- buyer rows ----------
  const buyerBody = document.querySelector("#buyer-table tbody");
  document.getElementById("add-buyer").addEventListener("click", () => {
    const tpl = document.getElementById("buyer-row-template");
    buyerBody.appendChild(tpl.content.cloneNode(true));
    buyerBody.lastElementChild.querySelector("input").focus();
  });
  buyerBody.addEventListener("click", (e) => {
    const btn = e.target.closest(".remove-buyer");
    if (!btn) return;
    btn.closest("tr").remove();
    recalc();
  });

  // ---------- feedback ----------
  const fbBtn = document.getElementById("send-feedback");
  const fbStatus = document.getElementById("feedback-status");
  fbBtn.addEventListener("click", async () => {
    fbBtn.disabled = true;
    fbStatus.textContent = "Sending…";
    try {
      const res = await fetch("/feedback", { method: "POST", body: new FormData(form) });
      fbStatus.innerHTML = await res.text();
      if (res.ok) {
        form.querySelectorAll('[name^="fb_"]').forEach((el) => {
          if (el.type === "radio") el.checked = false; else el.value = "";
        });
      }
    } catch (e) {
      fbStatus.textContent = "Couldn't reach the server. Try again.";
    } finally {
      fbBtn.disabled = false;
    }
  });

  // ---------- report ----------
  const reportBtn = document.getElementById("generate-report");
  reportBtn.addEventListener("click", async () => {
    reportBtn.disabled = true;
    const label = reportBtn.textContent;
    reportBtn.textContent = "Writing the report… (about 30 seconds)";
    try {
      const res = await fetch("/report", { method: "POST", body: new FormData(form) });
      swap(await res.text());
    } catch (e) {
      document.getElementById("report-output").innerHTML =
        '<p class="alert alert-bad">Couldn\'t reach the server. Try again.</p>';
    } finally {
      reportBtn.disabled = false;
      reportBtn.textContent = label;
    }
  });
})();
