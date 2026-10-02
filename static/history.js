// Change history / undo (see journal.py). Loaded after app.js.

const HISTORY_KIND_ICON = {
  fill_genres: "🎼", fill_years: "📅", unify_genre: "🎚", fix_artist_title: "✏️", organize: "🗂", edit_tags: "🏷",
};

function historyWhen(iso) {
  const d = new Date(iso + "Z");
  return isNaN(d) ? iso : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

async function renderHistory() {
  const list = el("history-list");
  let ops;
  try {
    ops = await api("/history");
  } catch (e) {
    list.innerHTML = `<div class="health-ok">${escapeHtml(e.message)}</div>`;
    return;
  }
  if (!ops.length) {
    list.innerHTML = `<div class="health-ok">Nothing recorded yet. Bulk tag fixes, hand edits and Organize show up here once they've run.</div>`;
    return;
  }
  list.innerHTML = ops.map((o) => `
    <div class="health-issue" data-id="${o.id}">
      <div class="health-issue-head">
        <span>${HISTORY_KIND_ICON[o.kind] || "•"}</span>
        <span class="health-issue-title">${escapeHtml(o.label)}</span>
        <button class="btn-small history-undo" ${o.undone ? "disabled" : ""}>${o.undone ? "Undone" : "Undo"}</button>
      </div>
      <p class="health-issue-detail">${escapeHtml(historyWhen(o.started_at))} · ${o.item_count.toLocaleString()} change${o.item_count === 1 ? "" : "s"}</p>
    </div>`).join("");
}

el("open-history").addEventListener("click", () => {
  el("history-backdrop").classList.remove("hidden");
  renderHistory();
});
el("history-close").addEventListener("click", () => el("history-backdrop").classList.add("hidden"));
el("history-backdrop").addEventListener("click", (e) => { if (e.target.id === "history-backdrop") el("history-backdrop").classList.add("hidden"); });

el("history-list").addEventListener("click", async (e) => {
  const btn = e.target.closest(".history-undo");
  if (!btn || btn.disabled) return;
  const row = btn.closest(".health-issue");
  const label = row.querySelector(".health-issue-title").textContent;
  if (!(await customConfirm(`Undo “${label}”?\n\nThe files' tags (or locations) and the library are put back. Anything you've changed again since is left as it is. A snapshot of the library is taken first.`, { okLabel: "Undo" }))) return;
  btn.disabled = true;
  const started = await api(`/history/${row.dataset.id}/undo`, { method: "POST" }).catch((err) => ({ started: false, error: err.message }));
  if (!started.started) {
    showToast(started.error || "Couldn't start the undo.", { kind: "error" });
    btn.disabled = false;
    return;
  }
  const toast = showToast("Undoing…", { duration: 0 });
  let status;
  try {
    status = await pollProgress("/history/progress", (s) => {
      toast.update(s.total ? `Undoing… ${s.done}/${s.total}` : "Undoing…");
      return s.running;
    });
  } finally {
    toast.remove();
  }
  if (status.error) {
    showToast(`Undo failed: ${status.error}`, { kind: "error" });
  } else {
    const r = status.result || {};
    showToast(`Restored ${r.restored || 0}${r.skipped ? `, left ${r.skipped} you changed since` : ""}.`, { kind: "success" });
  }
  await loadFacets();
  await loadTracks(true);
  renderHistory();
});
