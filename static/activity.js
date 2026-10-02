// Activity tray, system-problem banner, and About-dialog diagnostics.
// Loaded after app.js (shares its globals: el, api, showToast, escapeHtml).

// ------------------------------------------------------------ system status --
// A macOS folder-permission block or an unplugged drive used to show up as
// an empty library plus a bare "500". The server now diagnoses both (see
// system_status.py); this puts the explanation, and the one-click fix,
// where it can't be missed.
let systemWasOk = true;
async function checkSystemStatus() {
  let status;
  try {
    status = await api("/system/status");
  } catch (e) {
    return true;
  }
  const banner = el("system-banner");
  if (status.ok) {
    banner.classList.add("hidden");
    if (!systemWasOk) {
      // The drive/permission came back mid-session: pick up where we left off.
      systemWasOk = true;
      showToast("Reconnected — refreshing your library.", { kind: "success" });
      loadFacets().then(() => loadTracks(true)).catch(() => {});
    }
    return true;
  }
  systemWasOk = false;
  const problem = status.problems[0];
  el("system-banner-text").textContent = problem.message;
  el("system-banner-fix").classList.toggle("hidden", problem.fix !== "open_privacy_settings");
  banner.classList.remove("hidden");
  return false;
}

// An external drive can disappear (or come back) at any moment, long after
// the one check at launch -- so keep watching. One cheap request every 20s,
// paused while the window is in the background.
setInterval(() => { if (document.visibilityState === "visible") checkSystemStatus(); }, 20000);

el("system-banner-fix").addEventListener("click", () => {
  api("/system/open-privacy-settings", { method: "POST" }).catch(() => {});
});
el("system-banner-retry").addEventListener("click", async () => {
  if (await checkSystemStatus()) window.location.reload();
});

// ------------------------------------------------------------ activity tray --
const activity = { jobs: [], open: false, timer: null };

function activityStatusText(job) {
  if (job.running) {
    return job.total ? `${job.done.toLocaleString()} / ${job.total.toLocaleString()}` : "Working…";
  }
  if (job.error) return `Failed: ${job.error}`;
  if (job.cancelled) return "Cancelled";
  return job.summary || "Done";
}

function renderActivity() {
  const list = el("activity-list");
  if (!activity.jobs.length) {
    list.innerHTML = `<div class="activity-empty">Nothing running. Long tasks — scans, lookups, conversions — show up here with a Cancel button.</div>`;
    return;
  }
  list.innerHTML = activity.jobs.map((j) => {
    const bar = j.running
      ? `<div class="activity-bar"><div class="activity-bar-fill${j.percent == null ? " indeterminate" : ""}" style="width:${j.percent == null ? 40 : j.percent}%"></div></div>`
      : "";
    const state = j.running ? "running" : (j.error ? "failed" : (j.cancelled ? "cancelled" : "done"));
    return `
      <div class="activity-item ${state}" data-name="${escapeHtml(j.name)}">
        <div class="activity-item-head">
          <span class="activity-item-label">${escapeHtml(j.label)}</span>
          ${j.can_cancel ? `<button class="btn-small activity-cancel">Cancel</button>` : ""}
          ${!j.running ? `<button class="btn-icon activity-dismiss" title="Dismiss">✕</button>` : ""}
        </div>
        <div class="activity-item-status">${escapeHtml(activityStatusText(j))}</div>
        ${bar}
      </div>`;
  }).join("");
}

async function refreshActivity() {
  try {
    activity.jobs = await api("/jobs");
  } catch (e) {
    activity.timer = setTimeout(refreshActivity, 5000);
    return;
  }
  const running = activity.jobs.filter((j) => j.running);
  const badge = el("activity-badge");
  badge.textContent = running.length;
  badge.classList.toggle("hidden", running.length === 0);
  el("activity-btn").classList.toggle("busy", running.length > 0);
  if (activity.open) renderActivity();
  activity.timer = setTimeout(refreshActivity, running.length || activity.open ? 1000 : 4000);
}

el("activity-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  activity.open = !activity.open;
  el("activity-panel").classList.toggle("hidden", !activity.open);
  if (activity.open) {
    renderActivity();
    clearTimeout(activity.timer);
    refreshActivity();
  }
});
document.addEventListener("click", (e) => {
  if (activity.open && !el("activity-panel").contains(e.target) && e.target.id !== "activity-btn") {
    activity.open = false;
    el("activity-panel").classList.add("hidden");
  }
});
el("activity-list").addEventListener("click", async (e) => {
  const item = e.target.closest(".activity-item");
  if (!item) return;
  const name = item.dataset.name;
  if (e.target.closest(".activity-cancel")) {
    e.target.closest(".activity-cancel").disabled = true;
    await api(`/jobs/${name}/cancel`, { method: "POST" }).catch(() => {});
    refreshActivity();
  } else if (e.target.closest(".activity-dismiss")) {
    await api(`/jobs/${name}/dismiss`, { method: "POST" }).catch(() => {});
    refreshActivity();
  }
});

// ------------------------------------------------------------- diagnostics --
el("about-copy-diagnostics").addEventListener("click", async () => {
  const btn = el("about-copy-diagnostics");
  btn.disabled = true;
  try {
    const data = await api("/diagnostics");
    try {
      await navigator.clipboard.writeText(data.text);
      showToast("Diagnostics copied — paste them wherever you're reporting the problem.", { kind: "success" });
    } catch (clipErr) {
      el("about-diagnostics-text").value = data.text;
      el("about-diagnostics-text").classList.remove("hidden");
      el("about-diagnostics-text").select();
      showToast("Couldn't reach the clipboard — the text is selected below, copy it from there.", { kind: "info" });
    }
  } catch (e) {
    showToast(e.message, { kind: "error" });
  } finally {
    btn.disabled = false;
  }
});
el("about-show-log").addEventListener("click", async () => {
  try {
    const r = await api("/system/reveal-log", { method: "POST" });
    if (!r.ok) showToast(r.error || "No log file yet.", { kind: "info" });
  } catch (e) {
    showToast(e.message, { kind: "error" });
  }
});

checkSystemStatus();
refreshActivity();

// Thumbnails: after the library has loaded, ask the server to prepare any
// that are missing (it only starts work if there is some). Shows up in the
// Activity tray; scrolling gets smoother as it finishes.
setTimeout(() => { api("/art/warm", { method: "POST" }).catch(() => {}); }, 6000);
