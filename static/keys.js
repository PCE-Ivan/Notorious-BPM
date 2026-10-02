// Keyboard-first browsing of the track list. Loaded after app.js (shares its
// globals). The player's own shortcuts (Space, N/P, arrows, 1-5, S/Q/L/M) live
// next to the player in app.js -- arrows stay seek/volume, so list navigation
// is J/K (as in Gmail, Vim, Finder's type-select neighbours) instead.
//
//   J / K          move the highlight down / up      (Shift: extend a selection)
//   G / Shift+G    jump to the first / last loaded track
//   Enter          play the highlighted track
//   X              select / deselect it
//   Cmd/Ctrl+A     select every track matching the filters
//   E / A          edit tags / add to playlist (acts on the selection)
//   /              focus search          ?  help          Esc  clear selection

function navContext() {
  if (!el("playlist-view").classList.contains("hidden")) {
    return { rows: state.currentList || [], windowed: false };
  }
  if (state.view === "tracks" && !el("library-view").classList.contains("hidden")) {
    return { rows: state.libraryRows, windowed: true };
  }
  return null;
}

// Scrolls so the row at `idx` is fully visible, clear of the sticky selection
// toolbar -- minimal movement, like a native list.
function revealNavRow(ctx, idx) {
  const scroller = el("main-scroll");
  if (ctx.windowed) {
    if (!vwin.rowH) return;
    const hostTop = libraryHost().getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
    const bar = el("selection-toolbar");
    const topInset = (bar && !bar.classList.contains("hidden") ? bar.offsetHeight + 8 : 0) + 6;
    const top = hostTop + idx * vwin.rowH;
    const bottom = top + vwin.rowH;
    if (top < scroller.scrollTop + topInset) scroller.scrollTop = Math.max(0, top - topInset);
    else if (bottom > scroller.scrollTop + scroller.clientHeight - 6) scroller.scrollTop = bottom - scroller.clientHeight + 6;
    renderLibraryWindow();
    if (idx > ctx.rows.length - 25) loadTracks(false);  // nearing the end of what's loaded
  } else {
    const row = document.querySelector(`#playlist-tracks .track-row[data-id="${ctx.rows[idx].id}"]`);
    if (row) row.scrollIntoView({ block: "nearest" });
  }
}

function moveNavCursor(delta, { extend = false, to = null } = {}) {
  const ctx = navContext();
  if (!ctx || !ctx.rows.length) return;
  let idx = ctx.rows.findIndex((t) => t.id === state.cursorId);
  if (idx < 0) {
    // First use: start from the playing track if it's in view, else the top.
    idx = state.currentTrack ? ctx.rows.findIndex((t) => t.id === state.currentTrack.id) : -1;
    if (idx < 0) idx = delta > 0 ? -1 : ctx.rows.length;
  }
  const next = to != null ? to : Math.max(0, Math.min(ctx.rows.length - 1, idx + delta));
  const id = ctx.rows[next].id;
  if (extend) {
    if (state.anchorId == null) state.anchorId = ctx.rows[Math.max(0, idx)].id;
    // Re-derive the range from the anchor so shrinking back works too.
    state.selected.clear();
    selectRangeTo(id, ctx.rows);
  } else {
    setNavCursor(id);
  }
  revealNavRow(ctx, next);
  setNavCursor(id);  // the row may only have been created by revealNavRow's render
}

document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable) return;
  if (document.querySelector(".modal-backdrop:not(.hidden)")) return;
  const ctx = navContext();
  const mod = e.metaKey || e.ctrlKey;

  if (mod && !e.altKey && (e.key === "a" || e.key === "A")) {
    if (!ctx) return;
    e.preventDefault();
    el("select-all-filtered").click();
    return;
  }
  if (mod || e.altKey) return;

  switch (e.key) {
    case "j": case "J": if (ctx) { e.preventDefault(); moveNavCursor(1, { extend: e.shiftKey }); } break;
    case "k": case "K": if (ctx) { e.preventDefault(); moveNavCursor(-1, { extend: e.shiftKey }); } break;
    case "g": if (ctx) { e.preventDefault(); moveNavCursor(0, { to: 0 }); } break;
    case "G": if (ctx) { e.preventDefault(); moveNavCursor(0, { to: ctx.rows.length - 1 }); } break;
    case "Enter": {
      if (!ctx || tag === "button" || tag === "a" || tag === "summary") break;  // Enter belongs to the focused control
      const t = ctx.rows.find((r) => r.id === state.cursorId);
      if (t) { e.preventDefault(); playTrack(t, ctx.rows); }
      break;
    }
    case "x": case "X": {
      if (!ctx || state.cursorId == null) break;
      e.preventDefault();
      if (state.selected.has(state.cursorId)) state.selected.delete(state.cursorId);
      else state.selected.add(state.cursorId);
      state.anchorId = state.cursorId;
      syncSelectionView();
      updateSelectionToolbar();
      break;
    }
    case "e": case "E":
      if (state.selected.size) { e.preventDefault(); el("selection-edit-tags").click(); }
      break;
    case "a": case "A":
      if (state.selected.size) { e.preventDefault(); el("selection-add-to-playlist").click(); }
      break;
    case "/":
      e.preventDefault();
      el("search").focus();
      el("search").select();
      break;
    case "?":
      el("open-help").click();
      break;
    case "Escape":
      if (state.selected.size) clearSelection();
      break;
  }
});
