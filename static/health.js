// Library health modal (see health.py for what's checked and why).
// Loaded after app.js (shares el, api, showToast, escapeHtml, customConfirm,
// pollProgress, loadFacets, loadTracks).

const healthState = { result: null, running: false };

function renderHealth() {
  const result = healthState.result;
  const box = el("health-issues");
  const summary = el("health-summary");
  if (!result) {
    box.innerHTML = "";
    return;
  }
  const actionable = result.issues.filter((i) => i.severity !== "info");
  summary.textContent = result.healthy
    ? `✓ Nothing needs attention (${result.tracks.toLocaleString()} tracks checked).`
    : `${actionable.length} thing${actionable.length === 1 ? "" : "s"} worth a look across ${result.tracks.toLocaleString()} tracks.`;
  if (!result.issues.length) {
    box.innerHTML = `<div class="health-ok">All clear.</div>`;
    el("health-repair").disabled = true;
    return;
  }
  box.innerHTML = result.issues.map((i) => `
    <div class="health-issue" data-id="${escapeHtml(i.id)}">
      <div class="health-issue-head">
        <span class="health-pill ${i.severity}">${i.severity}</span>
        <span class="health-issue-title">${escapeHtml(i.title)}</span>
        <span class="health-issue-count">${i.count.toLocaleString()}</span>
      </div>
      <p class="health-issue-detail">${escapeHtml(i.detail)}</p>
      ${i.examples.length ? `<ul class="health-issue-examples">${i.examples.map((e) => `<li>${escapeHtml(e)}</li>`).join("")}</ul>` : ""}
      ${i.repair ? `<label class="health-issue-fix"><input type="checkbox" data-repair="${escapeHtml(i.repair)}" ${i.repair === "remove_missing_tracks" ? "" : "checked"}> ${escapeHtml(i.repair_label)}</label>` : ""}
    </div>`).join("");
  updateHealthRepairButton();
}

function updateHealthRepairButton() {
  const n = el("health-issues").querySelectorAll("input[data-repair]:checked").length;
  el("health-repair").disabled = n === 0;
  el("health-repair").textContent = n ? `Repair selected (${n})` : "Repair selected";
}

async function runHealthCheck({ quiet = false } = {}) {
  if (healthState.running) return null;
  healthState.running = true;
  const bar = el("health-progress");
  const fill = el("health-progress-fill");
  if (!quiet) {
    el("health-summary").textContent = "Checking…";
    el("health-issues").innerHTML = "";
    fill.style.width = "0%";
    bar.classList.remove("hidden");
  }
  try {
    const started = await api("/health/check", { method: "POST" });
    if (started.error && started.error !== "Already running") {
      if (!quiet) showToast(started.error, { kind: "error" });
      return null;
    }
    const status = await pollProgress("/health/progress", (s) => {
      if (!quiet) fill.style.width = s.total ? `${Math.min(100, (s.done / s.total) * 100)}%` : "0%";
      return s.running;
    });
    if (status.error) {
      if (!quiet) el("health-summary").textContent = `Check failed: ${status.error}`;
      return null;
    }
    healthState.result = status.result;
    if (!quiet) renderHealth();
    return status.result;
  } catch (e) {
    if (!quiet) showToast(e.message, { kind: "error" });
    return null;
  } finally {
    healthState.running = false;
    bar.classList.add("hidden");
  }
}

el("open-health").addEventListener("click", async () => {
  el("health-backdrop").classList.remove("hidden");
  if (healthState.result) renderHealth();
  else await runHealthCheck();
});
el("health-close").addEventListener("click", () => el("health-backdrop").classList.add("hidden"));
el("health-backdrop").addEventListener("click", (e) => { if (e.target.id === "health-backdrop") el("health-backdrop").classList.add("hidden"); });
el("health-run").addEventListener("click", () => runHealthCheck());
el("health-issues").addEventListener("change", updateHealthRepairButton);

el("health-repair").addEventListener("click", async () => {
  const picked = Array.from(el("health-issues").querySelectorAll("input[data-repair]:checked")).map((c) => c.dataset.repair);
  if (!picked.length) return;
  const labels = Array.from(el("health-issues").querySelectorAll("input[data-repair]:checked")).map((c) => "• " + c.parentElement.textContent.trim());
  if (!(await customConfirm(`Apply these repairs?\n\n${labels.join("\n")}\n\nA snapshot of the library is taken first.`, { okLabel: "Repair" }))) return;
  el("health-repair").disabled = true;
  try {
    const out = await api("/health/repair", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ repairs: picked }),
    });
    if (!out.ok) {
      showToast(out.error || "Couldn't repair.", { kind: "error" });
    } else {
      const total = Object.values(out.results).reduce((a, b) => a + b, 0);
      showToast(`Repaired ${total.toLocaleString()} item${total === 1 ? "" : "s"}.`, { kind: "success" });
      await loadFacets();
      await loadTracks(true);
    }
  } catch (e) {
    showToast(e.message, { kind: "error" });
  }
  await runHealthCheck();
});

// Once a day, quietly, shortly after launch -- only if nothing else is busy
// and the library is reachable. Speaks up only if something needs a look.
(async function maybeAutoCheckHealth() {
  await new Promise((r) => setTimeout(r, 25000));
  try {
    const [last, jobs, status] = await Promise.all([api("/health/last"), api("/jobs"), api("/system/status")]);
    const dayAgo = Date.now() / 1000 - 24 * 3600;
    if (!status.ok || jobs.some((j) => j.running)) return;
    if (last.last_check_at && last.last_check_at > dayAgo) return;
    const result = await runHealthCheck({ quiet: true });
    if (result && !result.healthy) {
      const n = result.issues.filter((i) => i.severity !== "info").length;
      showToast(`Library health: ${n} thing${n === 1 ? "" : "s"} to review — open 🩺 Health.`, { kind: "info", duration: 9000 });
    }
  } catch (e) { /* a background nicety -- never worth surfacing its own failure */ }
})();
