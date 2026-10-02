const API = "/api";

const state = {
  q: "", genre: "", decade: "", artist: "", language: "", ratedOnly: false, rating: "", sort: "artist",
  offset: 0, pageSize: 100, total: 0, loadingMore: false, hasMore: true,
  currentTrack: null, // full track object
  currentList: [], // list currently being browsed/played from (for "next" fallback)
  libraryRows: [], // every row of the main library list loaded so far (the list itself is windowed -- see renderLibraryWindow)
  history: [], // ids played this session, for excluding
  playHistory: [], // full track objects in play order, for the sidebar previous-track button
  historyPos: -1, // index into playHistory of the currently playing track
  autoplaySimilar: false,
  activePlaylistId: null,
  radioQueue: null, // {tracks:[...], name}
  selected: new Set(), // selected track ids, for multi-select actions
  shuffle: false,
  queue: [], // user-queued track objects, consumed before autoplay/similar
};

// Set while the theme stage + player bar are relocated into a floating
// Document Picture-in-Picture window (see popOutPlayer() below) -- every
// el() lookup falls back to searching that window's document too, since
// document.getElementById can no longer see nodes once they've moved to a
// different document.
let pipWindow = null;
const el = (id) => document.getElementById(id) || (pipWindow && pipWindow.document.getElementById(id));

// Tiles (topbar action buttons) keep a fixed icon + a label span whose text
// changes for status ("Scanning…", "Done ✓", etc.) -- this updates just the
// label so the icon never gets clobbered by a plain .textContent write.
function tileLabel(btn) {
  return btn.querySelector(".tile-label") || btn;
}
function getTileText(btn) {
  return tileLabel(btn).textContent;
}
function setTileText(btn, text) {
  tileLabel(btn).textContent = text;
}

// ---------------------------------------------------------------- fetch --
async function api(path, opts) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    // The server answers an unexpected failure with {"error": <readable
    // sentence>, "kind": ...} (see app.py's error handler) -- surface that
    // sentence instead of a bare "/path -> 500".
    let message = `${path} -> ${res.status}`;
    let kind = null;
    try {
      const body = await res.clone().json();
      if (body && body.error) { message = body.error; kind = body.kind || null; }
    } catch (e) { /* not JSON (e.g. a plain 404) -- keep the generic message */ }
    const err = new Error(message);
    err.status = res.status;
    err.kind = kind;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

// Shared by every background-job progress poll (duplicate cleanup, genre/year
// fill, artist genre unify): repeatedly GETs `path` and hands the result to
// `onTick`, which returns true to keep polling or false once the job's done.
// A poll that just called api() directly and looped itself would die
// silently on any transient failure (e.g. the server restarting mid-job) --
// api() throws, nothing catches it since poll() is fire-and-forget, and the
// loop simply stops rescheduling, leaving the UI frozen on stale text with
// no sign anything went wrong. This tolerates a few consecutive failures
// (worth a brief server hiccup) before surfacing a real error to the caller.
// Shared visual progress bar pinned to the topbar's bottom edge (see
// .scan-progress in style.css) -- used by both the choose-folder and
// rescan flows below, since they're the same underlying scan job.
function showScanProgress(done, total) {
  el("scan-progress").classList.remove("hidden");
  el("scan-progress-fill").style.width = total ? `${Math.min(100, (done / total) * 100)}%` : "3%";
}
function hideScanProgress() {
  el("scan-progress").classList.add("hidden");
  el("scan-progress-fill").style.width = "0%";
}

async function pollProgress(path, onTick, { intervalMs = 1000, maxFailures = 5 } = {}) {
  let failures = 0;
  for (;;) {
    let status;
    try {
      status = await api(path);
      failures = 0;
    } catch (e) {
      failures++;
      if (failures >= maxFailures) {
        throw new Error(`Lost connection to the server (${e.message}).`);
      }
      await new Promise((r) => setTimeout(r, intervalMs));
      continue;
    }
    if (!onTick(status)) return status;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

// -------------------------------------------------------------- facets --
async function loadFacets() {
  const data = await api("/facets");
  const genreSel = el("filter-genre");
  const decadeSelPrev = el("filter-decade");
  const languageSelPrev = el("filter-language");
  const artistListPrev = el("artist-list");
  genreSel.innerHTML = "";
  decadeSelPrev.innerHTML = "";
  [...languageSelPrev.querySelectorAll("option:not(:first-child)")].forEach((o) => o.remove());
  artistListPrev.innerHTML = "";
  data.genres.forEach((g) => {
    const opt = document.createElement("option");
    opt.value = g; opt.textContent = g;
    genreSel.appendChild(opt);
  });
  const decadeSel = el("filter-decade");
  data.decades.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d; opt.textContent = `${d}s`;
    decadeSel.appendChild(opt);
  });
  const smartGenreSel = el("smart-genre");
  const smartDecadeSel = el("smart-decade");
  if (smartGenreSel) {
    [...smartGenreSel.querySelectorAll("option:not(:first-child)")].forEach((o) => o.remove());
    data.genres.forEach((g) => {
      const opt = document.createElement("option");
      opt.value = g; opt.textContent = g;
      smartGenreSel.appendChild(opt);
    });
  }
  if (smartDecadeSel) {
    [...smartDecadeSel.querySelectorAll("option:not(:first-child)")].forEach((o) => o.remove());
    data.decades.forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d; opt.textContent = `${d}s`;
      smartDecadeSel.appendChild(opt);
    });
  }
  const languageSel = el("filter-language");
  (data.languages || []).forEach((lang) => {
    const opt = document.createElement("option");
    opt.value = lang; opt.textContent = lang;
    languageSel.appendChild(opt);
  });
  const artistList = el("artist-list");
  data.artists.forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a;
    artistList.appendChild(opt);
  });
  state.libraryTotal = data.total;
  el("stats").textContent = `${data.total.toLocaleString()} tracks`;
}

// The topbar count badge (#stats) otherwise always showed this raw library
// total (set once above, from /facets) even while a search or filter was
// narrowing the visible list down to a handful of rows (JB-005) -- called
// from loadTracks() below, which is the one place that actually knows the
// current filtered count.
function updateStatsBadge() {
  const filtered = !!(state.q || countActiveFilters());
  el("stats").textContent = filtered && state.libraryTotal != null
    ? `${state.total.toLocaleString()} of ${state.libraryTotal.toLocaleString()} tracks`
    : `${state.total.toLocaleString()} tracks`;
}

// -------------------------------------------------------------- tracks --
function trackQueryParams(extra = {}) {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  if (state.genre) p.set("genre", state.genre);
  if (state.decade) p.set("decade", state.decade);
  if (state.language) p.set("language", state.language);
  if (state.artist) p.set("artist", state.artist);
  if (state.ratedOnly) p.set("rated_only", "1");
  if (state.rating) p.set("rating", state.rating);
  p.set("sort", state.sort);
  p.set("limit", state.pageSize);
  p.set("offset", state.offset);
  Object.entries(extra).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

async function loadTracks(reset = false) {
  if (reset) {
    state.offset = 0;
    state.hasMore = true;
    state.libraryRows = [];
    state.currentList = state.libraryRows;
    resetLibraryWindow();
    clearSelection();
  }
  if (!state.hasMore || state.loadingMore) return;
  state.loadingMore = true;
  el("load-sentinel").textContent = state.offset > 0 ? "Loading more…" : "";
  try {
    const data = await api(`/tracks?${trackQueryParams()}`);
    state.total = data.total;
    updateStatsBadge();
    // Mutated in place (not concat'd into a new array): state.currentList is
    // the same array, which is what next/previous walk -- so a page that
    // loads while a track is playing extends the list playback sees too.
    state.libraryRows.push(...data.tracks);
    state.currentList = state.libraryRows;
    renderLibraryWindow();
    state.offset += data.tracks.length;
    state.hasMore = state.offset < state.total;
    if (state.total === 0 && state.offset === 0) {
      // A search/filter combo that matches nothing is a state real users
      // hit often (a typo, an over-narrow filter) -- bare centered text
      // in an otherwise blank canvas read as broken/unfinished rather
      // than "nothing matched", with no way forward from the message
      // itself. A search or filter is active whenever this fires (an
      // actually-empty library fails the choose-folder flow before ever
      // reaching this screen), so the suggestion is always relevant.
      resetLibraryWindow();
      el("track-list").innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">🔍</div>
          <div class="empty-state-title">No tracks found</div>
          <div class="empty-state-hint">Nothing matches your current search and filters.</div>
          <button id="empty-state-clear" class="btn-small">Clear search &amp; filters</button>
        </div>`;
      el("empty-state-clear").addEventListener("click", clearAllFilters);
      el("load-sentinel").textContent = "";
    } else {
      el("load-sentinel").textContent = state.hasMore
        ? ""
        : (state.total ? `${state.total.toLocaleString()} tracks — end of list` : "");
    }
    el("select-all-filtered").title = `Select every track matching the current filters (${state.total.toLocaleString()})`;
    el("select-all-filtered").disabled = state.total === 0;
  } finally {
    state.loadingMore = false;
  }
}

el("main-scroll").addEventListener("scroll", () => {
  if (el("library-view").classList.contains("hidden")) return;
  scheduleLibraryWindow();
  const c = el("main-scroll");
  if (c.scrollTop + c.clientHeight > c.scrollHeight - 600) {
    loadTracks(false);
  }
});

// Coming back to the library from a playlist (any of the places that
// un-hide it): re-fit the window to wherever the scroller is now.
new MutationObserver(() => {
  if (!el("library-view").classList.contains("hidden")) scheduleLibraryWindow();
}).observe(el("library-view"), { attributes: true, attributeFilter: ["class"] });

function starString(rating) {
  if (!rating) return "";
  return "★".repeat(rating) + "☆".repeat(5 - rating);
}

function rowStarsHtml(rating) {
  return [1, 2, 3, 4, 5].map((n) =>
    `<span data-star="${n}" class="${n <= rating ? "filled" : ""}">★</span>`
  ).join("");
}

// Shared by the inline row stars and (via a thin wrapper) the footer's own
// #stars -- click the currently-set star again to clear the rating, same
// convention either way. Updates the row/footer in place rather than
// reloading the list, so rating tracks while scrolling doesn't reset
// scroll position; a track that no longer matches an active rating filter
// just fades out of the current view instead.
async function rateTrack(t, rating, starsBox) {
  if (t.rating === rating) rating = 0;
  await api(`/rate/${t.id}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating }),
  });
  t.rating = rating;
  if (starsBox) {
    starsBox.querySelectorAll("span").forEach((s) => s.classList.toggle("filled", Number(s.dataset.star) <= rating));
  }
  if (state.currentTrack && state.currentTrack.id === t.id) {
    state.currentTrack.rating = rating;
    updateStars(rating);
  }
  return rating;
}

function artUrl(trackId, thumb) {
  return `${API}/art/${trackId}${thumb ? "?thumb=1" : ""}`;
}

// One track row. `contextList` is what clicking it plays through (next/
// previous walk it) -- the whole loaded library for the main list, the page
// for a playlist view.
function buildTrackRow(t, displayIdx, { showAdd, contextList }) {
  const row = document.createElement("div");
  row.className = "track-row";
  row.dataset.id = t.id;
  if (state.currentTrack && state.currentTrack.id === t.id) row.classList.add("playing");
  if (state.selected.has(t.id)) row.classList.add("selected");
  row.innerHTML = `
    <div class="col-check"><input type="checkbox" ${state.selected.has(t.id) ? "checked" : ""}></div>
    <div class="col-art">${t.has_art === 0 ? "" : `<img loading="lazy" src="${artUrl(t.id, true)}" alt="" onerror="this.remove()">`}</div>
    <div class="col-idx">${displayIdx}</div>
    <div class="col-title" title="${escapeHtml(t.title || "")}">${escapeHtml(t.title || "")}</div>
    <div class="col-artist" title="${escapeHtml(t.artist || "")}">${escapeHtml(t.artist || "")}</div>
    <div class="col-genre" title="${escapeHtml(t.primary_genre || "")}">${escapeHtml(t.primary_genre || "")}</div>
    <div class="col-year">${t.year || ""}</div>
    <div class="col-rating row-stars">${rowStarsHtml(t.rating || 0)}</div>
    <div class="col-queue" title="Play next">▸</div>
    <div class="col-add" title="Add to playlist">${showAdd ? "+" : ""}</div>
    <div class="col-lookup" title="Look up this track's genre, year, artist &amp; title on Deezer">🔍</div>
  `;
  row.addEventListener("click", (e) => {
    if (e.target.closest(".col-add") || e.target.closest(".col-check") || e.target.closest(".col-queue") || e.target.closest(".row-stars") || e.target.closest(".col-lookup")) return;
    playTrack(t, contextList);
  });
  row.querySelector(".col-check input").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleSelection(t.id, row);
  });
  row.querySelector(".col-queue").addEventListener("click", (e) => {
    e.stopPropagation();
    queueTrackNext(t, e.currentTarget);
  });
  const rowStars = row.querySelector(".row-stars");
  rowStars.addEventListener("click", (e) => {
    e.stopPropagation();
    const starEl = e.target.closest("span[data-star]");
    if (!starEl) return;
    rateTrack(t, Number(starEl.dataset.star), rowStars).then((rating) => {
      const violatesFilter = (state.ratedOnly && rating === 0) || (state.rating && Number(state.rating) !== rating);
      if (violatesFilter) {
        row.style.transition = "opacity .2s ease";
        row.style.opacity = "0";
        setTimeout(() => removeTrackRow(t.id, row), 200);
      }
    });
  });
  rowStars.addEventListener("mousemove", (e) => {
    const starEl = e.target.closest("span[data-star]");
    if (!starEl) return;
    const hoverRating = Number(starEl.dataset.star);
    rowStars.querySelectorAll("span").forEach((s) => s.classList.toggle("filled", Number(s.dataset.star) <= hoverRating));
  });
  rowStars.addEventListener("mouseleave", () => {
    rowStars.querySelectorAll("span").forEach((s) => s.classList.toggle("filled", Number(s.dataset.star) <= (t.rating || 0)));
  });
  if (showAdd) {
    row.querySelector(".col-add").addEventListener("click", (e) => {
      e.stopPropagation();
      openAddToPlaylistModal([t.id]);
    });
  }
  row.querySelector(".col-lookup").addEventListener("click", (e) => {
    e.stopPropagation();
    lookupTrackTags(t, row, e.currentTarget);
  });
  row.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    showTrackContextMenu(e, t, row);
  });
  return row;
}

// Playlist views (and anything else that isn't the main library list) just
// render every row they're given. The main library list is windowed -- see
// renderLibraryWindow below.
function renderTrackList(container, list, { showAdd, append } = {}) {
  if (!append) container.innerHTML = "";
  const baseIdx = append ? container.children.length : 0;
  list.forEach((t, i) => {
    container.appendChild(buildTrackRow(t, baseIdx + i + 1, { showAdd, contextList: list }));
  });
}

// ----------------------------------------------- windowed library list --
// A big library used to put every row it had ever loaded into the page --
// scroll through 21,000 tracks and that's 21,000 rows (a dozen elements and
// an image request each). Now only the rows near the viewport exist; the
// space the rest would take is padding on the list element, so scrollbars,
// scroll position and "load more" all behave exactly as before. Every row
// has one fixed height (set in CSS) which is what makes the arithmetic
// below exact; it's measured rather than hard-coded, so a layout that
// changes it only needs to call invalidateRowHeight().
const vwin = { rowH: 0, first: 0, last: -1, nodes: [], raf: 0, overscan: 12 };

function libraryHost() { return el("track-list"); }

function resetLibraryWindow() {
  const host = libraryHost();
  host.innerHTML = "";
  host.style.paddingTop = "0px";
  host.style.paddingBottom = "0px";
  vwin.first = 0;
  vwin.last = -1;
  vwin.nodes = [];
}

function invalidateRowHeight() {
  vwin.rowH = 0;
  if (state.libraryRows && state.libraryRows.length) {
    vwin.first = 0;
    vwin.last = -1;
    vwin.nodes = [];
    libraryHost().innerHTML = "";
    renderLibraryWindow(true);
  }
}

function libraryRowOptions() {
  return { showAdd: true, contextList: state.libraryRows };
}

function renderLibraryWindow(force = false) {
  const host = libraryHost();
  const rows = state.libraryRows;
  if (!rows || !rows.length || el("library-view").classList.contains("hidden")) return;

  if (!vwin.rowH) {
    // First render (or after a layout change): put one real row in to
    // measure what a row actually is in the current layout/theme.
    host.innerHTML = "";
    const probe = buildTrackRow(rows[0], 1, libraryRowOptions());
    host.appendChild(probe);
    vwin.rowH = probe.getBoundingClientRect().height || 44;
    host.innerHTML = "";
    vwin.first = 0;
    vwin.last = -1;
    vwin.nodes = [];
  }

  const scroller = el("main-scroll");
  const listTop = host.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
  const rowH = vwin.rowH;
  let from = Math.floor((scroller.scrollTop - listTop) / rowH) - vwin.overscan;
  let to = Math.ceil((scroller.scrollTop + scroller.clientHeight - listTop) / rowH) + vwin.overscan;
  from = Math.max(0, Math.min(from, rows.length - 1));
  to = Math.max(from, Math.min(to, rows.length - 1));
  if (!force && from === vwin.first && to === vwin.last && vwin.nodes.length === to - from + 1) return;

  const build = (i) => buildTrackRow(rows[i], i + 1, libraryRowOptions());
  const overlaps = vwin.nodes.length && !force && from <= vwin.last && to >= vwin.first;
  if (!overlaps) {
    host.innerHTML = "";
    const frag = document.createDocumentFragment();
    vwin.nodes = [];
    for (let i = from; i <= to; i++) {
      const node = build(i);
      vwin.nodes.push(node);
      frag.appendChild(node);
    }
    host.appendChild(frag);
  } else {
    while (vwin.first < from && vwin.nodes.length) { vwin.nodes.shift().remove(); vwin.first++; }
    while (vwin.last > to && vwin.nodes.length) { vwin.nodes.pop().remove(); vwin.last--; }
    for (let i = vwin.first - 1; i >= from; i--) {
      const node = build(i);
      vwin.nodes.unshift(node);
      host.insertBefore(node, host.firstChild);
    }
    for (let i = vwin.last + 1; i <= to; i++) {
      const node = build(i);
      vwin.nodes.push(node);
      host.appendChild(node);
    }
  }
  vwin.first = from;
  vwin.last = to;
  host.style.paddingTop = `${from * rowH}px`;
  host.style.paddingBottom = `${(rows.length - 1 - to) * rowH}px`;
}

function scheduleLibraryWindow() {
  if (vwin.raf) return;
  vwin.raf = requestAnimationFrame(() => {
    vwin.raf = 0;
    renderLibraryWindow();
  });
}
window.addEventListener("resize", scheduleLibraryWindow);

// Brings a track in the main list into view even if its row doesn't exist
// in the page right now (it's windowed away).
function scrollLibraryToTrack(trackId, { center = true } = {}) {
  const idx = state.libraryRows.findIndex((t) => t.id === trackId);
  if (idx < 0 || !vwin.rowH) return false;
  const scroller = el("main-scroll");
  const hostTop = libraryHost().getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop;
  const target = hostTop + idx * vwin.rowH - (center ? scroller.clientHeight / 2 : 0);
  scroller.scrollTop = Math.max(0, target);
  renderLibraryWindow();
  return true;
}

// A row removed from view (e.g. it no longer matches the rating filter after
// being re-rated) has to leave the data too, or windowing would just bring
// it back the next time it scrolls into range.
function removeTrackRow(trackId, row) {
  if (row.parentElement === libraryHost()) {
    const i = state.libraryRows.findIndex((t) => t.id === trackId);
    if (i >= 0) {
      state.libraryRows.splice(i, 1);
      state.total = Math.max(0, state.total - 1);
      state.offset = Math.max(0, state.offset - 1);
      updateStatsBadge();
      renderLibraryWindow(true);
      return;
    }
  }
  row.remove();
}

// -------------------------------------------------------- context menu --
// Right-clicking a row that's already part of a multi-selection acts on the
// whole selection (matching Finder/iTunes); right-clicking anything else
// collapses the selection down to just that one row first -- same
// convention, and it keeps the bulk-action items' effect visibly in sync
// with what's highlighted.
function showTrackContextMenu(event, track, row) {
  if (!(state.selected.has(track.id) && state.selected.size > 1)) {
    clearSelection();
    toggleSelection(track.id, row);
  }
  const menu = el("track-context-menu");
  const selectedIds = Array.from(state.selected);

  el("ctx-play").onclick = () => { playTrack(track, state.currentList); hideTrackContextMenu(); };
  el("ctx-queue-next").onclick = () => { queueTrackNext(track, null); hideTrackContextMenu(); };
  el("ctx-add-playlist").onclick = () => { openAddToPlaylistModal(selectedIds); hideTrackContextMenu(); };
  el("ctx-edit-tags").onclick = () => { el("selection-edit-tags").click(); hideTrackContextMenu(); };
  el("ctx-convert").onclick = () => { el("selection-convert").click(); hideTrackContextMenu(); };
  el("ctx-fetch-art").onclick = () => {
    hideTrackContextMenu();
    if (selectedIds.length > 1) {
      fetchArtForSelection(selectedIds);
    } else {
      fetchArtForTrack(track.id).catch(() => showToast("Couldn't find cover art for this track.", { kind: "error" }));
    }
  };
  el("ctx-reveal").onclick = () => {
    hideTrackContextMenu();
    api(`/tracks/${track.id}/reveal`, { method: "POST" }).then((r) => {
      if (!r.ok) showToast(r.error || "Couldn't show that file.", { kind: "error" });
    });
  };
  el("ctx-delete").onclick = async () => {
    hideTrackContextMenu();
    const n = selectedIds.length;
    const ok = await customConfirm(
      n > 1 ? `Move ${n} tracks to Trash?` : "Move this track to Trash?",
      { okLabel: "Delete", danger: true }
    );
    if (!ok) return;
    const status = await deleteTracksWithProgress(selectedIds);
    if (status && !status.error) {
      clearSelection();
      await loadFacets();
      await loadTracks(true);
    }
  };

  menu.classList.remove("hidden");
  const maxLeft = window.innerWidth - menu.offsetWidth - 8;
  const maxTop = window.innerHeight - menu.offsetHeight - 8;
  menu.style.left = `${Math.max(8, Math.min(event.clientX, maxLeft))}px`;
  menu.style.top = `${Math.max(8, Math.min(event.clientY, maxTop))}px`;
}

function hideTrackContextMenu() {
  el("track-context-menu").classList.add("hidden");
}

document.addEventListener("click", (e) => {
  if (!e.target.closest("#track-context-menu")) hideTrackContextMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideTrackContextMenu();
});
el("main-scroll").addEventListener("scroll", hideTrackContextMenu);

// Per-track equivalent of the header Tag button's bulk tools -- runs the
// same Deezer-backed genre/year/artist-title lookups against just this one
// track and refreshes the row in place, without needing a full library
// scan for a single file someone's looking at right now.
async function lookupTrackTags(t, row, btn) {
  const original = btn.textContent;
  btn.textContent = "…";
  try {
    const result = await api(`/tracks/${t.id}/lookup-tags`, { method: "POST" });
    if (result.errors && result.errors.length) {
      showToast(`Some lookups failed: ${result.errors.join(", ")}`, { kind: "error" });
    }
    Object.assign(t, result.track);
    if (state.currentTrack && state.currentTrack.id === t.id) {
      Object.assign(state.currentTrack, result.track);
    }
    const genreCell = row.querySelector(".col-genre");
    const yearCell = row.querySelector(".col-year");
    const titleCell = row.querySelector(".col-title");
    const artistCell = row.querySelector(".col-artist");
    if (genreCell) genreCell.textContent = t.primary_genre || "";
    if (yearCell) yearCell.textContent = t.year || "";
    if (titleCell) titleCell.textContent = t.title || "";
    if (artistCell) artistCell.textContent = t.artist || "";
    btn.textContent = result.changed.length ? "✓" : "–";
  } catch (e) {
    btn.textContent = "!";
  } finally {
    setTimeout(() => { btn.textContent = original; }, 1200);
  }
}

// ------------------------------------------------------------ selection --
function toggleSelection(trackId, row) {
  if (state.selected.has(trackId)) {
    state.selected.delete(trackId);
    row.classList.remove("selected");
  } else {
    state.selected.add(trackId);
    row.classList.add("selected");
  }
  updateSelectionToolbar();
}

function clearSelection() {
  state.selected.clear();
  document.querySelectorAll(".track-row.selected").forEach((row) => {
    row.classList.remove("selected");
    const cb = row.querySelector(".col-check input");
    if (cb) cb.checked = false;
  });
  updateSelectionToolbar();
}

function updateSelectionToolbar() {
  const bar = el("selection-toolbar");
  const n = state.selected.size;
  if (n === 0) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  el("selection-count").textContent = `${n} selected`;
}

el("select-all-filtered").addEventListener("click", async () => {
  const btn = el("select-all-filtered");
  const original = getTileText(btn);
  setTileText(btn, "Selecting…");
  btn.disabled = true;
  try {
    const data = await api(`/track-ids?${trackQueryParams()}`);
    data.ids.forEach((id) => state.selected.add(id));
    renderLibraryWindow(true);  // rows are built from state.selected, so a re-render picks up every newly selected one
    updateSelectionToolbar();
  } finally {
    setTileText(btn, original);
    btn.disabled = false;
  }
});

el("selection-clear").addEventListener("click", clearSelection);
el("selection-add-to-playlist").addEventListener("click", () => {
  if (state.selected.size === 0) return;
  openAddToPlaylistModal([...state.selected]);
});
el("selection-convert").addEventListener("click", () => {
  if (state.selected.size === 0) return;
  openFormatPickerModal("Convert selected tracks", (fmt) => ({
    endpoint: "/convert-tracks",
    body: { track_ids: [...state.selected], format: fmt },
    resultTextFn: (r) => { clearSelection(); return `${r.converted}/${r.total} converted`; },
  }));
});

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// --------------------------------------------------------------- player --
const audio = el("audio");

// A second, muted, never-heard <audio> element that exists purely to feed
// the Web Audio analyser for the Hi-Fi VU meters / Cassette spectrum bars
// (see ensureAudioGraph below) -- kept entirely separate from `audio`
// itself, the one AirPlay actually attaches to. Calling
// audioCtx.createMediaElementSource() on an element permanently redirects
// *that element's* output into the Web Audio graph for the rest of its
// life, with no way to undo it -- do that to `audio` directly (as this
// used to) and AirPlay glitches or drops out the moment Web Audio claims
// it, and once claimed there's no getting it back even after switching
// away from Hi-Fi/Cassette. Tapping this mirror instead means `audio`
// itself is never touched by Web Audio at all, so AirPlay stays exactly as
// native-clean as if the meters didn't exist.
const vuAudio = new Audio();
// NOT muted, deliberately -- see ensureAudioGraph's comment for why:
// Chromium-family engines (WebView2 on Windows, confirmed; regular Chrome
// likely too) skip real audio decoding/processing for a muted element
// even once it's been tapped by createMediaElementSource, so the
// analyser only ever sees silence (JB-012). Actual silence comes from
// ensureAudioGraph never connecting this graph to audioCtx.destination,
// which doesn't have that problem on any engine tested so far.
vuAudio.muted = false;
vuAudio.preload = "none";
// Mirrors `audio` only when something actually taps its output -- the
// Hi-Fi/Cassette VU meters, via ensureAudioGraph's createMediaElementSource
// (see there for why vuAudio has to be unmuted to work at all). Every other
// theme, and radio under ANY theme (which reads levels from the server
// instead -- see vuLoop), never calls ensureAudioGraph, so this element is
// never claimed by Web Audio and never silenced by it. Starting it anyway
// in that case would just be a second, independently-buffered copy of the
// exact same audio playing straight out the speakers alongside the real
// one -- two live-stream connections in particular drift out of sync
// within seconds, which is what "the radio sounds like echo" turned out to
// be (the same double-playback affects any theme other than Hi-Fi/
// Cassette, just less audibly for a fully-buffered local file).
// Called from syncThemeVisuals(), which already runs on every play/pause
// and theme change -- so switching themes mid-playback starts or stops
// this mirror exactly when the VU meters start or stop needing it.
function syncVuAudioMirror() {
  const isRadio = !!(state.currentTrack && state.currentTrack.isRadio);
  const needed = (state.theme === "hifi" || state.theme === "cassette") && !isRadio && !audio.paused && audio.src;
  if (!needed) {
    if (!vuAudio.paused) vuAudio.pause();
    return;
  }
  if (vuAudio.src !== audio.currentSrc) vuAudio.src = audio.currentSrc;
  // Only meaningful for a finite, seekable file -- a live radio stream
  // reports duration=Infinity and isn't seekable at all, so this would be
  // asking vuAudio to seek to an arbitrary, unreachable position.
  if (isFinite(audio.duration)) vuAudio.currentTime = audio.currentTime;
  vuAudio.play().catch(() => {});
}
audio.addEventListener("seeked", () => {
  if (isFinite(audio.duration)) vuAudio.currentTime = audio.currentTime;
});

function playTrack(track, contextList, opts) {
  stopRadioNowPlayingPolling();
  stopRadioLevelsPolling();
  const addHistory = !opts || opts.addHistory !== false;
  state.currentTrack = track;
  if (contextList) state.currentList = contextList;
  state.history.push(track.id);
  if (state.history.length > 50) state.history.shift();
  if (addHistory) {
    state.playHistory = state.playHistory.slice(0, state.historyPos + 1);
    state.playHistory.push(track);
    state.historyPos = state.playHistory.length - 1;
  }

  audio.src = `${API}/stream/${track.id}`;
  audio.play();
  el("np-title").textContent = track.title || "Untitled";
  el("np-sub").textContent = [track.artist, track.primary_genre, track.year].filter(Boolean).join(" · ");
  const artEl = el("np-art");
  artEl.classList.remove("hidden");
  artEl.onerror = () => artEl.classList.add("hidden");
  artEl.src = artUrl(track.id, true);
  const artFetchBtn = el("art-fetch-btn");
  if (artFetchBtn) artFetchBtn.classList.remove("hidden");
  el("radio-identify-btn").classList.add("hidden");
  updateStars(track.rating || 0);
  refreshPlayingHighlight();
  themeOnTrackChange(track);
  updateMediaSession(track, artUrl(track.id));
  api(`/plays/${track.id}`, { method: "POST" }).catch(() => {});
}

function refreshPlayingHighlight() {
  document.querySelectorAll(".track-row.playing").forEach((row) => row.classList.remove("playing"));
  if (!state.currentTrack) return;
  // By id, not by position: the main list is windowed, so a row's place in
  // the DOM no longer matches its place in the data.
  document.querySelectorAll(`.track-row[data-id="${state.currentTrack.id}"]`).forEach((row) => row.classList.add("playing"));
}

function togglePlayPause() {
  if (!audio.src) return;
  if (audio.paused) { audio.play(); } else { audio.pause(); }
}
el("play-pause").addEventListener("click", togglePlayPause);
el("hifi-play-pause").addEventListener("click", togglePlayPause);
el("cassette-play-pause").addEventListener("click", togglePlayPause);
el("vinyl-play-pause").addEventListener("click", togglePlayPause);
audio.addEventListener("play", () => {
  el("play-pause").textContent = "⏸";
  el("hifi-play-pause").textContent = "⏸";
  el("cassette-play-pause").textContent = "⏸";
  el("vinyl-play-pause").textContent = "⏸";
  syncThemeVisuals();
});
audio.addEventListener("pause", () => {
  el("play-pause").textContent = "▶";
  el("hifi-play-pause").textContent = "▶";
  el("cassette-play-pause").textContent = "▶";
  el("vinyl-play-pause").textContent = "▶";
  syncThemeVisuals();
});
// Without this, a failed /stream request (a moved/unreadable file, or -- as
// seen with an external drive the OS hadn't granted this app permission to
// read -- a 500) leaves the player just sitting there looking like it's
// still loading forever, with nothing telling you it already gave up.
audio.addEventListener("error", () => {
  if (!state.currentTrack || !audio.error) return;
  const reasons = {
    1: "playback was aborted",
    2: "a network error interrupted the download",
    3: "the file couldn't be decoded",
    4: "the file couldn't be loaded -- check the music folder is connected and this app has permission to read it",
  };
  const reason = reasons[audio.error.code] || "an unknown error occurred";
  showToast(`Couldn't play "${state.currentTrack.title || "this track"}" — ${reason}.`, { kind: "error" });
  // A file that won't load is very often a disconnected drive or a revoked
  // permission -- ask the server, which puts the real cause in the banner.
  if (typeof checkSystemStatus === "function") checkSystemStatus();
});

function playPrevious() {
  if (state.historyPos <= 0) return;
  state.historyPos--;
  playTrack(state.playHistory[state.historyPos], null, { addHistory: false });
}
el("hifi-prev").addEventListener("click", playPrevious);
el("hifi-next").addEventListener("click", playNextSimilar);
el("cassette-prev").addEventListener("click", playPrevious);
el("cassette-next").addEventListener("click", playNextSimilar);
el("vinyl-prev").addEventListener("click", playPrevious);
el("vinyl-next").addEventListener("click", playNextSimilar);

audio.addEventListener("timeupdate", () => {
  if (!isFinite(audio.duration)) return;
  const pct = (audio.currentTime / audio.duration) * 100;
  el("seek").value = pct;
  el("seek").style.setProperty("--fill", `${pct}%`);
  el("hifi-seek").value = pct;
  el("hifi-seek").style.setProperty("--fill", `${pct}%`);
  el("cassette-seek").value = pct;
  el("cassette-seek").style.setProperty("--fill", `${pct}%`);
  el("vinyl-seek").value = pct;
  el("vinyl-seek").style.setProperty("--fill", `${pct}%`);
  el("time-display").textContent = `${fmtTime(audio.currentTime)} / ${fmtTime(audio.duration)}`;
  themeOnTimeUpdate();
});
function seekTo(pct) {
  if (!isFinite(audio.duration)) return;
  el("seek").value = pct;
  el("seek").style.setProperty("--fill", `${pct}%`);
  el("hifi-seek").value = pct;
  el("hifi-seek").style.setProperty("--fill", `${pct}%`);
  el("cassette-seek").value = pct;
  el("cassette-seek").style.setProperty("--fill", `${pct}%`);
  el("vinyl-seek").value = pct;
  el("vinyl-seek").style.setProperty("--fill", `${pct}%`);
  audio.currentTime = (pct / 100) * audio.duration;
}
el("seek").addEventListener("input", () => seekTo(el("seek").value));
el("hifi-seek").addEventListener("input", () => seekTo(el("hifi-seek").value));
el("cassette-seek").addEventListener("input", () => seekTo(el("cassette-seek").value));
el("vinyl-seek").addEventListener("input", () => seekTo(el("vinyl-seek").value));

function fmtTime(s) {
  if (!isFinite(s)) return "0:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60).toString().padStart(2, "0");
  return `${m}:${sec}`;
}

// playNext() already knows how to fall through queue -> shuffle -> the next
// track in whatever list is playing -> (only at the true end of that list)
// similarity-based autoplay if that's checked -- so a track finishing
// naturally should always hand off to it, the same as clicking Next
// manually, not just when shuffle/queue/autoplay-similar happen to be on.
audio.addEventListener("ended", () => {
  playNext();
});
el("autoplay-similar").addEventListener("change", (e) => { state.autoplaySimilar = e.target.checked; });

// AirPlay only exists as a WebKit API (Safari) -- there's no standard web
// API for it. The button stays hidden everywhere else, and even in Safari
// only appears once the OS reports an AirPlay device is actually nearby,
// matching how Safari's own AirPlay icon behaves.
if (typeof audio.webkitShowPlaybackTargetPicker === "function") {
  audio.addEventListener("webkitplaybacktargetavailabilitychanged", (e) => {
    el("airplay-btn").classList.toggle("hidden", e.availability !== "available");
  });
  el("airplay-btn").addEventListener("click", () => {
    audio.webkitShowPlaybackTargetPicker();
  });
}

async function playNextSimilar() {
  if (state.currentTrack && state.currentTrack.isRadio) return;
  const currentId = state.currentTrack ? state.currentTrack.id : "";
  const exclude = state.history.join(",");
  const data = await api(`/next?current=${currentId}&exclude=${exclude}`);
  if (data.track) {
    playTrack(data.track);
  }
}

function countActiveFilters() {
  let n = 0;
  if (state.genre) n++;
  if (state.decade) n++;
  if (state.language) n++;
  if (state.artist) n++;
  if (state.ratedOnly) n++;
  if (state.rating) n++;
  return n;
}

// The search bar doubles as an action launcher -- typing "/" plus a command
// name and Enter runs the same button click a tile would. Suggestions show
// as soon as "/" is typed so the commands are discoverable rather than
// something you have to already know.
const SEARCH_COMMANDS = [
  { cmd: "rescan", label: "Rescan library", btn: "rescan-library", group: "Library" },
  { cmd: "tag", label: "Tag checker", btn: "open-tag-checker", group: "Library" },
  { cmd: "folder", label: "Choose folder", btn: "choose-folder", group: "Library" },
  { cmd: "duplicates", label: "Find duplicates", btn: "find-duplicates", group: "Library" },
  { cmd: "organize", label: "Organize by artist", btn: "organize-by-artist", group: "Library" },
  { cmd: "playlist", label: "Add to playlist", btn: "playlist-menu", group: "Library" },
  { cmd: "convert", label: "Convert format", btn: "convert-menu", group: "Library" },
  { cmd: "select", label: "Select all filtered", btn: "select-all-filtered", group: "Library" },
  { cmd: "ipod", label: "Import from iPod", btn: "import-ipod", group: "Import" },
  { cmd: "radio", label: "Similar-tracks radio", btn: "radio-mode", group: "Listen" },
  { cmd: "liveradio", label: "Live radio stations", btn: "open-internet-radio", group: "Listen" },
  { cmd: "trash", label: "Trash", btn: "open-trash", group: "System" },
  { cmd: "backup", label: "Backup library", btn: "open-export", group: "System" },
  { cmd: "stats", label: "Library stats", btn: "open-stats", group: "System" },
  { cmd: "health", label: "Library health check", btn: "open-health", group: "System" },
  { cmd: "history", label: "Change history / undo", btn: "open-history", group: "System" },
];

function runSearchCommand(command) {
  el("search").value = "";
  hideSearchSuggestions();
  el(command.btn).click();
}

function hideSearchSuggestions() {
  el("search-suggestions").classList.add("hidden");
  el("search-suggestions").innerHTML = "";
}

function showSearchSuggestions(fragment) {
  const matches = SEARCH_COMMANDS.filter((c) => c.cmd.startsWith(fragment));
  const box = el("search-suggestions");
  if (!matches.length) { hideSearchSuggestions(); return; }
  box.innerHTML = matches
    .map((c) => `<div class="search-suggestion" data-cmd="${c.cmd}"><span class="cmd">/${c.cmd}</span><span class="lbl">${escapeHtml(c.label)}</span></div>`)
    .join("");
  box.querySelectorAll(".search-suggestion").forEach((row) => {
    row.addEventListener("click", () => {
      const match = SEARCH_COMMANDS.find((c) => c.cmd === row.dataset.cmd);
      if (match) runSearchCommand(match);
    });
  });
  box.classList.remove("hidden");
}

el("search").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const val = el("search").value;
  if (!val.startsWith("/")) return;
  const typed = val.slice(1).trim().toLowerCase();
  const match = SEARCH_COMMANDS.find((c) => c.cmd === typed) || SEARCH_COMMANDS.find((c) => c.cmd.startsWith(typed));
  if (match) {
    e.preventDefault();
    runSearchCommand(match);
  }
});
el("search").addEventListener("blur", () => setTimeout(hideSearchSuggestions, 150));

// Command Bar layout only: a visual, clickable version of the same
// SEARCH_COMMANDS list above, for people who'd rather browse than remember
// a command name to type after "/".
function renderCommandPalette() {
  el("command-palette-list").innerHTML = SEARCH_COMMANDS.map((c) =>
    `<button class="command-palette-item" data-cmd="${c.cmd}"><span>${escapeHtml(c.label)}</span><span class="cmd-group">${escapeHtml(c.group)}</span></button>`
  ).join("");
  el("command-palette-list").querySelectorAll(".command-palette-item").forEach((row) => {
    row.addEventListener("click", () => {
      const match = SEARCH_COMMANDS.find((c) => c.cmd === row.dataset.cmd);
      closeCommandPalette();
      if (match) runSearchCommand(match);
    });
  });
}
function closeCommandPalette() { el("command-palette").classList.add("hidden"); }
el("open-command-palette").addEventListener("click", (e) => {
  e.stopPropagation();
  const palette = el("command-palette");
  const opening = palette.classList.contains("hidden");
  if (opening) renderCommandPalette();
  palette.classList.toggle("hidden");
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("#command-palette") && e.target.id !== "open-command-palette") closeCommandPalette();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeCommandPalette();
});
el("toggle-sidebar-filters").addEventListener("click", () => {
  document.body.classList.toggle("filters-open");
});

// ----------------------------------------------------------- layout modes --
// Toolbar/sidebar/track-list arrangement -- independent of the player theme
// below (which only ever restyles the now-playing stage). See style.css's
// own "LAYOUT MODES" section for what each one actually changes.
const COLLAPSIBLE_FILTER_GROUPS = ["filter-decade", "filter-language", "filter-artist", "filter-rating"];
state.layoutMode = localStorage.getItem("jukebox-layout-mode") || "classic";

function applyLayoutMode(name) {
  state.layoutMode = name;
  try { localStorage.setItem("jukebox-layout-mode", name); } catch (e) { /* private browsing etc -- fine to skip */ }
  api("/layout-mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ layoutMode: name }) }).catch(() => {});
  document.body.dataset.layout = name;
  document.body.classList.remove("filters-open");
  el("layout-select").value = name;
  invalidateRowHeight();  // each layout has its own row height
  if (name === "grouped-ribbon") setUpGroupedRibbonAccordion();
}

// Adds a collapse toggle to each of the less-frequently-used filter groups
// (Genre and Sort stay open -- everything else starts collapsed) the first
// time this layout is picked; harmless to call again on a later switch back
// into it, since it skips groups that already have their toggle.
function setUpGroupedRibbonAccordion() {
  COLLAPSIBLE_FILTER_GROUPS.forEach((inputId) => {
    const group = el(inputId).closest(".filter-group");
    if (!group || group.querySelector(".fgroup-toggle")) return;
    const label = group.querySelector("label");
    const toggle = document.createElement("button");
    toggle.className = "fgroup-toggle";
    toggle.type = "button";
    toggle.textContent = "▸";
    toggle.addEventListener("click", (e) => {
      e.preventDefault();
      group.classList.toggle("collapsed");
      toggle.textContent = group.classList.contains("collapsed") ? "▸" : "▾";
    });
    if (label) label.appendChild(toggle);
    group.classList.add("collapsed");
    toggle.textContent = "▸";
  });
}

el("layout-select").addEventListener("change", (e) => applyLayoutMode(e.target.value));

// ---------------------------------------------------------- player themes --
const THEME_PANELS = { hifi: "stage-hifi", cassette: "stage-cassette", vinyl: "stage-vinyl" };
state.theme = localStorage.getItem("jukebox-theme") || "default";
state.woodFinish = localStorage.getItem("jukebox-wood-finish") || "walnut";
state.cassetteDesign = localStorage.getItem("jukebox-cassette-design") || "blue";
state.vuColor = localStorage.getItem("jukebox-vu-color") || "amber";

function applyTheme(name) {
  state.theme = name;
  try { localStorage.setItem("jukebox-theme", name); } catch (e) { /* private browsing etc -- fine to skip */ }
  // Also saved server-side (see /api/theme) -- localStorage alone isn't
  // enough for the pywebview desktop app, whose WKWebView doesn't persist
  // it across separate launches.
  api("/theme", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ theme: name }) }).catch(() => {});
  document.body.dataset.theme = name;
  if (pipWindow) pipWindow.document.body.dataset.theme = name;
  el("theme-select").value = name;

  Object.values(THEME_PANELS).forEach((id) => el(id).classList.add("hidden"));
  if (THEME_PANELS[name]) {
    el("theme-stage").classList.remove("hidden");
    el(THEME_PANELS[name]).classList.remove("hidden");
  } else {
    el("theme-stage").classList.add("hidden");
    // Nothing themed left to show -- a still-open focus mode or floating
    // popout would just be showing a blank panel, so back out of both.
    exitFocusMode();
    if (pipWindow) pipWindow.close();
  }
  el("focus-mode-btn").classList.toggle("hidden", !THEME_PANELS[name]);
  el("popout-btn").classList.toggle("hidden", !THEME_PANELS[name] || !("documentPictureInPicture" in window));
  el("wood-select-wrap").classList.toggle("hidden", name !== "vinyl");
  el("cassette-design-select-wrap").classList.toggle("hidden", name !== "cassette");
  el("vu-color-select-wrap").classList.toggle("hidden", name !== "hifi");
  if (state.currentTrack) themeOnTrackChange(state.currentTrack);
  syncThemeVisuals();
  fitStagePanel();
}

el("theme-select").addEventListener("change", () => applyTheme(el("theme-select").value));

// The vinyl theme's turntable wood finish -- a separate, theme-scoped
// choice (see applyTheme above, which shows/hides this select) rather than
// a whole extra theme, since only the housing texture changes.
// Actual solid colors pulled from each option's own theming rule in
// style.css (the wood grain's base flood-color, the cassette band's SVG
// fill, the VU face gradient's accent) -- lets the picker show a real
// preview swatch instead of asking people to choose blind from a name
// like "Rust on Bone" in a native <select>, which can't render per-option
// color chips consistently across the WebKit/WebView2/WebKit2GTK
// backends this app runs in across platforms.
const WOOD_SWATCH_COLORS = { walnut: "#3a2a1c", ebony: "#241c15", mahogany: "#402019" };
const CASSETTE_SWATCH_COLORS = { blue: "#a9d4de", red: "#b5482f", rust: "#9c4a34" };
const VU_SWATCH_COLORS = { amber: "#e8961f", blue: "#3fa9e8", green: "#3ecf6e" };

function applyWoodFinish(name) {
  state.woodFinish = name;
  try { localStorage.setItem("jukebox-wood-finish", name); } catch (e) { /* private browsing etc -- fine to skip */ }
  api("/wood-finish", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ woodFinish: name }) }).catch(() => {});
  document.body.dataset.wood = name;
  if (pipWindow) pipWindow.document.body.dataset.wood = name;
  el("wood-select").value = name;
  el("wood-select-swatch").style.background = WOOD_SWATCH_COLORS[name] || "transparent";
}

el("wood-select").addEventListener("change", () => applyWoodFinish(el("wood-select").value));

// The cassette theme's shell design (color band, label, and shell material
// -- the window, both reels, and the tape-ring playback animation are
// untouched by any of these, see the [data-cassette-design] rules in
// style.css) -- same theme-scoped-choice pattern as the vinyl wood finish.
function applyCassetteDesign(name) {
  state.cassetteDesign = name;
  try { localStorage.setItem("jukebox-cassette-design", name); } catch (e) { /* private browsing etc -- fine to skip */ }
  api("/cassette-design", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cassetteDesign: name }) }).catch(() => {});
  document.body.dataset.cassetteDesign = name;
  if (pipWindow) pipWindow.document.body.dataset.cassetteDesign = name;
  el("cassette-design-select").value = name;
  el("cassette-design-select-swatch").style.background = CASSETTE_SWATCH_COLORS[name] || "transparent";
}

el("cassette-design-select").addEventListener("change", () => applyCassetteDesign(el("cassette-design-select").value));

// The Hi-Fi theme's VU meter backlight color -- also drives every button,
// highlight and glow across the theme (see the --accent/--accent-rgb/
// --vu-face-grad/--vu-glow overrides gated on [data-vu-color] in
// style.css), same theme-scoped-choice pattern as the other two pickers.
function applyVuColor(name) {
  state.vuColor = name;
  try { localStorage.setItem("jukebox-vu-color", name); } catch (e) { /* private browsing etc -- fine to skip */ }
  api("/vu-color", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ vuColor: name }) }).catch(() => {});
  document.body.dataset.vuColor = name;
  if (pipWindow) pipWindow.document.body.dataset.vuColor = name;
  el("vu-color-select").value = name;
  el("vu-color-select-swatch").style.background = VU_SWATCH_COLORS[name] || "transparent";
}

el("vu-color-select").addEventListener("change", () => applyVuColor(el("vu-color-select").value));

// ---------------------------------------------------- focus mode & popout --
// In the normal sidebar, each theme's panel is hand-fitted to its fixed
// width and left alone. In focus mode / popped out it suddenly has much
// more (or, in a small floating window, a differently-shaped) space to work
// with -- fitStagePanel() scales the panel's own intrinsic-size box up or
// down with a transform so it fills that space instead of sitting tiny in
// the middle of it. CSS (see .app.focus-mode / body.popout-body rules)
// switches the panel to its natural content size only in these two modes,
// so this never touches the tuned sidebar layout.
const STAGE_CONTENT_SELECTOR = { hifi: ".hifi-panel", cassette: ".cassette-shell", vinyl: ".turntable" };

// True whenever the stage panel is in the "collapsed then scaled up" layout
// (focus mode or popped out) rather than its normal sidebar layout -- vinyl's
// tonearm math branches on this too, since the disc sits at a genuinely
// different position relative to the tonearm's fixed mount in each.
function isStageScaled() {
  return !!(pipWindow || (el("app-root") && el("app-root").classList.contains("focus-mode")));
}

function fitStagePanel() {
  const stage = el("theme-stage");
  if (!stage) return;
  const sel = STAGE_CONTENT_SELECTOR[state.theme];
  const content = sel ? stage.querySelector(sel) : null;
  if (!content) return;
  if (stage.classList.contains("hidden")) {
    content.style.transform = "";
    return;
  }

  const scaling = isStageScaled();
  content.style.transform = "none";
  const naturalW = content.offsetWidth, naturalH = content.offsetHeight;
  const availW = stage.clientWidth, availH = stage.clientHeight;
  if (!naturalW || !naturalH || !availW || !availH) return;

  if (scaling) {
    content.style.transform = `scale(${Math.min(availW / naturalW, availH / naturalH)})`;
  } else if (state.theme === "vinyl" || state.theme === "cassette") {
    // Normal sidebar layout, vinyl/cassette only: never scale UP (hifi stays
    // exactly as before -- hand-fitted to its normal width, no shrink
    // logic) but DO shrink the panel as one rigid unit if its natural
    // content no longer fits the space actually available. Both panels are
    // wide enough (680px/470px design width) that .theme-stage's own
    // max-width can clamp availW below that natural width on an ordinary
    // laptop-width window -- without this, the excess just gets cropped by
    // theme-stage's overflow-x:hidden instead of shrinking to fit inside it.
    const fitScale = Math.min(1, availW / naturalW, availH / naturalH);
    content.style.transform = fitScale < 1 ? `scale(${fitScale})` : "";
  } else {
    content.style.transform = "";
  }
}

// The panel's own natural (unscaled) width/height -- used to size the
// pywebview focus-mode window to the same aspect ratio, so the min() scale
// above lands on the same factor for both axes and the panel fills the
// window exactly, edge to edge, with no letterbox margins on either side.
function stageContentNaturalSize() {
  const sel = STAGE_CONTENT_SELECTOR[state.theme];
  const stage = el("theme-stage");
  const content = sel ? stage.querySelector(sel) : null;
  if (!content) return null;
  return { width: content.offsetWidth, height: content.offsetHeight };
}

new ResizeObserver(fitStagePanel).observe(el("theme-stage"));
window.addEventListener("resize", fitStagePanel);

// Under the pywebview desktop app (not a plain browser tab), focus mode
// also shrinks the actual OS window down to a small standalone-looking
// player -- window.pywebview.api is pywebview's bridge to desktop_macos.py's
// JsApi, which does the real resize(); a browser tab has no such bridge, so
// this silently no-ops there and focus mode falls back to its CSS-only
// behavior (same panel, same window size).
function callDesktopApi(method, ...args) {
  if (window.pywebview && window.pywebview.api && window.pywebview.api[method]) {
    window.pywebview.api[method](...args).catch(() => {});
  }
}

function enterFocusMode() {
  // Measured before the focus-mode class change below, while the panel is
  // still unscaled in its normal sidebar layout -- this is its true
  // intrinsic size. Resizing the window to match this aspect ratio is what
  // lets fitStagePanel's min()-based scale land on the same factor for
  // both axes, so the panel fills the window exactly instead of leaving
  // letterbox margins on whichever axis doesn't match.
  const naturalSize = stageContentNaturalSize();
  el("app-root").classList.add("focus-mode");
  el("stage-exit-focus").classList.remove("hidden");
  el("focus-mode-btn").classList.add("active");
  callDesktopApi("enter_focus", naturalSize);
  fitStagePanel();
  updateVinylTonearm();
}
function exitFocusMode() {
  el("app-root").classList.remove("focus-mode");
  el("stage-exit-focus").classList.add("hidden");
  el("focus-mode-btn").classList.remove("active");
  callDesktopApi("exit_focus");
  fitStagePanel();
  updateVinylTonearm();
}
el("focus-mode-btn").addEventListener("click", () => {
  if (el("app-root").classList.contains("focus-mode")) exitFocusMode();
  else enterFocusMode();
});
el("stage-exit-focus").addEventListener("click", exitFocusMode);

// Moves the (live, not cloned) theme-stage panel and the transport footer
// into a real floating OS window via the Document Picture-in-Picture API,
// so the retro player keeps running -- including uninterrupted audio --
// while you switch to another tab or app entirely. Chrome/Edge only; the
// popout button stays hidden everywhere else (see applyTheme above).
async function popOutPlayer() {
  if (!("documentPictureInPicture" in window)) return;
  if (pipWindow) { pipWindow.focus(); return; }

  const stage = el("theme-stage");
  const bar = document.querySelector(".player-bar");
  const stageParent = stage.parentElement, stageNext = stage.nextSibling;
  const barParent = bar.parentElement, barNext = bar.nextSibling;

  const win = await documentPictureInPicture.requestWindow({ width: 360, height: 700 });
  pipWindow = win;

  const link = win.document.createElement("link");
  link.rel = "stylesheet";
  link.href = new URL("style.css", location.href).href;
  win.document.head.append(link);
  win.document.title = "Notorious B.P.M.";
  win.document.body.dataset.theme = state.theme;
  win.document.body.dataset.wood = state.woodFinish;
  win.document.body.dataset.cassetteDesign = state.cassetteDesign;
  win.document.body.dataset.vuColor = state.vuColor;
  win.document.body.classList.add("popout-body");

  const wrap = win.document.createElement("div");
  wrap.className = "popout-wrap";
  wrap.append(stage, bar);
  win.document.body.append(wrap);
  stage.classList.remove("hidden");

  exitFocusMode();
  el("popout-btn").classList.add("active");
  fitStagePanel();
  updateVinylTonearm();
  win.addEventListener("resize", fitStagePanel);

  win.addEventListener("pagehide", () => {
    stageParent.insertBefore(stage, stageNext);
    barParent.insertBefore(bar, barNext);
    pipWindow = null;
    el("popout-btn").classList.remove("active");
    applyTheme(state.theme);
    fitStagePanel();
  }, { once: true });
}
el("popout-btn").addEventListener("click", popOutPlayer);

// Tonearm angle mirrors playback position, like a real turntable: parked
// outside the disc when nothing is loaded, then sweeping from the outer
// edge of the grooves inward toward the label as the track progresses.
// The angle is solved from the actual live geometry (see
// updateVinylTonearm below) rather than a couple of hand-picked degree
// constants -- see ARM_SEG1_LEN/ARM_SEG2_LEN/ARM_BEND_DEG and
// angleForTargetRadius further down for how.
const TONEARM_REST_DEG = 6;

// The arm is two rigid segments meeting at a fixed bend (see .tonearm-seg1/
// -seg2 in CSS) -- these mirror those CSS lengths/angle exactly so the JS
// geometry below knows the arm's true shape.
const ARM_SEG1_LEN = 180;
// Not just the segment's own CSS height (115px) -- the headshell overhangs
// past the end of it, so this is the true bend-point-to-stylus-tip
// distance along the segment's own axis (115 + 13px into the headshell,
// per the headshell/stylus offsets in the CSS below). Deliberately short
// enough that the pivot-to-tip length lands close to the pivot-to-center
// distance -- that's what lets rMin (how close the tip can physically get
// to the record's center, below) come out small instead of stranding the
// arm partway across the disc at the end of a track.
const ARM_SEG2_LEN = 128;
const ARM_BEND_DEG = 18;
const ARM_BEND_RAD = ARM_BEND_DEG * Math.PI / 180;
// Where the headshell tip sits, in the arm's own unrotated local space
// (pivot at the origin, "straight down" is +y) -- law of cosines/sines
// against the fixed bend, not the sweep angle, which is applied on top of
// this afterward. Negative X: CSS rotate() is clockwise, and clockwise off
// straight-down swings left (toward -x), which is the direction
// .tonearm-seg2's rotate(18deg) actually bends relative to .tonearm-seg1 --
// this was the sign that was backwards before, and it only shows up as an
// error once the outer sweep rotation is layered on top of the bend.
const ARM_TIP_LOCAL_X = -ARM_SEG2_LEN * Math.sin(ARM_BEND_RAD);
const ARM_TIP_LOCAL_Y = ARM_SEG1_LEN + ARM_SEG2_LEN * Math.cos(ARM_BEND_RAD);
const ARM_EFFECTIVE_LEN = Math.hypot(ARM_TIP_LOCAL_X, ARM_TIP_LOCAL_Y);
// The bend alone (before any sweep rotation) already points the tip this
// many degrees off straight-down -- baked into every angle below so a
// sweep angle of 0 still means "arm hanging naturally bent," not "tip
// pointing straight down through the bend."
const ARM_BEND_OFFSET_DEG = Math.atan2(ARM_TIP_LOCAL_X, ARM_TIP_LOCAL_Y) * 180 / Math.PI;

// Finds the rotation (in the same convention CSS rotate() uses here) that
// puts a straight rod of length `armLen`, pivoted at (pivotX,pivotY), at
// exactly `targetR` px from (cx,cy) -- i.e. treats "how far from the
// record's center should the stylus sit" as the source of truth instead of
// a hand-guessed rotation angle, so the tip is geometrically guaranteed to
// land where intended regardless of how the panel around it is laid out.
function angleForTargetRadius(pivotX, pivotY, cx, cy, armLen, targetR) {
  const dx = cx - pivotX, dy = cy - pivotY;
  const d = Math.hypot(dx, dy);
  if (!d) return 0;
  const a = (armLen * armLen - targetR * targetR + d * d) / (2 * d);
  const h = Math.sqrt(Math.max(0, armLen * armLen - a * a));
  const ux = dx / d, uy = dy / d;
  const mx = pivotX + a * ux, my = pivotY + a * uy;
  const candidates = [
    [mx - h * uy, my + h * ux],
    [mx + h * uy, my - h * ux],
  ];
  // Two mathematically valid intersections exist (the arm could reach that
  // radius swung to either side); keep whichever is closest to the rest
  // angle so the arm sweeps continuously instead of ever jumping to the
  // mirrored solution on the far side of the disc.
  let best = 0, bestDiff = Infinity;
  for (const [px, py] of candidates) {
    const angle = Math.atan2(-(px - pivotX), py - pivotY) * (180 / Math.PI);
    const diff = Math.abs(angle - TONEARM_REST_DEG);
    if (diff < bestDiff) { bestDiff = diff; best = angle; }
  }
  return best;
}

function updateVinylTonearm() {
  const tonearm = el("tonearm");
  const disc = el("vinyl-disc");
  if (!tonearm || !disc) return;
  const isRadio = !!(state.currentTrack && state.currentTrack.isRadio);
  if (!audio.src || (!isRadio && (!isFinite(audio.duration) || !audio.duration))) {
    tonearm.style.transform = `rotate(${TONEARM_REST_DEG}deg)`;
    return;
  }
  // A live stream has no seekable duration to derive a real position from
  // -- audio.duration stays Infinity/NaN for as long as it plays. Rest at
  // a fixed point midway across the record instead of parking off the
  // disc entirely, so the panel actually looks like something's playing.
  const progress = isRadio ? 0.5 : Math.max(0, Math.min(1, audio.currentTime / audio.duration));

  // Measured from the live layout on every call (offsetLeft/Top/Width
  // reflect pre-transform position, unaffected by the disc's own spin
  // animation or any ancestor scale transform) rather than assumed from
  // hardcoded pixel values -- this is what keeps the tip landing on the
  // record through any future resize or redesign, in any layout mode,
  // without needing a second hand-tuned set of angles for each one.
  const pivotX = tonearm.offsetLeft + tonearm.offsetWidth / 2;
  const pivotY = tonearm.offsetTop + tonearm.offsetHeight / 2;
  const discCenterX = disc.offsetLeft + disc.offsetWidth / 2;
  const discCenterY = disc.offsetTop + disc.offsetHeight / 2;
  const discRadius = disc.offsetWidth / 2;

  const d = Math.hypot(discCenterX - pivotX, discCenterY - pivotY);
  if (!d) return;

  // outerR: right at the physical edge of the record. innerR: rMin is the
  // closest distance this arm's geometry can ever bring the stylus to the
  // center (a fixed rod pivoted this far away can't reach the label no
  // matter the angle) -- using it directly means "as far in as physically
  // possible," not a guessed fraction of the radius.
  const rMin = Math.abs(ARM_EFFECTIVE_LEN - d) + 4;
  const rMax = ARM_EFFECTIVE_LEN + d - 4;
  const outerR = Math.min(rMax, discRadius * 0.98);
  const innerR = Math.max(rMin, Math.min(outerR, discRadius * 0.05));
  const targetR = outerR + progress * (innerR - outerR);

  const straightAngle = angleForTargetRadius(pivotX, pivotY, discCenterX, discCenterY, ARM_EFFECTIVE_LEN, targetR);
  tonearm.style.transform = `rotate(${straightAngle + ARM_BEND_OFFSET_DEG}deg)`;
}

// Drag-to-seek: the reverse of updateVinylTonearm's radius-from-progress
// math above. There, a known progress picks a target radius, which is
// solved into a rotation angle. Here, the pointer's direction from the
// pivot is what's known -- projecting it out to the arm's own fixed
// length (a rigid rod, always exactly ARM_EFFECTIVE_LEN from the pivot,
// whichever way it's pointed) gives the tip position that direction
// would put it at, and that tip's distance from the disc center is
// exactly the "target radius" updateVinylTonearm would have started
// from, so it inverts cleanly back to a progress fraction.
function seekFromTonearmPointer(clientX, clientY) {
  const tonearm = el("tonearm");
  const disc = el("vinyl-disc");
  if (!tonearm || !disc || !audio.src || !isFinite(audio.duration) || !audio.duration) return;

  // getBoundingClientRect() alone doesn't match updateVinylTonearm's own
  // geometry: the disc can render at a different size than its layout
  // box via a CSS scale transform (fitStagePanel fits the stage to
  // whatever container size it's actually in), same reasoning as that
  // function's own offsetLeft/Top/Width use -- ARM_EFFECTIVE_LEN and the
  // rest of this arm's dimensions are authored against the *unscaled*
  // layout box, so a real pointer position (necessarily post-transform,
  // there's no other way to receive one) has to be converted back into
  // that same unscaled space before any of this geometry lines up with
  // it, or the arm answers a systematically wrong radius for wherever
  // it's actually pointed.
  const discRect = disc.getBoundingClientRect();
  const discScale = disc.offsetWidth ? (discRect.width / disc.offsetWidth) : 1;

  const parent = tonearm.offsetParent;
  const parentRect = parent ? parent.getBoundingClientRect() : { left: 0, top: 0 };
  const pivotX = parentRect.left + tonearm.offsetLeft + tonearm.offsetWidth / 2;
  const pivotY = parentRect.top + tonearm.offsetTop + tonearm.offsetHeight / 2;
  const discCenterX = parentRect.left + disc.offsetLeft + disc.offsetWidth / 2;
  const discCenterY = parentRect.top + disc.offsetTop + disc.offsetHeight / 2;
  const discRadius = disc.offsetWidth / 2;

  // Undoes the disc's own render-time scale around its center, so this
  // lands in the same unscaled space as pivotX/Y and discCenterX/Y above.
  const mouseX = discCenterX + (clientX - discCenterX) / discScale;
  const mouseY = discCenterY + (clientY - discCenterY) / discScale;

  const d = Math.hypot(discCenterX - pivotX, discCenterY - pivotY);
  if (!d) return;

  const rMin = Math.abs(ARM_EFFECTIVE_LEN - d) + 4;
  const rMax = ARM_EFFECTIVE_LEN + d - 4;
  const outerR = Math.min(rMax, discRadius * 0.98);
  const innerR = Math.max(rMin, Math.min(outerR, discRadius * 0.05));

  const dx = mouseX - pivotX, dy = mouseY - pivotY;
  const dist = Math.hypot(dx, dy);
  if (!dist) return;
  const tipX = pivotX + (dx / dist) * ARM_EFFECTIVE_LEN;
  const tipY = pivotY + (dy / dist) * ARM_EFFECTIVE_LEN;
  const targetR = Math.hypot(tipX - discCenterX, tipY - discCenterY);

  const clampedR = Math.max(innerR, Math.min(outerR, targetR));
  const progress = Math.max(0, Math.min(1, (clampedR - outerR) / (innerR - outerR)));

  audio.currentTime = progress * audio.duration;
  updateVinylTonearm();
}

let tonearmDragging = false;
function initTonearmDrag() {
  const tonearm = el("tonearm");
  if (!tonearm) return;
  tonearm.addEventListener("pointerdown", (e) => {
    if (!audio.src) return;
    tonearmDragging = true;
    tonearm.classList.add("dragging");
    tonearm.setPointerCapture(e.pointerId);
    seekFromTonearmPointer(e.clientX, e.clientY);
    e.preventDefault();
  });
  tonearm.addEventListener("pointermove", (e) => {
    if (!tonearmDragging) return;
    seekFromTonearmPointer(e.clientX, e.clientY);
  });
  const endDrag = (e) => {
    if (!tonearmDragging) return;
    tonearmDragging = false;
    tonearm.classList.remove("dragging");
    try { tonearm.releasePointerCapture(e.pointerId); } catch (err) { /* already released */ }
  };
  tonearm.addEventListener("pointerup", endDrag);
  tonearm.addEventListener("pointercancel", endDrag);
}
initTonearmDrag();

// Driven by rAF rather than the audio "timeupdate" event, same as the VU
// meters and cassette reels below -- timeupdate's firing rate isn't
// specified and isn't reliably frequent (observed as slow enough in
// pywebview's WKWebView that the tonearm visibly lagged well behind the
// actual playback position instead of tracking it).
//
// Also drives the disc's own spin (a plain JS transform, not a CSS
// @keyframes animation) for the same reliability reason: a CSS animation
// left paused while its element's ancestor is display:none (switching away
// from the vinyl theme and back) would occasionally never resume.
// vinylDiscAngle persists across loop start/stops so the disc resumes from
// wherever it stopped rather than snapping back to 0; vinylDiscLastTs is
// reset to null on each (re)start so the first frame after a pause/theme
// switch doesn't jump the disc forward by the elapsed idle time.
let vinylTonearmRafId = null;
let vinylDiscAngle = 0;
let vinylDiscLastTs = null;
const VINYL_DISC_MS_PER_REV = 2600; // matches the old CSS animation's 2.6s/rev
function vinylTonearmLoop(ts) {
  if (state.theme !== "vinyl" || audio.paused) {
    vinylTonearmRafId = null;
    vinylDiscLastTs = null;
    return;
  }
  const disc = el("vinyl-disc");
  if (disc) {
    if (vinylDiscLastTs != null) {
      vinylDiscAngle = (vinylDiscAngle + ((ts - vinylDiscLastTs) / VINYL_DISC_MS_PER_REV) * 360) % 360;
    }
    vinylDiscLastTs = ts;
    disc.style.transform = `rotate(${vinylDiscAngle}deg)`;
  }
  updateVinylTonearm();
  vinylTonearmRafId = requestAnimationFrame(vinylTonearmLoop);
}
function startVinylTonearmLoopIfNeeded() {
  if (state.theme !== "vinyl" || audio.paused || vinylTonearmRafId) return;
  vinylDiscLastTs = null;
  vinylTonearmRafId = requestAnimationFrame(vinylTonearmLoop);
}

function syncThemeVisuals() {
  const playing = !audio.paused && !!audio.src;

  syncVuAudioMirror();

  // The disc's rotation itself is advanced by vinylTonearmLoop (started
  // below when playing); nothing to do here when paused -- the loop's own
  // guard clause stops advancing it and it stays wherever it was.
  updateVinylTonearm();

  if (playing) {
    startVuLoopIfNeeded();
    startCasVisLoopIfNeeded();
    startCasReelLoopIfNeeded();
    startVinylTonearmLoopIfNeeded();
  }
}

// Shared by any fixed-width "display line" (VFD segments, cassette label
// handwriting, ...): if the text is wider than its box, make it run instead
// of clipping -- scroll left to reveal the tail, hold, scroll back, hold.
function scheduleMarqueeIfOverflowing(line, inner) {
  requestAnimationFrame(() => {
    const overflow = inner.scrollWidth - line.clientWidth;
    if (overflow > 2) {
      // Travel (the 8%-46% and 54%-92% keyframe spans) covers 38% of the
      // total duration each way -- pick a duration so the scroll itself
      // moves at a steady, readable pace no matter how long the text is.
      const pxPerSecond = 45;
      const duration = Math.max(4, overflow / pxPerSecond / 0.38);
      line.style.setProperty("--vfd-scroll-dist", `-${overflow}px`);
      line.style.setProperty("--vfd-duration", `${duration}s`);
      line.classList.add("scrolling");
    }
  });
}

function renderVfdLine(elId, text) {
  const line = el(elId);
  if (!line) return;
  line.classList.remove("scrolling");
  line.style.removeProperty("--vfd-scroll-dist");
  line.style.removeProperty("--vfd-duration");
  line.innerHTML = "";

  const inner = document.createElement("span");
  inner.className = "vfd-line-inner";
  (text || "").toUpperCase().split("").forEach((ch) => {
    const span = document.createElement("span");
    span.className = "vfd-char";
    span.textContent = ch === " " ? " " : ch;
    inner.appendChild(span);
  });
  line.appendChild(inner);
  scheduleMarqueeIfOverflowing(line, inner);
}

function renderScrollingText(elId, text) {
  const line = el(elId);
  if (!line) return;
  line.classList.remove("scrolling");
  line.style.removeProperty("--vfd-scroll-dist");
  line.style.removeProperty("--vfd-duration");
  line.innerHTML = "";

  const inner = document.createElement("span");
  inner.className = "scroll-line-inner";
  inner.textContent = text || "";
  line.appendChild(inner);
  scheduleMarqueeIfOverflowing(line, inner);
}

function themeOnTrackChange(track, artworkUrl) {
  if (artworkUrl === undefined) artworkUrl = artUrl(track.id);
  renderVfdLine("hifi-vfd-artist", track.artist || "UNKNOWN ARTIST");
  renderVfdLine("hifi-vfd-title", track.title || "UNTITLED");

  const hifiArt = el("hifi-art");
  if (hifiArt) {
    if (artworkUrl) {
      hifiArt.classList.remove("hidden");
      hifiArt.onerror = () => hifiArt.classList.add("hidden");
      hifiArt.src = artworkUrl;
    } else {
      hifiArt.classList.add("hidden");
    }
  }
  el("hifi-seek").value = 0;
  el("hifi-seek").style.setProperty("--fill", "0%");
  el("cassette-seek").value = 0;
  el("cassette-seek").style.setProperty("--fill", "0%");
  el("vinyl-seek").value = 0;
  el("vinyl-seek").style.setProperty("--fill", "0%");

  renderScrollingText("cassette-hw-artist", track.artist || "Unknown Artist");
  renderScrollingText("cassette-hw-title", track.title || "Untitled");
  const cassetteArt = el("cassette-art");
  if (cassetteArt) {
    if (artworkUrl) {
      cassetteArt.classList.remove("hidden");
      cassetteArt.onerror = () => cassetteArt.classList.add("hidden");
      cassetteArt.src = artworkUrl;
    } else {
      cassetteArt.classList.add("hidden");
    }
  }

  const vinylDisc = el("vinyl-disc");
  if (vinylDisc) vinylDisc.style.setProperty("--vinyl-art", artworkUrl ? `url("${artworkUrl}")` : "none");
  updateVinylTonearm();
  renderScrollingText("vinyl-np-artist", track.artist || "Unknown Artist");
  renderScrollingText("vinyl-np-title", track.title || "Untitled");
  fitStagePanel();
}

// Tape wound around a reel is a ring outside the hub, drawn as a stroked
// circle: HUB_R is the fixed inner edge (the hub itself, always visible),
// MAX_TAPE_R is the outer edge when a reel is completely full.
const CAS_HUB_R = 20.2; // 12.6 * 1.6 -- sprockets enlarged another 60%
const CAS_MAX_TAPE_R = 72; // 48 * 1.5 -- tape enlarged another 50%, clipped to the window bounds

function setTapeRing(el2, outerR) {
  if (!el2) return;
  const clamped = Math.max(CAS_HUB_R, outerR);
  el2.setAttribute("r", (CAS_HUB_R + clamped) / 2);
  el2.setAttribute("stroke-width", clamped - CAS_HUB_R);
}

// Current wound-tape outer radius on each reel, shared with the sprocket
// spin loop below so it can spin each reel at the correct speed for how
// much tape is actually on it right now.
let casReelOuterLeft = CAS_MAX_TAPE_R, casReelOuterRight = CAS_HUB_R;

function themeOnTimeUpdate() {
  updateVinylTonearm();
  if (!isFinite(audio.duration) || !audio.duration) return;
  const progress = audio.currentTime / audio.duration;
  // Tape spools from the left (supply) reel to the right (take-up) reel as it
  // plays -- the reels themselves stay put; only the amount of tape wound
  // around each one (the thickness of that ring) changes.
  casReelOuterLeft = CAS_MAX_TAPE_R - progress * (CAS_MAX_TAPE_R - CAS_HUB_R);
  casReelOuterRight = CAS_HUB_R + progress * (CAS_MAX_TAPE_R - CAS_HUB_R);
  setTapeRing(el("reel-tape-left"), casReelOuterLeft);
  setTapeRing(el("reel-tape-right"), casReelOuterRight);
}

// Real tape reels don't spin at a fixed rate: the tape itself moves past the
// head at a constant linear speed, so a reel's angular speed is inversely
// proportional to how much tape is currently wound on it (omega = v / r) --
// a full reel turns slowly, a nearly-empty one spins noticeably faster.
let casReelAngleLeft = 0, casReelAngleRight = 0, casReelLastTime = 0, casReelRafId = null;
const CAS_REEL_SPEED_K = 2600; // deg/sec at radius 1; tuned for the new (larger) radius range

function casReelLoop(ts) {
  if (state.theme !== "cassette" || audio.paused) {
    casReelRafId = null;
    casReelLastTime = 0;
    return;
  }
  const now = ts || performance.now();
  const dt = casReelLastTime ? Math.min(0.1, (now - casReelLastTime) / 1000) : 0.016;
  casReelLastTime = now;

  casReelAngleLeft = (casReelAngleLeft + (CAS_REEL_SPEED_K / Math.max(CAS_HUB_R, casReelOuterLeft)) * dt) % 360;
  casReelAngleRight = (casReelAngleRight + (CAS_REEL_SPEED_K / Math.max(CAS_HUB_R, casReelOuterRight)) * dt) % 360;

  const reelL = el("reel-left"), reelR = el("reel-right");
  if (reelL) reelL.style.transform = `rotate(${casReelAngleLeft}deg)`;
  if (reelR) reelR.style.transform = `rotate(${casReelAngleRight}deg)`;

  casReelRafId = requestAnimationFrame(casReelLoop);
}

function startCasReelLoopIfNeeded() {
  if (state.theme !== "cassette" || audio.paused || casReelRafId) return;
  casReelLastTime = 0;
  casReelRafId = requestAnimationFrame(casReelLoop);
}

// Real audio-reactive VU meters via the Web Audio API.
let audioCtx = null, analyser = null, vuDataArray = null, vuFreqArray = null, vuRafId = null;
let vuLevelL = 0, vuLevelR = 0, vuLastTime = 0;

// Server-computed replacement for the analyser, used only while playing
// internet radio (see vuLoop/casVisLoop) -- kept updated by
// startRadioLevelsPolling below.
let radioLevel = 0;
let radioBands = new Array(12).fill(0);
let radioLevelsTimer = null;
let radioLevelsSid = null;

function stopRadioLevelsPolling() {
  if (radioLevelsTimer) {
    clearInterval(radioLevelsTimer);
    radioLevelsTimer = null;
  }
  radioLevel = 0;
  radioBands = new Array(12).fill(0);
  // Lets the ffmpeg decode behind this sid exit immediately instead of
  // waiting out its own 15s idle timeout -- best-effort, the app closing
  // outright is exactly what that timeout exists for anyway.
  if (radioLevelsSid) {
    api("/radio/levels/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sid: radioLevelsSid }),
    }).catch(() => {});
    radioLevelsSid = null;
  }
}

function startRadioLevelsPolling(sid) {
  stopRadioLevelsPolling();
  radioLevelsSid = sid;
  api("/radio/levels/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url: state.currentTrack.streamUrl, sid }),
  }).catch(() => {});
  radioLevelsTimer = setInterval(async () => {
    try {
      const data = await api(`/radio/levels?sid=${sid}`);
      if (data.ok) {
        radioLevel = data.level;
        radioBands = data.bands;
      }
    } catch (e) { /* keep the last known values on a transient failure */ }
  }, 150);
}

function ensureAudioGraph() {
  if (audioCtx) return;
  // Taps vuAudio (see its own definition above), never the real `audio`
  // element -- and never connects to audioCtx.destination at all, since
  // this graph only exists to feed the analyser numbers, not to be heard.
  // vuAudio is deliberately NOT muted (see its own definition) -- this
  // missing destination connection is what actually keeps it silent.
  // Only ever called for local file playback (see
  // startVuLoopIfNeeded/startCasVisLoopIfNeeded) -- internet radio uses
  // server-computed levels instead, since WebKit has long-standing bugs
  // where this never produces usable data for network-streamed audio at
  // all (bugs.webkit.org #180696, #211394).
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = audioCtx.createMediaElementSource(vuAudio);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    vuDataArray = new Uint8Array(analyser.frequencyBinCount);
    vuFreqArray = new Uint8Array(analyser.frequencyBinCount);
  } catch (e) {
    console.warn("Web Audio setup failed:", e);
  }
}

// Real VU meters are mechanical: the needle can't jump to a new reading, it
// swings toward it. IEC 60268-17 defines a ~300ms ballistic response (rise
// to 99% of a step in 300ms, same nominal speed on the way back down), which
// is why a real meter looks smooth and slightly lazy instead of twitchy.
// We reproduce that with an exponential follower instead of drawing the raw
// per-frame signal level directly.
const VU_BALLISTIC_TAU = 0.3; // seconds

function vuLoop(ts) {
  const isRadio = state.currentTrack && state.currentTrack.isRadio;
  if (state.theme !== "hifi" || (!analyser && !isRadio)) { vuRafId = null; vuLastTime = 0; return; }

  const playing = !audio.paused;
  let target = 0;
  if (playing && isRadio) {
    // WebKit's AnalyserNode never produces real data for a live-streamed
    // element (see ensureAudioGraph's comment) -- radioLevel is computed
    // server-side instead and just polled in.
    target = Math.min(1, radioLevel * 3.2);
  } else if (playing) {
    analyser.getByteTimeDomainData(vuDataArray);
    let sum = 0;
    for (let i = 0; i < vuDataArray.length; i++) {
      const v = (vuDataArray[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / vuDataArray.length);
    target = Math.min(1, rms * 3.2);
  }

  const now = ts || performance.now();
  const dt = vuLastTime ? Math.min(0.1, (now - vuLastTime) / 1000) : 0.016;
  vuLastTime = now;

  // The two needles are separate mechanical movements reading the same
  // program material, so they settle at very slightly different rates --
  // never perfectly in lockstep, but never randomly jittering either.
  const coeffL = 1 - Math.exp(-dt / VU_BALLISTIC_TAU);
  const coeffR = 1 - Math.exp(-dt / (VU_BALLISTIC_TAU * 1.08));
  vuLevelL += (target - vuLevelL) * coeffL;
  vuLevelR += (target - vuLevelR) * coeffR;

  const needleL = el("vu-needle-l");
  const needleR = el("vu-needle-r");
  if (needleL) needleL.style.transform = `rotate(${-60 + vuLevelL * 96}deg)`;
  if (needleR) needleR.style.transform = `rotate(${-60 + vuLevelR * 96}deg)`;

  // Once paused and the needles have settled back near rest, stop the loop
  // instead of freezing mid-swing.
  if (!playing && vuLevelL < 0.002 && vuLevelR < 0.002) {
    vuLevelL = 0; vuLevelR = 0; vuLastTime = 0; vuRafId = null;
    return;
  }
  vuRafId = requestAnimationFrame(vuLoop);
}

function startVuLoopIfNeeded() {
  if (state.theme !== "hifi" || audio.paused || vuRafId) return;
  const isRadio = state.currentTrack && state.currentTrack.isRadio;
  if (!isRadio) {
    // Radio doesn't need this graph at all (see vuLoop) -- skip it there
    // so a Web Audio hiccup can't block the meter from running off
    // server-computed levels instead.
    ensureAudioGraph();
    if (!audioCtx) return;
    if (audioCtx.state === "suspended") audioCtx.resume();
  }
  vuLastTime = 0;
  vuRafId = requestAnimationFrame(vuLoop);
}

// Cassette-deck spectrum bars, built once and driven off the same analyser
// the Hi-Fi VU meters use.
const CAS_VIS_BAR_COUNT = 12;
let casVisRafId = null, casVisBars = null, casVisLevels = null;

function ensureCasVisBars() {
  const container = el("cassette-visualizer");
  if (!container) return null;
  if (casVisBars && casVisBars[0] && casVisBars[0].isConnected) return casVisBars;
  container.innerHTML = "";
  casVisBars = [];
  for (let i = 0; i < CAS_VIS_BAR_COUNT; i++) {
    const bar = document.createElement("div");
    bar.className = "cas-vis-bar";
    container.appendChild(bar);
    casVisBars.push(bar);
  }
  casVisLevels = casVisBars.map(() => 0);
  return casVisBars;
}

function casVisLoop() {
  const isRadio = state.currentTrack && state.currentTrack.isRadio;
  if (state.theme !== "cassette" || (!analyser && !isRadio)) { casVisRafId = null; return; }
  const bars = ensureCasVisBars();
  if (!bars) { casVisRafId = null; return; }

  const playing = !audio.paused;
  let targets;
  if (playing && isRadio) {
    // Same WebKit limitation as vuLoop -- radioBands is computed
    // server-side and just polled in for a live stream.
    targets = radioBands;
  } else if (playing) {
    analyser.getByteFrequencyData(vuFreqArray);
    const perBar = Math.floor(vuFreqArray.length / bars.length);
    targets = bars.map((_, i) => {
      let sum = 0;
      for (let j = 0; j < perBar; j++) sum += vuFreqArray[i * perBar + j];
      return sum / perBar / 255;
    });
  } else {
    targets = bars.map(() => 0);
  }

  let stillMoving = false;
  bars.forEach((bar, i) => {
    casVisLevels[i] += (targets[i] - casVisLevels[i]) * 0.35;
    if (Math.abs(targets[i] - casVisLevels[i]) > 0.004) stillMoving = true;
    bar.style.height = `${Math.max(6, casVisLevels[i] * 100)}%`;
  });

  if (!playing && !stillMoving) { casVisRafId = null; return; }
  casVisRafId = requestAnimationFrame(casVisLoop);
}

function startCasVisLoopIfNeeded() {
  if (state.theme !== "cassette" || audio.paused || casVisRafId) return;
  const isRadio = state.currentTrack && state.currentTrack.isRadio;
  if (!isRadio) {
    ensureAudioGraph();
    if (!audioCtx) return;
    if (audioCtx.state === "suspended") audioCtx.resume();
  }
  ensureCasVisBars();
  casVisRafId = requestAnimationFrame(casVisLoop);
}

// --------------------------------------------------------------- rating --
function updateStars(rating) {
  document.querySelectorAll("#stars span").forEach((s) => {
    s.classList.toggle("filled", Number(s.dataset.star) <= rating);
  });
}
// Shared by the footer star row's click handler and the 1-5 keyboard
// shortcuts below -- update in place rather than reloading the list, so
// rating doesn't reset scroll position/pagination. If the track is
// visible in the current list and the new rating now violates an active
// rating filter, fade its row out instead of forcing a full reload.
async function rateCurrentTrack(rating) {
  if (!state.currentTrack || state.currentTrack.isRadio) return;
  const t = state.currentTrack;
  const playingRow = document.querySelector(".track-row.playing");
  const rowStars = playingRow ? playingRow.querySelector(".row-stars") : null;
  const actual = await rateTrack(t, rating, rowStars);
  const violatesFilter = (state.ratedOnly && actual === 0) || (state.rating && Number(state.rating) !== actual);
  if (playingRow && violatesFilter) {
    playingRow.style.transition = "opacity .2s ease";
    playingRow.style.opacity = "0";
    setTimeout(() => removeTrackRow(t.id, playingRow), 200);
  }
}
el("stars").addEventListener("click", (e) => {
  const starEl = e.target.closest("span[data-star]");
  if (!starEl) return;
  rateCurrentTrack(Number(starEl.dataset.star));
});
el("stars").addEventListener("mousemove", (e) => {
  const starEl = e.target.closest("span[data-star]");
  if (!starEl) return;
  const hoverRating = Number(starEl.dataset.star);
  document.querySelectorAll("#stars span").forEach((s) => {
    s.classList.toggle("filled", Number(s.dataset.star) <= hoverRating);
  });
});
el("stars").addEventListener("mouseleave", () => {
  updateStars(state.currentTrack ? state.currentTrack.rating || 0 : 0);
});

// ------------------------------------------------------------- filters --
let searchDebounce;
el("search").addEventListener("input", (e) => {
  clearTimeout(searchDebounce);
  const val = e.target.value;
  if (val.startsWith("/")) {
    showSearchSuggestions(val.slice(1).trim().toLowerCase());
    return; // composing a command, not a library search
  }
  hideSearchSuggestions();
  searchDebounce = setTimeout(() => {
    state.q = val;
    loadTracks(true);
  }, 300);
});
function multiSelectValue(selectEl) {
  return Array.from(selectEl.selectedOptions).map((o) => o.value).join(",");
}
el("filter-genre").addEventListener("change", (e) => { state.genre = multiSelectValue(e.target); loadTracks(true); });
el("filter-decade").addEventListener("change", (e) => { state.decade = multiSelectValue(e.target); loadTracks(true); });
el("filter-language").addEventListener("change", (e) => { state.language = e.target.value; loadTracks(true); });
el("filter-artist").addEventListener("change", (e) => { state.artist = e.target.value; loadTracks(true); });
el("filter-rated").addEventListener("change", (e) => { state.ratedOnly = e.target.checked; loadTracks(true); });
el("filter-rating").addEventListener("change", (e) => {
  state.rating = e.target.value;
  updateDeleteRatedVisibility();
  loadTracks(true);
});
el("sort").addEventListener("change", (e) => { state.sort = e.target.value; loadTracks(true); });
function clearAllFilters() {
  state.q = ""; state.genre = ""; state.decade = ""; state.language = ""; state.artist = ""; state.ratedOnly = false; state.rating = ""; state.sort = "artist";
  el("search").value = "";
  Array.from(el("filter-genre").options).forEach((o) => { o.selected = false; });
  Array.from(el("filter-decade").options).forEach((o) => { o.selected = false; });
  el("filter-language").value = ""; el("filter-artist").value = ""; el("filter-rated").checked = false;
  el("filter-rating").value = ""; el("sort").value = "artist";
  updateDeleteRatedVisibility();
  loadTracks(true);
}
el("clear-filters").addEventListener("click", clearAllFilters);

// The bulk-delete action is scoped tightly to the "exactly 1 star" filter --
// it never appears for any other rating so there's no risk of it acting on
// tracks the user didn't explicitly filter down to for deletion.
function updateDeleteRatedVisibility() {
  el("delete-rated-1").classList.toggle("hidden", state.rating !== "1");
}
el("delete-rated-1").addEventListener("click", async () => {
  const confirmed = await customConfirm(
    "Permanently delete every 1-star rated file from disk? This cannot be undone.",
    { okLabel: "Delete", danger: true }
  );
  if (!confirmed) return;
  const result = await api("/delete-rated", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ rating: 1 }),
  });
  showToast(`Deleted ${result.deleted} file(s).${result.errors.length ? ` ${result.errors.length} failed.` : ""}`, { kind: result.errors.length ? "error" : "success" });
  loadFacets();
  loadTracks(true);
});

// ------------------------------------------------------------- playlists --
async function loadPlaylists() {
  const playlists = await api("/playlists");
  const listEl = el("playlist-list");
  listEl.innerHTML = "";
  playlists.forEach((p) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="pl-name">${escapeHtml(p.name)}</span>
      <span class="count">${p.track_count}</span>
      <button class="pl-delete" title="Delete playlist">🗑</button>
    `;
    li.addEventListener("click", () => openPlaylist(p.id));
    li.querySelector(".pl-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!(await customConfirm(`Delete playlist "${p.name}"? This cannot be undone.`, { okLabel: "Delete", danger: true }))) return;
      await api(`/playlists/${p.id}`, { method: "DELETE" });
      if (state.activePlaylistId === p.id) {
        state.activePlaylistId = null;
        el("playlist-view").classList.add("hidden");
        el("library-view").classList.remove("hidden");
      }
      await loadPlaylists();
    });
    listEl.appendChild(li);
  });
}

async function openPlaylist(playlistId) {
  const data = await api(`/playlists/${playlistId}`);
  state.activePlaylistId = playlistId;
  el("library-view").classList.add("hidden");
  el("playlist-view").classList.remove("hidden");
  el("playlist-view-title").textContent = data.name;
  clearSelection();
  renderTrackList(el("playlist-tracks"), data.tracks, { showAdd: false });
  state.currentList = data.tracks;
}
el("back-to-library").addEventListener("click", () => {
  state.activePlaylistId = null;
  el("playlist-view").classList.add("hidden");
  el("library-view").classList.remove("hidden");
  clearSelection();
});
el("delete-playlist").addEventListener("click", async () => {
  if (!state.activePlaylistId) return;
  if (!(await customConfirm("Delete this playlist? This cannot be undone.", { okLabel: "Delete", danger: true }))) return;
  await api(`/playlists/${state.activePlaylistId}`, { method: "DELETE" });
  state.activePlaylistId = null;
  el("playlist-view").classList.add("hidden");
  el("library-view").classList.remove("hidden");
  loadPlaylists();
});

el("new-playlist").addEventListener("click", () => {
  openModal("New playlist", `<input type="text" id="modal-playlist-name" placeholder="Playlist name">`, async () => {
    const name = el("modal-playlist-name").value.trim();
    const p = await api("/playlists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    await loadPlaylists();
    closeModal();
    openPlaylist(p.id);
  });
});

// Playlist pickers (this one and the Convert flow's below) just listed
// every playlist with no way to narrow it down -- fine with a handful,
// but it's a long undifferentiated scroll once there are many (each one
// showing 0 tracks or not is no help either, since an empty playlist
// looks identical to a real one in the list). A filter box earns its
// keep past a small count; below that it's just one more control for no
// reason, so it's only added when it'd actually help.
const PLAYLIST_FILTER_THRESHOLD = 6;

function playlistFilterInputHtml(inputId) {
  return `<input type="text" id="${inputId}" placeholder="Filter playlists…" style="margin-bottom:8px">`;
}

function wirePlaylistFilter(inputId, containerId) {
  const input = el(inputId);
  if (!input) return;
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    document.querySelectorAll(`#${containerId} .modal-list-item`).forEach((item) => {
      const name = (item.querySelector("span")?.textContent || "").toLowerCase();
      item.classList.toggle("hidden", !!q && !name.includes(q));
    });
  });
}

async function openAddToPlaylistModal(trackIds) {
  const playlists = await api("/playlists");
  const label = trackIds.length > 1 ? `Add to playlist` : `Add to playlist`;
  const countNote = trackIds.length > 1
    ? `<p style="color:var(--text-dim);font-size:12px;margin:0 0 12px">${trackIds.length} tracks selected</p>`
    : "";
  let body = countNote;
  if (playlists.length > PLAYLIST_FILTER_THRESHOLD) body += playlistFilterInputHtml("modal-playlist-filter");
  body += `<div id="modal-playlist-picker">`;
  if (playlists.length === 0) {
    body += `<p style="color:var(--text-dim)">No playlists yet — create one below.</p>`;
  } else {
    playlists.forEach((p) => {
      body += `<div class="modal-list-item" data-id="${p.id}"><span>${escapeHtml(p.name)}</span><span class="count">${p.track_count}</span></div>`;
    });
  }
  body += `</div><hr style="border-color:var(--border);margin:12px 0"><input type="text" id="modal-new-playlist-name" placeholder="Or create new playlist…">`;
  openModal(label, body, async () => {
    const newName = el("modal-new-playlist-name").value.trim();
    if (newName) {
      const p = await api("/playlists", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: newName, track_ids: trackIds }),
      });
      await loadPlaylists();
      closeModal();
      clearSelection();
      return;
    }
    closeModal();
  });
  wirePlaylistFilter("modal-playlist-filter", "modal-playlist-picker");
  document.querySelectorAll("#modal-playlist-picker .modal-list-item").forEach((item) => {
    item.addEventListener("click", async () => {
      await api(`/playlists/${item.dataset.id}/tracks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_ids: trackIds }),
      });
      await loadPlaylists();
      closeModal();
      clearSelection();
    });
  });
}

// ------------------------------------------------------------------ radio --
// Genre-seeded shuffle: pick a genre, and it immediately starts playing a
// random shuffle of that genre (not tied to whatever's currently playing,
// unlike the old "similar tracks" radio -- this lives in the tile bar now,
// alongside the other library-wide tools, so it shouldn't depend on a
// track already being loaded).
el("radio-mode").addEventListener("click", () => {
  const genres = Array.from(el("filter-genre").options).map((o) => o.value).filter(Boolean);
  if (!genres.length) return;
  const body = `
    <div id="radio-genre-picker" style="display:flex;flex-direction:column;gap:6px;max-height:320px;overflow-y:auto">
      ${genres.map((g) => `<div class="modal-list-item" data-genre="${escapeHtml(g)}"><span>${escapeHtml(g)}</span></div>`).join("")}
    </div>
  `;
  openModal("Play radio — pick a genre", body, () => {});
  document.querySelectorAll("#radio-genre-picker .modal-list-item").forEach((item) => {
    item.addEventListener("click", async () => {
      closeModal();
      startGenreRadio(item.dataset.genre);
    });
  });
});

async function startGenreRadio(genre) {
  const data = await api(`/radio?genre=${encodeURIComponent(genre)}&count=30`);
  if (!data.tracks.length) { showToast(`No tracks found in "${genre}".`, { kind: "info" }); return; }
  state.activePlaylistId = null;
  el("library-view").classList.add("hidden");
  el("playlist-view").classList.remove("hidden");
  el("playlist-view-title").textContent = `📻 Radio — ${genre}`;
  renderTrackList(el("playlist-tracks"), data.tracks, { showAdd: true });
  state.radioQueue = data.tracks;
  playTrack(data.tracks[0], data.tracks);
  el("delete-playlist").textContent = "Save as playlist";
  el("delete-playlist").classList.remove("danger");
  el("delete-playlist").onclick = async () => {
    const name = prompt("Save radio as playlist named:", `Radio - ${genre}`);
    if (!name) return;
    await api("/playlists", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, track_ids: data.tracks.map((t) => t.id) }),
    });
    await loadPlaylists();
    el("delete-playlist").textContent = "Delete playlist";
    el("delete-playlist").classList.add("danger");
    showToast("Saved!", { kind: "success" });
  };
}

// restore default delete-playlist handler when navigating back to a real playlist
const originalBackHandler = el("back-to-library");
originalBackHandler.addEventListener("click", () => {
  el("delete-playlist").textContent = "Delete playlist";
  el("delete-playlist").classList.add("danger");
  el("delete-playlist").onclick = async () => {
    if (!state.activePlaylistId) return;
    if (!(await customConfirm("Delete this playlist? This cannot be undone.", { okLabel: "Delete", danger: true }))) return;
    await api(`/playlists/${state.activePlaylistId}`, { method: "DELETE" });
    state.activePlaylistId = null;
    state.activeSmartPlaylistId = null;
    el("playlist-view").classList.add("hidden");
    el("library-view").classList.remove("hidden");
    loadPlaylists();
  };
});

// -------------------------------------------------------------- convert --
// `buildRequest(fmt)` returns {endpoint, body, resultTextFn} for whatever
// is being converted (a selection or a whole playlist) -- kept as a plain
// object builder rather than an immediate API call so this can show the
// destination folder and let it be changed before anything actually
// starts.
async function openFormatPickerModal(title, buildRequest) {
  const dest = await api("/convert/output-dir");
  const body = `
    <p style="color:var(--text-dim);font-size:12px;margin:0 0 12px">
      Writes a new file -- your original is never touched or deleted. Converting into MP3 320 is lossy;
      converting a lossy source (MP3/AAC) into FLAC/ALAC repackages it without recovering lost quality.
    </p>
    <div class="convert-dest-row">
      <span class="convert-dest-label">Save to:</span>
      <span id="convert-dest-path" class="convert-dest-path" title="${escapeHtml(dest.path)}">${escapeHtml(dest.path)}</span>
      <button id="convert-dest-change" class="btn-small">Change…</button>
    </div>
    <div id="format-picker" style="display:flex;flex-direction:column;gap:6px;margin-top:12px">
      <div class="modal-list-item" data-format="flac"><span>FLAC</span><span class="count">lossless</span></div>
      <div class="modal-list-item" data-format="alac"><span>ALAC</span><span class="count">lossless, .m4a</span></div>
      <div class="modal-list-item" data-format="mp3320"><span>MP3 320</span><span class="count">lossy, smaller</span></div>
    </div>
  `;
  openModal(title, body, () => {});
  el("convert-dest-change").addEventListener("click", async () => {
    const result = await api("/convert/output-dir", { method: "POST" });
    if (result.ok) {
      el("convert-dest-path").textContent = result.path;
      el("convert-dest-path").title = result.path;
    }
  });
  document.querySelectorAll("#format-picker .modal-list-item").forEach((item) => {
    item.addEventListener("click", () => {
      const { endpoint, body: reqBody, resultTextFn } = buildRequest(item.dataset.format);
      runConvertInModal(endpoint, reqBody, resultTextFn);
    });
  });
}

// Replaces the modal's own body with a progress view and runs the convert
// job in place, rather than closing the modal and tracking progress on
// whichever button was clicked -- convert can be triggered from three
// different toolbar/playlist buttons, and a visual bar reads better here
// than squeezed under an arbitrary button anyway.
async function runConvertInModal(endpoint, body, resultTextFn) {
  el("modal-body").innerHTML = `
    <p id="convert-status" class="dup-summary-text">Converting…</p>
    <div id="convert-progress" class="dup-progress"><div id="convert-progress-fill" class="dup-progress-fill"></div></div>
  `;
  try {
    const started = await api(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (started.error) {
      el("convert-progress").classList.add("hidden");
      el("convert-status").textContent = started.error === "Already running"
        ? "A conversion is already running." : (started.error || "Couldn't start the conversion.");
      return;
    }
    // Each file is a real ffmpeg transcode -- runs in the background with
    // progress polling, same shape as the other long-running jobs, since a
    // big batch can take minutes and a static "Converting…" the whole time
    // looks indistinguishable from stuck.
    const status = await pollProgress("/convert-tracks/progress", (s) => {
      el("convert-status").textContent = s.total ? `Converting… ${s.done}/${s.total}` : "Converting…";
      el("convert-progress-fill").style.width = s.total ? `${Math.min(100, (s.done / s.total) * 100)}%` : "3%";
      return s.running;
    });
    el("convert-progress").classList.add("hidden");
    if (status.error) {
      el("convert-status").textContent = `Conversion failed: ${status.error}`;
      return;
    }
    el("convert-status").textContent = resultTextFn(status.results);
  } catch (e) {
    el("convert-progress").classList.add("hidden");
    el("convert-status").textContent = "Conversion failed.";
  }
}

el("convert-playlist").addEventListener("click", () => {
  if (!state.activePlaylistId) return;
  openFormatPickerModal("Convert every track in this playlist", (fmt) => ({
    endpoint: `/playlists/${state.activePlaylistId}/convert`,
    body: { format: fmt },
    resultTextFn: (r) => `${r.converted}/${r.total} converted`,
  }));
});

// Topbar "Convert" — asks what to convert (current selection, or a playlist), then format.
el("convert-menu").addEventListener("click", async () => {
  if (state.selected.size > 0) {
    openFormatPickerModal("Convert selected tracks", (fmt) => ({
      endpoint: "/convert-tracks",
      body: { track_ids: [...state.selected], format: fmt },
      resultTextFn: (r) => { clearSelection(); return `${r.converted}/${r.total} converted`; },
    }));
    return;
  }

  const playlists = await api("/playlists");
  let body = `<p style="color:var(--text-dim);font-size:12px;margin:0 0 12px">
    Check tracks in the list first to convert a specific selection, or pick a whole playlist below.
  </p>`;
  if (playlists.length > PLAYLIST_FILTER_THRESHOLD) body += playlistFilterInputHtml("convert-source-filter");
  body += `<div id="convert-source-picker">`;
  if (playlists.length === 0) {
    body += `<p style="color:var(--text-dim)">No playlists yet — select some tracks instead.</p>`;
  } else {
    playlists.forEach((p) => {
      body += `<div class="modal-list-item" data-id="${p.id}"><span>${escapeHtml(p.name)}</span><span class="count">${p.track_count} tracks</span></div>`;
    });
  }
  body += `</div>`;
  openModal("Convert — choose what", body, () => {});
  wirePlaylistFilter("convert-source-filter", "convert-source-picker");
  document.querySelectorAll("#convert-source-picker .modal-list-item").forEach((item) => {
    item.addEventListener("click", () => {
      closeModal();
      const playlistId = item.dataset.id;
      openFormatPickerModal("Convert every track in this playlist", (fmt) => ({
        endpoint: `/playlists/${playlistId}/convert`,
        body: { format: fmt },
        resultTextFn: (r) => `${r.converted}/${r.total} converted`,
      }));
    });
  });
});

// Topbar "+ Playlist" — adds your current selection, or (if nothing's
// selected) every track matching the current filters, to a playlist.
el("playlist-menu").addEventListener("click", async () => {
  if (state.selected.size > 0) {
    openAddToPlaylistModal([...state.selected]);
    return;
  }
  const btn = el("playlist-menu");
  const original = getTileText(btn);
  setTileText(btn, "Loading…");
  btn.disabled = true;
  try {
    const data = await api(`/track-ids?${trackQueryParams()}`);
    if (data.ids.length === 0) return;
    openAddToPlaylistModal(data.ids);
  } finally {
    setTileText(btn, original);
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- modal --
let modalOkHandler = null;
function openModal(title, bodyHtml, onOk) {
  el("modal-title").textContent = title;
  el("modal-body").innerHTML = bodyHtml;
  modalOkHandler = onOk;
  el("modal-backdrop").classList.remove("hidden");
}
function closeModal() {
  el("modal-backdrop").classList.add("hidden");
  modalOkHandler = null;
}
el("modal-cancel").addEventListener("click", closeModal);
el("modal-ok").addEventListener("click", () => { if (modalOkHandler) modalOkHandler(); });
el("modal-backdrop").addEventListener("click", (e) => { if (e.target.id === "modal-backdrop") closeModal(); });

// ----------------------------------------------------------------- toast --
// Replaces alert() for one-off messages -- non-blocking, auto-dismissing,
// themed via the active theme's own tokens instead of the OS's plain
// dialog box. role="status" + the container's aria-live (see index.html)
// means a screen reader announces it without needing focus moved to it.
// duration: 0 skips the auto-dismiss timer -- for a toast a caller means to
// keep updating live (see fetchArtForSelection below) and remove itself
// once the work it's reporting on actually finishes, rather than vanishing
// on a fixed clock unrelated to that.
function showToast(message, { kind = "info", duration = 4000 } = {}) {
  const container = el("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  toast.setAttribute("role", "status");
  toast.textContent = message;
  container.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("show"));
  let removed = false;
  let timer = null;
  const remove = () => {
    if (removed) return;
    removed = true;
    if (timer) clearTimeout(timer);
    toast.classList.remove("show");
    setTimeout(() => toast.remove(), 200);
  };
  toast.addEventListener("click", remove);
  if (duration > 0) timer = setTimeout(remove, duration);
  return { update: (text) => { toast.textContent = text; }, remove };
}

// --------------------------------------------------------------- confirm --
// Replaces confirm() -- same reasoning as showToast above, but a question
// that needs an answer instead of a one-off message, so it's a real modal
// (see #confirm-backdrop in index.html) rather than a toast. Promise-based
// since confirm() being synchronous is exactly what made it block the
// whole page in a way nothing else here does.
let confirmResolve = null;
function customConfirm(message, { okLabel = "OK", cancelLabel = "Cancel", danger = false } = {}) {
  return new Promise((resolve) => {
    confirmResolve = resolve;
    el("confirm-message").textContent = message;
    el("confirm-ok").textContent = okLabel;
    el("confirm-cancel").textContent = cancelLabel;
    el("confirm-ok").className = danger ? "danger-btn" : "btn-primary";
    el("confirm-backdrop").classList.remove("hidden");
  });
}
function resolveConfirm(value) {
  el("confirm-backdrop").classList.add("hidden");
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(value);
}
el("confirm-ok").addEventListener("click", () => resolveConfirm(true));
el("confirm-cancel").addEventListener("click", () => resolveConfirm(false));
el("confirm-backdrop").addEventListener("click", (e) => { if (e.target.id === "confirm-backdrop") resolveConfirm(false); });

// ------------------------------------------------------- modal accessibility --
// Applied once per modal backdrop here instead of hand-wiring role/focus-
// trap/Escape into each modal's own open() function separately -- every
// modal in this app already shows/hides itself by toggling the "hidden"
// class on its backdrop (openIpodModal, closeDupPanel, the raw
// classList.add/remove calls scattered through this file, ...), so
// watching for that one shared signal via MutationObserver reaches all of
// them without touching any of those call sites.
function makeModalAccessible(backdrop) {
  const panel = backdrop.firstElementChild;
  if (panel) {
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
  }
  let lastFocused = null;

  function focusablesIn(root) {
    return [...root.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
      .filter((node) => !node.disabled && node.offsetParent !== null);
  }

  document.addEventListener("keydown", (e) => {
    if (backdrop.classList.contains("hidden")) return;
    if (e.key === "Escape") {
      e.stopPropagation();
      const closer = backdrop.querySelector('[id$="-close"], [id$="-cancel"]');
      if (closer) closer.click();
      else backdrop.classList.add("hidden");
      return;
    }
    if (e.key !== "Tab") return;
    const focusables = focusablesIn(backdrop);
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });

  new MutationObserver(() => {
    if (!backdrop.classList.contains("hidden")) {
      lastFocused = document.activeElement;
      const focusables = focusablesIn(backdrop);
      (focusables[0] || panel || backdrop).focus?.();
    } else if (lastFocused && document.body.contains(lastFocused)) {
      lastFocused.focus();
      lastFocused = null;
    }
  }).observe(backdrop, { attributes: true, attributeFilter: ["class"] });
}

[
  "modal-backdrop", "confirm-backdrop", "dup-backdrop", "dup-review-backdrop",
  "tags-backdrop", "ipod-backdrop", "queue-backdrop", "trash-backdrop",
  "stats-backdrop", "help-backdrop", "about-backdrop", "lyrics-backdrop",
  "radio-backdrop", "export-backdrop", "smart-playlist-backdrop", "welcome-backdrop",
].forEach((id) => { const backdrop = el(id); if (backdrop) makeModalAccessible(backdrop); });

// ------------------------------------------------------------- first run --
// Shown once (localStorage-flagged) so a brand new window isn't just an
// unfamiliar library view with no hint that four hardware skins, an iPod
// importer, and live radio are all sitting in the toolbar already.
(function maybeShowWelcome() {
  let seen = false;
  try { seen = localStorage.getItem("jukebox-welcome-seen") === "1"; } catch (e) { /* private browsing etc -- just show it every time */ }
  if (!seen) el("welcome-backdrop").classList.remove("hidden");
})();
function dismissWelcome() {
  el("welcome-backdrop").classList.add("hidden");
  try { localStorage.setItem("jukebox-welcome-seen", "1"); } catch (e) { /* fine to just show it again next launch */ }
}
el("welcome-close").addEventListener("click", dismissWelcome);
el("welcome-dismiss").addEventListener("click", dismissWelcome);
el("welcome-backdrop").addEventListener("click", (e) => { if (e.target.id === "welcome-backdrop") dismissWelcome(); });

// --------------------------------------------------------------- rescan --
async function runRescan(forcePrune = false) {
  const btn = el("rescan-library");
  const original = getTileText(btn);
  setTileText(btn, "Scanning…");
  btn.disabled = true;
  try {
    const started = await api("/rescan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force_prune: forcePrune }),
    });
    if (!started.started) {
      showToast(started.error === "Already running" ? "A scan is already running." : (started.error || "Couldn't start the scan."), { kind: "error" });
      setTileText(btn, original);
      return;
    }
    // A large library (thousands of files on a freshly-connected drive) can
    // take minutes to scan -- poll instead of waiting on one long blocking
    // request, so the button (and the progress bar) shows real progress
    // instead of looking hung.
    showScanProgress(0, 0);
    const status = await pollProgress("/scan-progress", (s) => {
      setTileText(btn, s.total ? `Scanning… ${s.done}/${s.total}` : "Scanning…");
      showScanProgress(s.done, s.total);
      return s.running;
    });
    if (status.error) {
      setTileText(btn, "Failed");
      showToast(`Scan failed: ${status.error}`, { kind: "error" });
      setTimeout(() => { setTileText(btn, original); }, 3000);
      return;
    }
    await loadFacets();
    await loadTracks(true);
    const stats = status.result || {};
    setTileText(btn, `+${stats.inserted || 0} / -${stats.removed || 0}`);
    setTimeout(() => { setTileText(btn, original); }, 3000);
    // The scan deliberately refuses to auto-remove more than ~20% of the
    // index in one pass (a sleeping/disconnected drive could otherwise
    // look like a mass deletion) -- surface that instead of silently
    // saying nothing, with a one-click way to confirm it's expected.
    if (stats.warning) {
      if (await customConfirm(`${stats.warning}\n\nForce cleanup now anyway?`, { okLabel: "Force cleanup", danger: true })) {
        await runRescan(true);
      }
    }
  } catch (e) {
    setTileText(btn, "Failed");
    setTimeout(() => { setTileText(btn, original); }, 3000);
  } finally {
    btn.disabled = false;
    hideScanProgress();
  }
}
el("rescan-library").addEventListener("click", () => runRescan(false));

// ------------------------------------------------------ iPod Classic import --
// A checklist modal instead of squeezing everything into the tile button's
// own label -- the whole thing (detect, copy, review/fix, a whole-library
// rescan, art backfill) can run for several minutes with long stretches
// where nothing changes but the step really is still working, and a single
// small label has no room to make that legible.
//
// Flow: Detect -> copy to a holding folder (MUSIC_DIR/.ipod_staging/<name>,
// see ipod_import.py) -> Review batch (this session's own interactive
// panel: nothing here is in the real library yet, so Fix names/tags/art
// each write straight to the staged files) -> Move to library (chains the
// same rescan + art-backfill the old direct-to-library flow used) ->
// duplicate summary. Never inserted into the tracks table until moved --
// see ipod_import.py's own module docstring for why.
const IPOD_STEP_ICON = { pending: "○", active: "⟳", done: "✓", error: "✕" };
const IPOD_REVIEW_MAX_SHOWN = 200; // cap DOM rows for a huge batch; the count in the heading still reflects the real total

function setIpodStep(key, state, detail) {
  const li = el(`ipod-step-${key}`);
  if (!li) return;
  li.classList.remove("pending", "active", "done", "error");
  li.classList.add(state);
  const icon = li.querySelector(".ipod-step-icon");
  if (icon) icon.textContent = IPOD_STEP_ICON[state] || IPOD_STEP_ICON.pending;
  if (detail !== undefined) {
    const d = el(`ipod-step-${key}-detail`);
    if (d) d.textContent = detail;
  }
}

function resetIpodSteps() {
  ["detect", "copy", "move", "scan", "art"].forEach((k) => setIpodStep(k, "pending", ""));
  el("ipod-library-choice").classList.add("hidden");
  el("ipod-steps").classList.add("hidden");
  el("ipod-move-steps").classList.add("hidden");
  el("ipod-skipped-panel").classList.add("hidden");
  el("ipod-skipped-list").innerHTML = "";
  el("ipod-review-panel").classList.add("hidden");
  el("ipod-review-list").innerHTML = "";
  el("ipod-review-action-status").textContent = "";
  el("ipod-incomplete-banner").classList.add("hidden");
  ipodReviewState.complete = true;
  el("ipod-staging-path").textContent = "";
  el("ipod-summary").classList.add("hidden");
  el("ipod-summary-list").innerHTML = "";
  el("ipod-summary-review-dups").classList.add("hidden");
  el("ipod-skip-prompt").classList.add("hidden");
  hideIpodProgress();
}

function formatAgo(seconds) {
  if (seconds < 90) return "less than a minute ago";
  const mins = Math.round(seconds / 60);
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
  const hours = Math.round(mins / 60);
  return `${hours} hour${hours === 1 ? "" : "s"} ago`;
}

// A rescan this recent is very unlikely to be stale enough to matter --
// asking every single time would just be a click to dismiss on a second
// iPod import minutes after the first one.
const IPOD_SKIP_RESCAN_WITHIN_SECONDS = 3600;

// Resolves true (skip) or false (rescan anyway) once the user picks one.
function askSkipRescan(secondsAgo) {
  return new Promise((resolve) => {
    const prompt = el("ipod-skip-prompt");
    el("ipod-skip-when").textContent = `Your library was already scanned ${formatAgo(secondsAgo)}.`;
    prompt.classList.remove("hidden");
    const cleanup = () => {
      prompt.classList.add("hidden");
      yes.removeEventListener("click", onYes);
      no.removeEventListener("click", onNo);
    };
    const yes = el("ipod-skip-yes");
    const no = el("ipod-skip-no");
    const onYes = () => { cleanup(); resolve(true); };
    const onNo = () => { cleanup(); resolve(false); };
    yes.addEventListener("click", onYes);
    no.addEventListener("click", onNo);
  });
}

function showIpodProgress(done, total) {
  el("ipod-progress").classList.remove("hidden");
  el("ipod-progress-fill").style.width = total ? `${Math.min(100, (done / total) * 100)}%` : "3%";
}
function hideIpodProgress() {
  el("ipod-progress").classList.add("hidden");
  el("ipod-progress-fill").style.width = "0%";
}
function openIpodModal() { el("ipod-backdrop").classList.remove("hidden"); }
function closeIpodModal() { el("ipod-backdrop").classList.add("hidden"); }
el("ipod-close").addEventListener("click", closeIpodModal);
el("ipod-backdrop").addEventListener("click", (e) => { if (e.target.id === "ipod-backdrop") closeIpodModal(); });

function ipodReviewTrackMeta(t) {
  const format = t.ext ? t.ext.replace(/^\./, "").toUpperCase() : null;
  const bitrate = t.bitrate ? `${t.bitrate}kbps` : null;
  return [format, bitrate, t.album, t.genre, t.year, t.duration ? fmtTime(t.duration) : null].filter(Boolean).join(" · ");
}

function renderIpodReviewTrack(t) {
  const row = document.createElement("div");
  row.className = "dup-review-track";
  row.innerHTML = `
    <span class="ipod-review-track-art" title="${t.has_art ? "Has cover art" : "No cover art"}">${t.has_art ? "🖼" : "—"}</span>
    <div class="dup-review-track-info">
      <div class="dup-review-track-title">${escapeHtml(t.artist || "Unknown artist")} — ${escapeHtml(t.title || "")}</div>
      <div class="dup-review-track-meta">${escapeHtml(ipodReviewTrackMeta(t))}</div>
      <div class="dup-review-track-meta">${escapeHtml(t.path || "")}</div>
    </div>
  `;
  return row;
}

// Tracks the copy step skipped outright (never staged) because their
// (artist, title) already matched something in the library -- see
// ipod_import.import_tracks's own docstring. Shown once, right after
// copying, so a bitrate comparison is available before the batch moves on.
function renderIpodSkippedTrack(d) {
  const row = document.createElement("div");
  row.className = "dup-review-track";
  const ipodBetter = d.ipod_bitrate && d.library_bitrate && d.ipod_bitrate > d.library_bitrate * 1.05;
  const libBetter = d.ipod_bitrate && d.library_bitrate && d.library_bitrate > d.ipod_bitrate * 1.05;
  const fmt = (v) => v ? `${v}kbps` : "unknown";
  row.innerHTML = `
    <div class="dup-review-track-info">
      <div class="dup-review-track-title">${escapeHtml(d.artist || "Unknown artist")} — ${escapeHtml(d.title || "")}</div>
      <div class="ipod-skipped-track-bitrates">
        iPod: <span class="${ipodBetter ? "better" : ""}">${fmt(d.ipod_bitrate)}</span>
        · Library: <span class="${libBetter ? "better" : ""}">${fmt(d.library_bitrate)}</span>
      </div>
      <div class="dup-review-track-meta">${escapeHtml(d.library_path || "")}</div>
    </div>
  `;
  return row;
}

// The one active staged batch this modal is currently working with --
// resolved once (from the import result, or from an existing pending
// batch when resuming) and reused for every /ipod/staging/* call after.
// `complete` tracks the last-known copy-completeness (see loadIpodReviewList)
// so syncIpodMoveButton can re-apply the "still copying" gate on
// ipod-move-to-library even after something else (runIpodStagingFix's own
// disable/re-enable around a fix) has touched that button in between.
const ipodReviewState = { ipodName: null, complete: true };

function syncIpodMoveButton() {
  const moveBtn = el("ipod-move-to-library");
  moveBtn.disabled = !ipodReviewState.complete;
  moveBtn.title = ipodReviewState.complete ? "" : "Finish copying first — some tracks from this batch are still missing.";
}

async function loadIpodReviewList() {
  const data = await api(`/ipod/staging?ipod_name=${encodeURIComponent(ipodReviewState.ipodName || "")}`);
  const tracks = data.tracks || [];
  el("ipod-staging-path").textContent = data.staging_path || "";
  const list = el("ipod-review-list");
  list.innerHTML = "";
  tracks.slice(0, IPOD_REVIEW_MAX_SHOWN).forEach((t) => list.appendChild(renderIpodReviewTrack(t)));
  if (tracks.length > IPOD_REVIEW_MAX_SHOWN) {
    const more = document.createElement("div");
    more.className = "ipod-review-more";
    more.textContent = `…and ${tracks.length - IPOD_REVIEW_MAX_SHOWN} more`;
    list.appendChild(more);
  }
  el("ipod-review-heading").textContent = tracks.length
    ? `${tracks.length} track${tracks.length === 1 ? "" : "s"} waiting for review — nothing here is in your library yet.`
    : "Nothing staged to review.";

  // data.complete is false when a previous copy was interrupted partway
  // (see ipod_import.COMPLETE_MARKER) -- `tracks` here is only what made
  // it in before that happened, not the whole batch, so moving it to the
  // library now would silently leave the rest behind for good.
  const banner = el("ipod-incomplete-banner");
  ipodReviewState.complete = data.complete !== false;
  if (!ipodReviewState.complete) {
    el("ipod-incomplete-text").textContent = data.expected_total
      ? `Copying didn't finish last time — ${tracks.length} of ${data.expected_total} tracks copied so far.`
      : `Copying didn't finish last time — ${tracks.length} track${tracks.length === 1 ? "" : "s"} copied so far. Reconnect the iPod to see the full count.`;
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }
  syncIpodMoveButton();
  return tracks.length;
}

el("ipod-continue-copy").addEventListener("click", async () => {
  const btn = el("ipod-continue-copy");
  btn.disabled = true;
  el("ipod-incomplete-text").textContent = "Continuing…";
  try {
    const started = await api("/ipod/staging/continue-import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ipod_name: ipodReviewState.ipodName }),
    });
    if (!started.started) {
      showToast(started.error || "Couldn't continue copying.", { kind: "error" });
      await loadIpodReviewList();
      return;
    }
    showIpodProgress(0, 0);
    const status = await pollProgress("/ipod/import-progress", (s) => {
      el("ipod-incomplete-text").textContent = s.total ? `Continuing — ${s.done} / ${s.total}` : "Continuing…";
      showIpodProgress(s.done, s.total);
      return s.running;
    });
    hideIpodProgress();
    if (status.error) showToast(status.error, { kind: "error" });
    await loadIpodReviewList();
  } catch (e) {
    hideIpodProgress();
    showToast(e.message || "Couldn't continue copying.", { kind: "error" });
  } finally {
    btn.disabled = false;
  }
});
el("ipod-reveal-staging").addEventListener("click", async () => {
  try {
    const result = await api("/ipod/staging/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ipod_name: ipodReviewState.ipodName }),
    });
    if (!result.ok) showToast(result.error || "Couldn't open that folder.", { kind: "error" });
  } catch (e) {
    showToast("Couldn't open that folder.", { kind: "error" });
  }
});

async function runIpodStagingFix(action, label) {
  const buttons = [el("ipod-fix-names"), el("ipod-fix-tags"), el("ipod-fix-art"), el("ipod-move-to-library")];
  buttons.forEach((b) => { b.disabled = true; });
  const status = el("ipod-review-action-status");
  try {
    const started = await api("/ipod/staging/fix", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, ipod_name: ipodReviewState.ipodName }),
    });
    if (!started.started) {
      showToast(started.error || "Couldn't start.", { kind: "error" });
      return;
    }
    const result = await pollProgress("/ipod/staging/fix-progress", (s) => {
      status.textContent = s.total ? `${label}… ${s.done} / ${s.total}` : `${label}…`;
      return s.running;
    });
    if (result.error) {
      showToast(result.error, { kind: "error" });
    } else {
      const r = result.result || {};
      showToast(
        action === "names" ? `Fixed ${r.fixed || 0} of ${r.checked || 0} names.`
        : action === "tags" ? `Filled ${r.fixed_genre || 0} genre and ${r.fixed_year || 0} year tag(s).`
        : `Found art for ${r.fixed || 0} of ${r.needed_art || 0} track(s).`,
        { kind: "success" },
      );
    }
    status.textContent = "";
    await loadIpodReviewList();
  } finally {
    // loadIpodReviewList (above) already set ipod-move-to-library's
    // disabled state correctly, but this blanket re-enable would otherwise
    // clobber it back to enabled if the batch is still incomplete.
    buttons.forEach((b) => { b.disabled = false; });
    syncIpodMoveButton();
  }
}
el("ipod-fix-names").addEventListener("click", () => runIpodStagingFix("names", "Fixing names"));
el("ipod-fix-tags").addEventListener("click", () => runIpodStagingFix("tags", "Filling genre & year"));
el("ipod-fix-art").addEventListener("click", () => runIpodStagingFix("art", "Fetching cover art"));

async function moveIpodStagingToLibrary() {
  el("ipod-skipped-panel").classList.add("hidden");
  el("ipod-review-panel").classList.add("hidden");
  el("ipod-move-steps").classList.remove("hidden");

  setIpodStep("move", "active", "");
  showIpodProgress(0, 0);
  const started = await api("/ipod/staging/move", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ipod_name: ipodReviewState.ipodName }),
  });
  if (!started.started) {
    setIpodStep("move", "error", started.error || "Couldn't start.");
    return;
  }
  let status = await pollProgress("/ipod/staging/move-progress", (s) => {
    setIpodStep("move", "active", s.total ? `${s.done} / ${s.total}` : "");
    showIpodProgress(s.done, s.total);
    return s.running;
  });
  if (status.error) {
    setIpodStep("move", "error", status.error);
    return;
  }
  const moveResult = status.result || {};
  const moved = moveResult.total || 0;
  const duplicates = moveResult.duplicates || [];
  setIpodStep("move", "done", `${moved} track${moved === 1 ? "" : "s"} moved`);

  // A rescan walks the WHOLE library, not just this batch, and can take
  // minutes on a large collection -- if one already ran recently (any
  // trigger: Rescan, Choose Folder, an earlier iPod import), ask before
  // doing it again rather than always paying that cost. Only asks when
  // it's actually recent; an old or missing last-scan time just proceeds
  // straight to rescanning.
  const lastScan = await api("/last-scan");
  let skipRescan = false;
  if (lastScan.seconds_ago != null && lastScan.seconds_ago < IPOD_SKIP_RESCAN_WITHIN_SECONDS) {
    setIpodStep("scan", "pending", "Waiting for you to choose…");
    skipRescan = await askSkipRescan(lastScan.seconds_ago);
  }
  if (skipRescan) {
    setIpodStep("scan", "done", `Skipped — already scanned ${formatAgo(lastScan.seconds_ago)}`);
  } else {
    setIpodStep("scan", "active", "Rescanning your whole library, not just this batch — can take a few minutes.");
    status = await pollProgress("/scan-progress", (s) => {
      setIpodStep("scan", "active", s.total ? `${s.done} / ${s.total}` : "Walking your music folder…");
      showIpodProgress(s.done, s.total);
      return s.running;
    });
    setIpodStep("scan", "done", "");
  }

  // Safety net for anything fix_staged_art didn't catch (skipped, or
  // failed to find a match) -- same _fetch_and_cache_art mechanism as the
  // per-track "Fetch cover art" button.
  const movedPaths = moveResult.moved_paths || [];
  let artFetched = null, artNeeded = null;
  if (movedPaths.length) {
    setIpodStep("art", "active", "");
    const afStarted = await api("/ipod/backfill-art", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: movedPaths }),
    });
    if (afStarted.started || afStarted.error === "Already running") {
      const afStatus = await pollProgress("/ipod/backfill-art-progress", (s) => {
        setIpodStep("art", "active", s.total ? `${s.done} / ${s.total}` : "");
        showIpodProgress(s.done, s.total);
        return s.running;
      });
      const afResult = afStatus.result || {};
      artFetched = afResult.fetched || 0;
      artNeeded = afResult.needed_art || 0;
      setIpodStep("art", afStatus.error ? "error" : "done", afStatus.error || (artNeeded
        ? `Found art for ${artFetched} of ${artNeeded}`
        : "Every moved track already had cover art"));
    } else {
      setIpodStep("art", "error", afStarted.error || "Couldn't start");
    }
  } else {
    setIpodStep("art", "done", "Nothing to check");
  }

  hideIpodProgress();
  await loadFacets();
  await loadTracks(true);

  const list = el("ipod-summary-list");
  const item = (text) => { const li = document.createElement("li"); li.textContent = text; list.appendChild(li); };
  item(`${moved} track${moved === 1 ? "" : "s"} added to your library`);
  if (artNeeded) item(`Cover art found for ${artFetched} of ${artNeeded} tracks still missing it`);
  item(duplicates.length
    ? `${duplicates.length} track${duplicates.length === 1 ? "" : "s"} looked like something already in your library`
    : "None of the moved tracks looked like duplicates");
  if (duplicates.length) {
    const reviewBtn = el("ipod-summary-review-dups");
    reviewBtn.classList.remove("hidden");
    reviewBtn.onclick = () => { closeIpodModal(); el("find-duplicates").click(); };
  }
  el("ipod-summary").classList.remove("hidden");
}
el("ipod-move-to-library").addEventListener("click", () => {
  moveIpodStagingToLibrary().catch(() => {
    showToast("Something went wrong moving the batch into your library.", { kind: "error" });
  });
});

async function enterIpodReview(ipodName) {
  ipodReviewState.ipodName = ipodName;
  hideIpodProgress();
  el("ipod-review-panel").classList.remove("hidden");
  await loadIpodReviewList();
}

async function runIpodImport() {
  // Reset/open already happened in openIpodImportChoice() (the modal was
  // already showing the "where should this go" screen); beginIpodDetection()
  // just revealed the step list this actually drives.
  const btn = el("import-ipod");
  btn.disabled = true;

  // find_ipod() is normally near-instant, but a real Classic's spinning
  // drive over USB occasionally makes a single file check stall for tens
  // of seconds to a few minutes with nothing else to show for it in the
  // meantime (confirmed against a real device: the exact same call was
  // instant most times, once took over 20s) -- this reframes a long wait
  // as expected instead of looking stuck.
  setIpodStep("detect", "active", "");
  const slowNoteTimer = setTimeout(() => {
    setIpodStep("detect", "active", "Still looking — iPods with a spinning drive can take a while to respond over USB.");
  }, 5000);

  try {
    const started = await api("/ipod/import", { method: "POST" });
    clearTimeout(slowNoteTimer);
    // A batch from an earlier import that was never reviewed/moved takes
    // priority -- resume reviewing it instead of starting a second,
    // overlapping one.
    if (!started.started && started.pending && started.pending.length) {
      setIpodStep("detect", "done", "");
      setIpodStep("copy", "done", started.pending[0].complete === false
        ? "A previous copy didn't finish — see below"
        : "Resuming a previous import");
      await enterIpodReview(started.pending[0].name);
      return;
    }
    // "Already running" means a previous click's job is still going (e.g.
    // the modal was closed and reopened) -- re-attach to it below instead
    // of treating it as a failure.
    if (!started.started && started.error !== "Already running") {
      setIpodStep("detect", "error", started.error || "Couldn't find an iPod.");
      return;
    }
    setIpodStep("detect", "done", "");

    setIpodStep("copy", "active", "");
    showIpodProgress(0, 0);
    const status = await pollProgress("/ipod/import-progress", (s) => {
      setIpodStep("copy", "active", s.total ? `${s.done} / ${s.total}` : "");
      showIpodProgress(s.done, s.total);
      return s.running;
    });
    if (status.error) {
      setIpodStep("copy", "error", status.error);
      return;
    }
    const stats = status.result || {};
    const copied = stats.copied || 0;
    const already = stats.already_staged || 0;
    const duplicates = stats.duplicates || [];
    const copyDetail = [
      already ? `${copied} new, ${already} already staged from a previous run` : `${copied} track${copied === 1 ? "" : "s"}`,
      duplicates.length ? `${duplicates.length} skipped as already in your library` : null,
    ].filter(Boolean).join(" — ");
    setIpodStep("copy", "done", copyDetail);

    if (duplicates.length) {
      const list = el("ipod-skipped-list");
      list.innerHTML = "";
      duplicates.forEach((d) => list.appendChild(renderIpodSkippedTrack(d)));
      el("ipod-skipped-heading").textContent =
        `${duplicates.length} track${duplicates.length === 1 ? "" : "s"} skipped — already in your library by artist & title. Never copied, so there's nothing to undo.`;
      el("ipod-skipped-panel").classList.remove("hidden");
    }

    await enterIpodReview(stats.ipod_name);
  } catch (e) {
    hideIpodProgress();
    const active = document.querySelector(".ipod-step.active");
    if (active) active.classList.replace("active", "error");
  } finally {
    btn.disabled = false;
  }
}
// The iPod's own music frequently overlaps heavily with whatever library
// is already open (it was very likely synced FROM it, or something close
// to it, originally) -- see this session's own real-world case, where an
// iPod import merged into the main library needed a large manual
// duplicate cleanup afterward. Asking up front, every time, keeps that a
// deliberate choice instead of a default.
async function openIpodImportChoice() {
  resetIpodSteps();
  openIpodModal();
  el("ipod-library-choice").classList.remove("hidden");
  let name = "current library";
  try {
    const cur = await api("/library/current");
    if (cur && cur.name) name = cur.name;
  } catch (e) { /* leave the generic label */ }
  el("ipod-current-library-name").textContent = name;
}

function beginIpodDetection() {
  el("ipod-library-choice").classList.add("hidden");
  el("ipod-steps").classList.remove("hidden");
  runIpodImport();
}

el("ipod-use-current-library").addEventListener("click", beginIpodDetection);

el("ipod-new-library-for-import").addEventListener("click", async () => {
  const btn = el("ipod-new-library-for-import");
  btn.disabled = true;
  try {
    const result = await api("/library/new", { method: "POST" });
    if (!result.ok) {
      if (!result.cancelled) showToast(result.error || "Couldn't create the library.", { kind: "error" });
      return;
    }
    await loadLibraryName();
    showToast(`Created "${result.name}".`, { kind: "success" });
    beginIpodDetection();
  } catch (e) {
    showToast("Couldn't create the library.", { kind: "error" });
  } finally {
    btn.disabled = false;
  }
});

el("import-ipod").addEventListener("click", openIpodImportChoice);

// -------------------------------------------------------------- tag checker --
// Missing genre/album/year are unambiguous straight from the DB (NULL/empty
// means the tag is actually missing -- unlike artist/title/art, which
// scan_library always backfills from the filename or never checks, so the
// fast scan can't tell a real gap from a normal file that way. The deep
// scan below opens every file directly to answer those three instead.
const TAG_CHECK_LABELS = {
  genre: "Missing genre", album: "Missing album", year: "Missing year",
  artist: "Missing artist tag", title: "Missing title tag", art: "Missing cover art",
};
const TAG_CHECK_TYPES = { genre: "text", album: "text", year: "number", artist: "text", title: "text" };

function renderTagIssueRow(track, key, editable) {
  const row = document.createElement("div");
  row.className = "tag-issue-row";
  const label = TAG_CHECK_LABELS[key] || "";
  const inputHtml = editable
    ? `<input type="${TAG_CHECK_TYPES[key] || "text"}" class="tag-issue-input" placeholder="${label.replace("Missing ", "")}">
       <button class="btn-small tag-issue-save">Save</button>`
    : "";
  row.innerHTML = `
    <div class="tag-issue-info">${escapeHtml(track.artist || "Unknown artist")} — ${escapeHtml(track.title)}</div>
    ${inputHtml}
  `;
  if (editable) {
    row.querySelector(".tag-issue-save").addEventListener("click", async () => {
      const input = row.querySelector(".tag-issue-input");
      const value = input.value.trim();
      if (!value) return;
      const saveBtn = row.querySelector(".tag-issue-save");
      saveBtn.disabled = true;
      saveBtn.textContent = "Saving…";
      try {
        await api(`/tags/${track.id}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ field: key, value }),
        });
        row.classList.add("tag-issue-done");
        input.disabled = true;
        saveBtn.textContent = "Saved";
        loadFacets();
      } catch (e) {
        saveBtn.disabled = false;
        saveBtn.textContent = "Save";
        showToast("Couldn't save that tag.", { kind: "error" });
      }
    });
  }
  return row;
}

// `key === "art"` has no manual fix here (no automatic art source), so its
// rows render as a read-only reference list instead of an editable one.
function renderTagIssueSection(container, key, info, extraBtnLabel, extraBtnHandler) {
  const editable = key !== "art";
  const section = document.createElement("div");
  section.className = "tag-issue-section";
  const extraBtnHtml = extraBtnLabel ? `<button class="btn-small tag-issue-extra">${extraBtnLabel}</button>` : "";
  section.innerHTML = `<div class="tag-issue-header"><span>${TAG_CHECK_LABELS[key]} (${info.count})</span>${extraBtnHtml}</div>`;
  const list = document.createElement("div");
  list.className = "tag-issue-list";
  info.tracks.forEach((t) => list.appendChild(renderTagIssueRow(t, key, editable)));
  section.appendChild(list);
  container.appendChild(section);
  if (extraBtnHandler) {
    section.querySelector(".tag-issue-extra").addEventListener("click", extraBtnHandler);
  }
}

async function scanTags() {
  const checks = ["genre", "album", "year"].filter((k) => el(`check-${k}`).checked);
  el("tags-results").innerHTML = "Scanning…";
  const data = await api("/tags/audit");
  el("tags-results").innerHTML = "";

  let anyIssues = false;
  checks.forEach((key) => {
    const info = data[key];
    if (!info || info.count === 0) return;
    anyIssues = true;
    const extraLabel = key === "genre" ? "Auto-fill via Deezer" : null;
    const extraHandler = key === "genre" ? async (e) => {
      const btn = e.target;
      btn.disabled = true;
      const started = await api("/fill-genres", { method: "POST" });
      if (started.error) {
        btn.disabled = false;
        showToast(started.error === "Already running" ? "A genre lookup is already running." : started.error, { kind: "error" });
        return;
      }
      // Runs in the background on the server (hundreds of Deezer lookups
      // can take several minutes) -- poll instead of one long blocking
      // request, so the button always shows real progress, never a frozen
      // "Looking up…" with no way to tell it apart from actually hanging.
      let status;
      try {
        status = await pollProgress("/fill-genres/progress", (s) => {
          btn.textContent = s.total ? `Looking up… ${s.done}/${s.total} (${s.found} found)` : "Looking up…";
          return s.running;
        });
      } catch (pollErr) {
        btn.textContent = "Auto-fill via Deezer";
        btn.disabled = false;
        showToast(pollErr.message, { kind: "error" });
        return;
      }
      if (status.error) {
        btn.textContent = "Auto-fill via Deezer";
        btn.disabled = false;
        showToast(`Genre lookup failed: ${status.error}`, { kind: "error" });
        return;
      }
      const stats = status.result || {};
      btn.textContent = `Filled ${stats.found || 0}/${stats.checked || 0}`;
      await loadFacets();
      await loadTracks(true);
      setTimeout(scanTags, 1200);
    } : null;
    renderTagIssueSection(el("tags-results"), key, info, extraLabel, extraHandler);
  });

  if (!anyIssues) {
    el("tags-results").innerHTML = `<div class="tags-empty">No issues found for the selected checks.</div>`;
  }
}

async function runDeepScan() {
  const checks = ["artist", "title", "art"].filter((k) => el(`check-${k}`).checked);
  const btn = el("tags-deepscan-btn");
  const original = btn.textContent;
  btn.disabled = true;
  el("tags-deep-results").innerHTML = "";

  const started = await api("/tags/deep-scan", { method: "POST" });
  if (started.error) {
    btn.disabled = false;
    if (started.error !== "Already running") showToast(started.error, { kind: "error" });
    else btn.textContent = "Already running…";
    return;
  }

  let status;
  try {
    // Runs in the background on the server (opening tens of thousands of
    // files can take a while even parallelized) -- poll instead of one
    // long blocking request, so the button always shows real progress
    // instead of a static "this can take a while" with no way to tell
    // it apart from actually hanging.
    status = await pollProgress("/tags/deep-scan/progress", (s) => {
      btn.textContent = s.total ? `Deep scanning… ${s.done}/${s.total}` : "Deep scanning…";
      return s.running;
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    el("tags-deep-results").innerHTML = `<div class="tags-empty">${escapeHtml(e.message)}</div>`;
    return;
  }
  btn.disabled = false;
  btn.textContent = original;

  if (status.error) {
    el("tags-deep-results").innerHTML = `<div class="tags-empty">Deep scan failed: ${escapeHtml(status.error)}</div>`;
    return;
  }
  const data = status.result || {};
  let anyIssues = false;
  checks.forEach((key) => {
    const info = data[key];
    if (!info || info.count === 0) return;
    anyIssues = true;
    const extraLabel = key === "art" ? "Find missing cover art" : null;
    const extraHandler = key === "art" ? async (e) => {
      const btn = e.target;
      btn.disabled = true;
      const started = await api("/fill-art", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_ids: info.tracks.map((t) => t.id) }),
      });
      if (started.error) {
        btn.disabled = false;
        showToast(started.error === "Already running" ? "An art lookup is already running." : started.error, { kind: "error" });
        return;
      }
      // Tries several free sources (Deezer, then iTunes, then
      // MusicBrainz+Cover Art Archive) per track -- can take a while
      // across hundreds of tracks, so poll instead of one long blocking
      // request with no way to tell it apart from stuck.
      let artStatus;
      try {
        artStatus = await pollProgress("/fill-art/progress", (s) => {
          btn.textContent = s.total ? `Looking up… ${s.done}/${s.total} (${s.fixed} found)` : "Looking up…";
          return s.running;
        });
      } catch (pollErr) {
        btn.textContent = "Find missing cover art";
        btn.disabled = false;
        showToast(pollErr.message, { kind: "error" });
        return;
      }
      if (artStatus.error) {
        btn.textContent = "Find missing cover art";
        btn.disabled = false;
        showToast(`Cover art lookup failed: ${artStatus.error}`, { kind: "error" });
        return;
      }
      const stats = artStatus.result || {};
      btn.textContent = `Found ${stats.fixed || 0}/${stats.checked || 0}`;
      await loadFacets();
      await loadTracks(true);
    } : null;
    renderTagIssueSection(el("tags-deep-results"), key, info, extraLabel, extraHandler);
  });
  if (!anyIssues) {
    el("tags-deep-results").innerHTML = `<div class="tags-empty">No issues found for the selected checks.</div>`;
  }
  loadFacets();
}

el("open-tag-checker").addEventListener("click", () => {
  el("tags-backdrop").classList.remove("hidden");
  scanTags();
  refreshVerifyAudioStatus(true);
  // A whole-library run keeps going after this panel is closed -- reattach to it.
  api("/verify-audio/progress").then((s) => { if (s.running && !verifyAudioFollowing) runVerifyAudio(null); }).catch(() => {});
});
el("tags-scan-btn").addEventListener("click", scanTags);
el("tags-deepscan-btn").addEventListener("click", runDeepScan);

el("fix-years-btn").addEventListener("click", async () => {
  const btn = el("fix-years-btn");
  const original = btn.textContent;
  const resultEl = el("fix-years-result");
  btn.disabled = true;
  const started = await api("/fill-years", { method: "POST" });
  if (started.error) {
    btn.disabled = false;
    if (started.error !== "Already running") showToast(started.error, { kind: "error" });
    else resultEl.textContent = "Already running…";
    return;
  }
  let status;
  try {
    status = await pollProgress("/fill-years/progress", (s) => {
      btn.textContent = s.total ? `Checking… ${s.done}/${s.total} (${s.updated} corrected)` : "Checking…";
      return s.running;
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    resultEl.textContent = e.message;
    return;
  }
  btn.disabled = false;
  btn.textContent = original;
  if (status.error) {
    resultEl.textContent = `Failed: ${status.error}`;
    return;
  }
  const stats = status.result || {};
  resultEl.textContent = `Corrected ${stats.updated || 0} of ${stats.checked || 0} tracks.`;
  await loadFacets();
  await loadTracks(true);
});
el("unify-genre-btn").addEventListener("click", async () => {
  const btn = el("unify-genre-btn");
  const original = btn.textContent;
  const resultEl = el("unify-genre-result");
  btn.disabled = true;
  btn.textContent = "Checking…";

  const preview = await api("/unify-artist-genre/preview");
  if (!preview.artists_to_fix) {
    btn.disabled = false;
    btn.textContent = original;
    resultEl.textContent = "Every artist already has a single, consistent genre.";
    return;
  }
  const exampleLines = preview.examples
    .map((e) => `  ${e.artist} → ${e.genre} (${e.tracks} track(s) changing)`)
    .join("\n");
  const confirmed = await customConfirm(
    `This will set ${preview.artists_to_fix.toLocaleString()} artist(s) to their single most common genre, ` +
    `updating ${preview.tracks_to_update.toLocaleString()} track(s) total (both the file's tag and the library). Biggest changes:\n\n${exampleLines}\n\nContinue?`,
    { okLabel: "Continue" }
  );
  if (!confirmed) {
    btn.disabled = false;
    btn.textContent = original;
    return;
  }

  const started = await api("/unify-artist-genre", { method: "POST" });
  if (started.error) {
    btn.disabled = false;
    if (started.error !== "Already running") showToast(started.error, { kind: "error" });
    else resultEl.textContent = "Already running…";
    return;
  }
  let status;
  try {
    status = await pollProgress("/unify-artist-genre/progress", (s) => {
      btn.textContent = s.total ? `Fixing… ${s.done}/${s.total} artists (${s.updated} tracks updated)` : "Fixing…";
      return s.running;
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    resultEl.textContent = e.message;
    return;
  }
  btn.disabled = false;
  btn.textContent = original;
  if (status.error) {
    resultEl.textContent = `Failed: ${status.error}`;
    return;
  }
  const stats = status.result || {};
  resultEl.textContent = stats.artists_fixed
    ? `Fixed ${stats.artists_fixed.toLocaleString()} artist(s), updated ${stats.tracks_updated.toLocaleString()} track(s).`
    : "Every artist already has a single, consistent genre.";
  await loadFacets();
  await loadTracks(true);
});

el("fix-artist-title-btn").addEventListener("click", async () => {
  const btn = el("fix-artist-title-btn");
  const original = btn.textContent;
  const resultEl = el("fix-artist-title-result");
  btn.disabled = true;
  btn.textContent = "Checking a sample…";

  const preview = await api("/fix-artist-title/preview");
  if (!preview.estimated_fixes) {
    btn.disabled = false;
    btn.textContent = original;
    resultEl.textContent = `Checked a sample of ${preview.sampled.toLocaleString()} track(s) — nothing worth correcting found.`;
    return;
  }
  const exampleLines = preview.examples.map((e) => `  ${e.before}  →  ${e.after}`).join("\n");
  const confirmed = await customConfirm(
    `Based on a sample of ${preview.sampled.toLocaleString()} track(s), an estimated ` +
    `${preview.estimated_fixes.toLocaleString()} of ${preview.tracks_checked.toLocaleString()} track(s) have a correction available ` +
    `(both the file's tag and the library get updated). This is an estimate — the real run checks every track, not just the sample. Examples:\n\n${exampleLines}\n\nContinue?`,
    { okLabel: "Continue" }
  );
  if (!confirmed) {
    btn.disabled = false;
    btn.textContent = original;
    return;
  }

  const started = await api("/fix-artist-title", { method: "POST" });
  if (started.error) {
    btn.disabled = false;
    btn.textContent = original;
    if (started.error !== "Already running") showToast(started.error, { kind: "error" });
    else resultEl.textContent = "Already running…";
    return;
  }
  let status;
  try {
    status = await pollProgress("/fix-artist-title/progress", (s) => {
      btn.textContent = s.total ? `Fixing… ${s.done}/${s.total} (${s.updated} corrected)` : "Fixing…";
      return s.running;
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = original;
    resultEl.textContent = e.message;
    return;
  }
  btn.disabled = false;
  btn.textContent = original;
  if (status.error) {
    resultEl.textContent = `Failed: ${status.error}`;
    return;
  }
  const stats = status.result || {};
  resultEl.textContent = `Checked ${stats.checked || 0} track(s), corrected ${stats.updated || 0}.`;
  await loadFacets();
  await loadTracks(true);
});

const VERIFY_AUDIO_ERROR_MESSAGES = {
  fpcalc_missing: "Needs “fpcalc” installed (brew install chromaprint) — same requirement as Live Radio's song ID.",
  no_api_key: "Needs a free AcoustID API key — set one in Live Radio's settings first.",
};

function renderAudioMismatchRow(m) {
  const row = document.createElement("div");
  row.className = "tag-issue-row";
  row.innerHTML = `
    <div class="tag-issue-info">
      ${escapeHtml(m.current_artist || "Unknown artist")} — ${escapeHtml(m.current_title || "")}
      <br><span style="color:var(--accent)">→ ${escapeHtml(m.found_artist)} — ${escapeHtml(m.found_title)}</span>
    </div>
    <button class="btn-small tag-issue-save">Apply</button>
    <button class="btn-small">Dismiss</button>
  `;
  const [applyBtn, dismissBtn] = row.querySelectorAll("button");
  applyBtn.addEventListener("click", async () => {
    applyBtn.disabled = true;
    applyBtn.textContent = "Applying…";
    try {
      await api(`/tags/${m.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ field: "artist", value: m.found_artist }) });
      await api(`/tags/${m.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ field: "title", value: m.found_title }) });
      row.classList.add("tag-issue-done");
      applyBtn.textContent = "Applied";
      dismissBtn.remove();
      await loadFacets();
      await loadTracks(true);
    } catch (e) {
      applyBtn.disabled = false;
      applyBtn.textContent = "Apply";
      showToast("Couldn't apply that fix.", { kind: "error" });
    }
  });
  dismissBtn.addEventListener("click", () => {
    row.remove();
    api("/verify-audio/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: m.id }),
    }).catch(() => {});
  });
  return row;
}

// Shows "N of M verified" and any mismatches from earlier runs (they're kept in
// the library, so a long run's findings are still here the next day).
async function refreshVerifyAudioStatus(loadResults = false) {
  let st;
  try {
    st = await api("/verify-audio/status");
  } catch (e) {
    return;
  }
  el("verify-audio-status-line").textContent = st.total
    ? `${st.verified.toLocaleString()} of ${st.total.toLocaleString()} tracks verified so far` +
      (st.mismatches ? ` · ${st.mismatches.toLocaleString()} possible mismatch(es)` : "")
    : "";
  el("verify-audio-all-btn").textContent = st.verified && st.verified < st.total ? "Continue whole library" : "Verify whole library";
  if (loadResults && st.mismatches && !el("verify-audio-mismatches").children.length) {
    const data = await api("/verify-audio/results").catch(() => null);
    if (data) data.mismatches.forEach((m) => el("verify-audio-mismatches").appendChild(renderAudioMismatchRow(m)));
  }
}

let verifyAudioFollowing = false;
async function runVerifyAudio(trackIds) {
  const resultEl = el("verify-audio-bulk-result");
  const progressEl = el("verify-audio-bulk-progress");
  const progressFill = el("verify-audio-bulk-progress-fill");
  const list = el("verify-audio-mismatches");
  const buttons = [el("verify-audio-bulk-btn"), el("verify-audio-all-btn")];
  list.innerHTML = "";
  resultEl.textContent = "";
  if (trackIds && !trackIds.length) {
    resultEl.textContent = "Select some tracks in the library list first, then come back here.";
    return;
  }
  if (!verifyAudioFollowing) {
    let started;
    try {
      started = await api("/verify-audio", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(trackIds ? { track_ids: trackIds } : {}),
      });
    } catch (e) {
      showToast(e.message, { kind: "error" });
      return;
    }
    if (started.error && started.error !== "Already running") {
      showToast(started.error, { kind: "error" });
      return;
    }
    if (started.error) resultEl.textContent = "Already running — following it…";
  }
  verifyAudioFollowing = true;
  buttons.forEach((b) => { b.disabled = true; });
  el("verify-audio-cancel").disabled = false;
  el("verify-audio-cancel").classList.remove("hidden");
  progressFill.style.width = "0%";
  progressEl.classList.remove("hidden");
  let status;
  try {
    status = await pollProgress("/verify-audio/progress", (s) => {
      el("verify-audio-status-line").textContent = s.total ? `Listening… ${s.done.toLocaleString()} / ${s.total.toLocaleString()}` : "Listening…";
      progressFill.style.width = s.total ? `${Math.min(100, (s.done / s.total) * 100)}%` : "0%";
      return s.running;
    });
  } catch (e) {
    resultEl.textContent = e.message;
    return;
  } finally {
    verifyAudioFollowing = false;
    buttons.forEach((b) => { b.disabled = false; });
    el("verify-audio-cancel").classList.add("hidden");
    progressEl.classList.add("hidden");
    refreshVerifyAudioStatus();
  }
  if (status.error) {
    resultEl.textContent = VERIFY_AUDIO_ERROR_MESSAGES[status.error] || `Failed: ${status.error}`;
    return;
  }
  const stats = status.result || {};
  const mismatches = stats.mismatches || [];
  resultEl.textContent = (status.cancelled ? "Stopped — what was checked is kept. " : "") +
    `Checked ${stats.checked || 0} track(s)` +
    (stats.errors ? `, ${stats.errors} couldn't be identified` : "") +
    (mismatches.length ? `. ${mismatches.length} possible mismatch(es) below.` : " — no mismatches found.");
  mismatches.forEach((m) => list.appendChild(renderAudioMismatchRow(m)));
}

el("verify-audio-bulk-btn").addEventListener("click", () => runVerifyAudio(Array.from(state.selected)));
el("verify-audio-all-btn").addEventListener("click", () => runVerifyAudio(null));
el("verify-audio-cancel").addEventListener("click", async () => {
  el("verify-audio-cancel").disabled = true;
  await api("/jobs/verify_audio/cancel", { method: "POST" }).catch(() => {});
});

el("tags-close").addEventListener("click", () => el("tags-backdrop").classList.add("hidden"));
el("tags-backdrop").addEventListener("click", (e) => { if (e.target.id === "tags-backdrop") el("tags-backdrop").classList.add("hidden"); });

el("choose-folder").addEventListener("click", async () => {
  const btn = el("choose-folder");
  const original = getTileText(btn);
  setTileText(btn, "Choosing…");
  btn.disabled = true;
  try {
    const result = await api("/choose-folder", { method: "POST" });
    if (!result.ok) {
      if (!result.cancelled) showToast(result.error || "Couldn't set that folder.", { kind: "error" });
      return;
    }
    if (!result.started) {
      showToast("Couldn't start the scan (one may already be running).", { kind: "error" });
      setTileText(btn, original);
      return;
    }
    // The backend always (re)scans the picked folder now, even if it's the
    // same one already configured -- re-picking a folder used to be a
    // silent no-op if it matched the saved path, which looked exactly like
    // "nothing happens" to someone re-selecting a drive to make sure it
    // gets indexed. DB wipe (if the folder actually changed) already
    // happened synchronously before this; the scan itself runs in the
    // background, so poll instead of assuming it's done, same as
    // rescan-library above.
    showScanProgress(0, 0);
    const status = await pollProgress("/scan-progress", (s) => {
      setTileText(btn, s.total ? `Scanning… ${s.done}/${s.total}` : "Scanning…");
      showScanProgress(s.done, s.total);
      return s.running;
    });
    clearSelection();
    await loadFacets();
    await loadTracks(true);
    await loadPlaylists();
    await loadCurrentFolder();
    if (status.error) {
      setTileText(btn, "Failed");
      showToast(`Scan failed: ${status.error}`, { kind: "error" });
      setTimeout(() => { setTileText(btn, original); }, 4000);
      return;
    }
    const stats = status.result || {};
    setTileText(btn, `+${stats.inserted || 0} tracks`);
    setTimeout(() => { setTileText(btn, original); }, 4000);
  } catch (e) {
    setTileText(btn, "Failed");
    setTimeout(() => { setTileText(btn, original); }, 3000);
  } finally {
    btn.disabled = false;
    hideScanProgress();
  }
});

// ---------------------------------------------------------------- library --
// Multiple, independent, switchable libraries -- each just one .nbpmlib
// file the user names and saves wherever they like (an external drive,
// anywhere); music files stay put, only the catalog lives in the file. All
// the actual switching logic is server-side (app.py's _switch_library);
// this is the small topbar dropdown (New/Open/Recent) plus the current
// name display.
async function loadLibraryName() {
  try {
    const cur = await api("/library/current");
    el("library-name").textContent = cur.name || "Library";
  } catch (e) { /* leave the placeholder -- not worth a toast on startup */ }
}

function closeLibraryMenu() {
  el("library-menu").classList.add("hidden");
}

async function loadLibraryRecentList() {
  const list = el("library-recent-list");
  list.innerHTML = "";
  let recents = [];
  try {
    recents = await api("/library/recent");
  } catch (e) {
    list.innerHTML = `<div class="library-recent-empty">Couldn't load recent libraries.</div>`;
    return;
  }
  if (!recents.length) {
    list.innerHTML = `<div class="library-recent-empty">No other libraries yet.</div>`;
    return;
  }
  recents.forEach((r) => {
    const item = document.createElement("button");
    item.className = "library-recent-item" + (r.exists ? "" : " missing");
    item.innerHTML = `<span class="name">${escapeHtml(r.name)}</span>` +
      (r.exists ? "" : `<span class="missing-tag">not found</span>`);
    item.title = r.path;
    item.disabled = !r.exists;
    item.addEventListener("click", () => switchToLibrary(r.path));
    list.appendChild(item);
  });
}

async function switchToLibrary(path) {
  closeLibraryMenu();
  showToast("Switching library…", { kind: "info" });
  try {
    const result = await api("/library/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!result.ok) {
      if (!result.cancelled) showToast(result.error || "Couldn't open that library.", { kind: "error" });
      return;
    }
    await afterLibrarySwitch();
  } catch (e) {
    showToast("Couldn't open that library.", { kind: "error" });
  }
}

async function afterLibrarySwitch() {
  clearSelection();
  await loadLibraryName();
  await loadFacets();
  await loadTracks(true);
  await loadPlaylists();
  await loadSmartPlaylists();
  await loadCurrentFolder();
}

el("library-menu-btn").addEventListener("click", async (e) => {
  e.stopPropagation();
  const menu = el("library-menu");
  const opening = menu.classList.contains("hidden");
  menu.classList.toggle("hidden");
  if (opening) await loadLibraryRecentList();
});
document.addEventListener("click", (e) => {
  if (!el("library-control").contains(e.target)) closeLibraryMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeLibraryMenu();
});

el("library-new").addEventListener("click", async () => {
  closeLibraryMenu();
  const btn = el("library-menu-btn");
  btn.disabled = true;
  try {
    const result = await api("/library/new", { method: "POST" });
    if (!result.ok) {
      if (!result.cancelled) showToast(result.error || "Couldn't create the library.", { kind: "error" });
      return;
    }
    if (result.started) {
      showScanProgress(0, 0);
      const status = await pollProgress("/scan-progress", (s) => {
        showScanProgress(s.done, s.total);
        return s.running;
      });
      hideScanProgress();
      if (status.error) showToast(`Scan failed: ${status.error}`, { kind: "error" });
    }
    await afterLibrarySwitch();
    showToast(`Created "${result.name}".`, { kind: "success" });
  } catch (e) {
    showToast("Couldn't create the library.", { kind: "error" });
  } finally {
    btn.disabled = false;
  }
});

el("library-open").addEventListener("click", async () => {
  closeLibraryMenu();
  const btn = el("library-menu-btn");
  btn.disabled = true;
  try {
    const result = await api("/library/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!result.ok) {
      if (!result.cancelled) showToast(result.error || "Couldn't open that library.", { kind: "error" });
      return;
    }
    await afterLibrarySwitch();
    showToast(`Opened "${result.name}".`, { kind: "success" });
  } catch (e) {
    showToast("Couldn't open that library.", { kind: "error" });
  } finally {
    btn.disabled = false;
  }
});

// -------------------------------------------------------------- duplicates --
// One-click cleanup: scan for songs that have both a plain album version and
// a Live/Remastered/Mix version, and offer to remove just the latter. All
// the "which version is which" logic lives server-side (app.py); this is
// just a scan -> confirm -> delete flow with an optional read-only preview.
const dupState = { anyDeleted: false };

async function scanDuplicates() {
  el("dup-summary").textContent = "Scanning your library for duplicates…";
  el("dup-clean-btn").classList.add("hidden");
  el("dup-toggle-details").classList.add("hidden");
  el("dup-details").classList.add("hidden");
  el("dup-details").innerHTML = "";
  delete el("dup-details").dataset.rendered;
  el("dup-review-open").classList.add("hidden");

  // The scan itself is just DB queries and stat() calls -- fast, and over
  // before there'd be anything meaningful to count a percentage against
  // -- so this shows "still working" with a sliding indeterminate bar
  // rather than a real 0-100% progress bar there's no real number for.
  const progressEl = el("dup-progress");
  const progressFill = el("dup-progress-fill");
  progressFill.classList.add("indeterminate");
  progressEl.classList.remove("hidden");

  let preview;
  try {
    preview = await api("/duplicates/auto-clean", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dry_run: true }),
    });
  } finally {
    progressEl.classList.add("hidden");
    progressFill.classList.remove("indeterminate");
  }
  dupState.preview = preview;

  if (preview.tracks_to_delete === 0) {
    el("dup-summary").textContent = preview.groups_skipped > 0
      ? `Found ${preview.groups_skipped.toLocaleString()} duplicate group(s), but none is safe to remove automatically — same song, multiple copies, with nothing to reliably tell which one to keep.`
      : "No duplicates found.";
  } else {
    const parts = [];
    if (preview.groups_cleaned) {
      parts.push(`<b>${preview.groups_cleaned.toLocaleString()}</b> song(s) with a Live, Remastered, or Mix version alongside the album track`);
    }
    if (preview.repeat_groups_cleaned) {
      parts.push(`<b>${preview.repeat_groups_cleaned.toLocaleString()}</b> song(s) downloaded twice into the same folder`);
    }
    if (preview.same_recording_cleaned) {
      parts.push(`<b>${preview.same_recording_cleaned.toLocaleString()}</b> song(s) with identical copies of the same recording (keeping the better-quality one)`);
    }
    el("dup-summary").innerHTML =
      `Found ${parts.join(" and ")}.<br>` +
      `Removing them deletes <b>${preview.tracks_to_delete.toLocaleString()}</b> file(s) — one copy of every song stays.` +
      (preview.groups_skipped
        ? `<br><span class="dup-summary-note">${preview.groups_skipped.toLocaleString()} other duplicate group(s) couldn't be resolved automatically (no plain version to compare against, or the copies differ enough that they might not be the same recording), so they're left alone.</span>`
        : "");

    el("dup-clean-btn").textContent = `Remove ${preview.tracks_to_delete.toLocaleString()} duplicate version(s)`;
    el("dup-clean-btn").classList.remove("hidden");
    el("dup-toggle-details").textContent = "Show details";
    el("dup-toggle-details").classList.remove("hidden");
  }

  if (preview.groups_skipped) {
    el("dup-review-open").textContent = `Review remaining duplicates (${preview.groups_skipped.toLocaleString()})`;
    el("dup-review-open").classList.remove("hidden");
  }
}

function renderDupDetails() {
  const details = el("dup-details");
  if (details.dataset.rendered) return;
  details.innerHTML = (dupState.preview.tracks || [])
    .map((t) => `<div class="dup-detail-row" data-track-id="${t.id}"><span>${escapeHtml(t.artist || "Unknown artist")} — ${escapeHtml(t.title)}</span></div>`)
    .join("");
  details.dataset.rendered = "1";
}

el("dup-toggle-details").addEventListener("click", () => {
  const details = el("dup-details");
  const showing = details.classList.toggle("hidden") === false;
  el("dup-toggle-details").textContent = showing ? "Hide details" : "Show details";
  if (showing) renderDupDetails();
});

el("dup-clean-btn").addEventListener("click", async () => {
  const preview = dupState.preview;
  if (!preview) return;
  const confirmed = await customConfirm(
    `Remove ${preview.tracks_to_delete.toLocaleString()} duplicate file(s)? One copy of every song is kept — removed files go to Trash, not deleted outright.`,
    { okLabel: "Remove", danger: true }
  );
  if (!confirmed) return;

  const btn = el("dup-clean-btn");
  btn.disabled = true;
  btn.textContent = "Removing…";

  // Show the file-by-file list (already built from the preview) alongside
  // the progress bar so removed entries can be struck off it below as soon
  // as the run finishes, instead of the whole thing just vanishing.
  renderDupDetails();
  el("dup-toggle-details").classList.add("hidden");
  el("dup-details").classList.remove("hidden");
  const progressEl = el("dup-progress");
  const progressFill = el("dup-progress-fill");
  progressFill.style.width = "0%";
  progressEl.classList.remove("hidden");

  const started = await api("/duplicates/auto-clean", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: false }),
  });
  if (started.error) {
    btn.disabled = false;
    btn.textContent = `Remove ${preview.tracks_to_delete.toLocaleString()} duplicate version(s)`;
    progressEl.classList.add("hidden");
    el("dup-details").classList.add("hidden");
    el("dup-toggle-details").classList.remove("hidden");
    showToast(started.error === "Already running" ? "A cleanup is already running." : started.error, { kind: "error" });
    return;
  }

  // If the music folder lives on a different drive than the app's own
  // data (an external drive, say), each removal is a real file copy, not
  // a fast rename -- for hundreds of files that can take minutes, so this
  // polls for progress instead of one long blocking request that would
  // otherwise look identical to just being stuck.
  let status;
  try {
    status = await pollProgress("/duplicates/auto-clean/progress", (s) => {
      btn.textContent = s.total ? `Removing… ${s.done}/${s.total}` : "Removing…";
      progressFill.style.width = s.total ? `${Math.min(100, (s.done / s.total) * 100)}%` : "0%";
      return s.running;
    });
  } catch (e) {
    el("dup-summary").textContent = `${e.message} The removal may still be running on the server — reopen this panel in a moment to check.`;
    return;
  }
  dupState.anyDeleted = true;
  btn.classList.add("hidden");
  progressEl.classList.add("hidden");
  if (status.error) {
    el("dup-summary").textContent = `Removal failed: ${status.error}`;
    return;
  }
  const errors = status.errors || [];
  const failedIds = new Set(errors.map((e) => e.track_id));
  // Every previewed track this run didn't report as failed was actually
  // deleted -- strike those rows out of the list rather than hiding the
  // whole thing, and flag the rest with why they're still here.
  el("dup-details").querySelectorAll(".dup-detail-row").forEach((row) => {
    const id = Number(row.dataset.trackId);
    if (failedIds.has(id)) {
      row.classList.add("dup-detail-failed");
      const err = errors.find((e) => e.track_id === id);
      row.querySelector("span").textContent += ` — failed: ${err ? err.error : "unknown error"}`;
    } else {
      row.remove();
    }
  });
  if (!el("dup-details").querySelector(".dup-detail-row")) {
    el("dup-details").classList.add("hidden");
  }
  el("dup-summary").textContent = errors.length
    ? `Removed ${status.deleted.toLocaleString()} file(s), ${errors.length} failed.`
    : `Removed ${status.deleted.toLocaleString()} file(s). One copy of every song was kept.`;
});

// ------------------------------------------------------ duplicate review --
// For the groups neither auto-clean rule could confidently resolve: same
// song, multiple copies, nothing reliable to tell which one to keep. Shows
// each group's tracks side by side (album/year/duration) so a person can
// make that call -- one at a time via the per-row button, or several at
// once by checking boxes and using the bulk bar. Deleting goes through the
// same trash-based /delete-tracks endpoint the rest of the app uses.
const dupReviewState = { offset: 0, total: 0, loading: false, selected: new Set(), source: "meta" };

function dupReviewTrackMeta(t) {
  // Format first -- it's the detail that actually matters for deciding
  // which copy to keep (a lossless FLAC/ALAC/WAV file over a lossy
  // MP3/AAC one of the same song), where album/year/duration mostly just
  // help tell two editions apart rather than rank them.
  const format = t.ext ? t.ext.replace(/^\./, "").toUpperCase() : null;
  return [format, t.kbps ? `${t.kbps} kbps` : null, t.album, t.year, t.duration ? fmtTime(t.duration) : null].filter(Boolean).join(" · ");
}

function updateDupReviewBulkBar() {
  const n = dupReviewState.selected.size;
  el("dup-review-bulk-bar").classList.toggle("hidden", n === 0);
  el("dup-review-selected-count").textContent = `${n} file(s) selected`;
}

function dupReviewForgetTrack(trackId) {
  dupReviewState.selected.delete(trackId);
  updateDupReviewBulkBar();
}

// Shared by the dup-review screen's delete buttons and the library's own
// right-click "Delete" -- /delete-tracks runs in the background with
// progress polling (a cross-filesystem move per file can take a real,
// visible amount of time for more than a few files). `progressIds` lets a
// caller wire this into its own visible progress bar; callers with nowhere
// sensible to show one (a right-click delete of a track or two, over
// almost before a bar could render) just get a toast summary instead.
async function deleteTracksWithProgress(trackIds, progressIds = null) {
  const progressEl = progressIds && el(progressIds.bar);
  const progressFill = progressIds && el(progressIds.fill);
  const started = await api("/delete-tracks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ track_ids: trackIds }),
  });
  if (!started.started) {
    showToast(started.error === "Already running" ? "A deletion is already running." : (started.error || "Couldn't delete."), { kind: "error" });
    return null;
  }
  if (progressEl) {
    progressFill.style.width = "0%";
    progressEl.classList.remove("hidden");
  }
  try {
    const status = await pollProgress("/delete-tracks/progress", (s) => {
      if (progressFill) progressFill.style.width = s.total ? `${Math.min(100, (s.done / s.total) * 100)}%` : "0%";
      return s.running;
    });
    if (!progressEl && status && !status.error) {
      showToast(`Deleted ${status.deleted || 0} track(s).`, { kind: "success" });
    }
    return status;
  } finally {
    if (progressEl) progressEl.classList.add("hidden");
  }
}

function renderDupReviewGroup(group) {
  const card = document.createElement("div");
  card.className = "dup-review-group";
  const first = group.tracks[0] || {};
  card.innerHTML = `<div class="dup-review-group-header">${escapeHtml(group.artist || "Unknown artist")} — ${escapeHtml(group.title || first.title || "")}${group.match ? `<span class="dup-review-match">${escapeHtml(group.match)}</span>` : ""}</div>`;

  const list = document.createElement("div");
  group.tracks.forEach((t) => {
    const row = document.createElement("div");
    row.className = "dup-review-track";
    row.innerHTML = `
      <input type="checkbox" class="dup-review-track-check">
      <div class="dup-review-track-info">
        <div class="dup-review-track-title">${escapeHtml(t.title || "")}</div>
        <div class="dup-review-track-meta">${escapeHtml(dupReviewTrackMeta(t))}</div>
        ${group.match && t.path ? `<div class="dup-review-track-file" title="${escapeHtml(t.path)}">${escapeHtml(t.path.split("/").pop())}</div>` : ""}
      </div>
      <button class="btn-small danger-btn dup-review-delete-btn">🗑 Delete this file</button>
    `;
    const checkbox = row.querySelector(".dup-review-track-check");
    checkbox.addEventListener("change", () => {
      // Never allow every copy in a group to end up checked -- that would
      // wipe the song entirely rather than just trim duplicates.
      const uncheckedLeft = Array.from(list.querySelectorAll(".dup-review-track-check")).filter((c) => !c.checked).length;
      if (checkbox.checked && uncheckedLeft === 0) {
        checkbox.checked = false;
        showToast("Keep at least one copy of each song — uncheck another copy in this group first.", { kind: "info" });
        return;
      }
      if (checkbox.checked) dupReviewState.selected.add(t.id);
      else dupReviewState.selected.delete(t.id);
      updateDupReviewBulkBar();
    });
    row.querySelector(".dup-review-delete-btn").addEventListener("click", async (e) => {
      if (list.children.length <= 1) return; // never delete the last remaining copy
      if (!(await customConfirm(`Delete "${t.title}" (${dupReviewTrackMeta(t) || "no album info"})? Goes to Trash, not deleted outright.`, { okLabel: "Delete", danger: true }))) return;
      const rowBtn = e.target;
      rowBtn.disabled = true;
      const status = await deleteTracksWithProgress([t.id], { bar: "dup-review-progress", fill: "dup-review-progress-fill" });
      if (!status) { rowBtn.disabled = false; return; }
      if (status.error) {
        showToast(`Delete failed: ${status.error}`, { kind: "error" });
        rowBtn.disabled = false;
        return;
      }
      dupState.anyDeleted = true;
      dupReviewForgetTrack(t.id);
      row.remove();
      if (list.children.length <= 1) card.classList.add("dup-review-resolved");
    });
    row.dataset.trackId = t.id;
    if (group.best_id === t.id) row.classList.add("best");
    list.appendChild(row);
  });
  card.appendChild(list);

  const actions = document.createElement("div");
  actions.className = "dup-review-group-actions";
  if (group.best_id != null) {
    const keepBest = document.createElement("button");
    keepBest.className = "btn-small";
    keepBest.textContent = "Select all but the best";
    keepBest.addEventListener("click", () => {
      list.querySelectorAll(".dup-review-track").forEach((row) => {
        const box = row.querySelector(".dup-review-track-check");
        if (Number(row.dataset.trackId) !== group.best_id && !box.checked) {
          box.checked = true;
          box.dispatchEvent(new Event("change"));
        }
      });
    });
    actions.appendChild(keepBest);
  }
  const notDup = document.createElement("button");
  notDup.className = "btn-small";
  notDup.textContent = "Not duplicates";
  notDup.title = "Remember that these are different — they won't be suggested again";
  notDup.addEventListener("click", async () => {
    const ids = Array.from(list.querySelectorAll(".dup-review-track")).map((r) => Number(r.dataset.trackId));
    notDup.disabled = true;
    try {
      await api("/duplicates/dismiss", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ track_ids: ids }),
      });
    } catch (e) {
      notDup.disabled = false;
      showToast(e.message, { kind: "error" });
      return;
    }
    ids.forEach((id) => dupReviewState.selected.delete(id));
    updateDupReviewBulkBar();
    card.remove();
    dupReviewState.total = Math.max(0, dupReviewState.total - 1);
    dupReviewState.offset = Math.max(0, dupReviewState.offset - 1);
    updateDupReviewCount();
  });
  actions.appendChild(notDup);
  card.appendChild(actions);
  el("dup-review-list").appendChild(card);
}

el("dup-review-select-none").addEventListener("click", () => {
  dupReviewState.selected.clear();
  document.querySelectorAll(".dup-review-track-check:checked").forEach((c) => { c.checked = false; });
  updateDupReviewBulkBar();
});

el("dup-review-delete-selected").addEventListener("click", async () => {
  const ids = Array.from(dupReviewState.selected);
  if (!ids.length) return;
  const btn = el("dup-review-delete-selected");
  if (!(await customConfirm(`Delete ${ids.length.toLocaleString()} file(s)? Goes to Trash, not deleted outright.`, { okLabel: "Delete", danger: true }))) return;
  btn.disabled = true;
  btn.textContent = "Deleting…";
  el("dup-review-select-none").disabled = true;
  let status;
  try {
    status = await deleteTracksWithProgress(ids, { bar: "dup-review-progress", fill: "dup-review-progress-fill" });
  } finally {
    btn.disabled = false;
    el("dup-review-select-none").disabled = false;
    btn.textContent = "Delete selected";
  }
  if (!status) return;
  if (status.error) {
    showToast(`Delete failed: ${status.error}`, { kind: "error" });
    return;
  }
  dupState.anyDeleted = true;
  ids.forEach((id) => {
    const row = el("dup-review-list").querySelector(`.dup-review-track[data-track-id="${id}"]`);
    if (!row) return;
    const card = row.closest(".dup-review-group");
    row.remove();
    if (card && card.querySelectorAll(".dup-review-track").length <= 1) card.classList.add("dup-review-resolved");
  });
  dupReviewState.selected.clear();
  updateDupReviewBulkBar();
});

function updateDupReviewCount() {
  el("dup-review-count").textContent = dupReviewState.total
    ? `Showing ${dupReviewState.offset.toLocaleString()} of ${dupReviewState.total.toLocaleString()} group(s)`
    : "";
  if (!dupReviewState.total) {
    el("dup-review-list").innerHTML = `<div class="dup-review-empty">Nothing left to review.</div>`;
  }
  el("dup-review-load-more").classList.toggle("hidden", dupReviewState.offset >= dupReviewState.total);
}

async function loadDupReviewPage() {
  if (dupReviewState.loading) return;
  dupReviewState.loading = true;
  el("dup-review-load-more").textContent = "Loading…";
  try {
    const path = dupReviewState.source === "audio" ? "/audio-dupes/groups" : "/duplicates/review";
    const data = await api(`${path}?limit=20&offset=${dupReviewState.offset}`);
    data.groups.forEach(renderDupReviewGroup);
    dupReviewState.offset += data.groups.length;
    dupReviewState.total = data.total_groups;
    updateDupReviewCount();
  } finally {
    dupReviewState.loading = false;
    el("dup-review-load-more").textContent = "Load more";
  }
}

const DUP_REVIEW_META_INTRO = el("dup-review-intro").textContent;
const DUP_REVIEW_AUDIO_INTRO =
  "These files sound the same — compared by the audio itself, so the names and tags can differ. " +
  "Format and bitrate are shown so you can keep the best copy; the one marked ★ is the likeliest " +
  "keeper. Check the copies you don't want (at least one per song always stays) and delete them " +
  "together or one at a time — deleting sends a file to Trash. If a group isn't really the same " +
  "song, mark it “Not duplicates” and it won't come back.";

function openDupReview(source) {
  dupReviewState.source = source;
  dupReviewState.offset = 0;
  dupReviewState.total = 0;
  dupReviewState.selected.clear();
  el("dup-review-list").innerHTML = "";
  el("dup-review-count").textContent = "";
  el("dup-review-title").textContent = source === "audio" ? "Same recording, different files" : "Review remaining duplicates";
  el("dup-review-intro").textContent = source === "audio" ? DUP_REVIEW_AUDIO_INTRO : DUP_REVIEW_META_INTRO;
  updateDupReviewBulkBar();
  el("dup-review-backdrop").classList.remove("hidden");
  loadDupReviewPage();
}
el("dup-review-open").addEventListener("click", () => openDupReview("meta"));
el("dup-review-load-more").addEventListener("click", loadDupReviewPage);
el("dup-review-close").addEventListener("click", () => {
  el("dup-review-backdrop").classList.add("hidden");
  if (dupState.anyDeleted) {
    loadFacets();
    loadTracks(true);
  }
});
el("dup-review-backdrop").addEventListener("click", (e) => { if (e.target.id === "dup-review-backdrop") el("dup-review-close").click(); });

// ---- by sound: the same recording under different names/tags/encodings ----
async function refreshAudioDupes() {
  let st;
  try {
    st = await api("/audio-dupes/status");
  } catch (e) {
    return;
  }
  const scanBtn = el("dup-audio-scan");
  const parts = [];
  if (!st.fpcalc) {
    parts.push("Needs the free “fpcalc” tool installed (brew install chromaprint) — same requirement as Live Radio's song ID.");
  } else if (st.groups) {
    parts.push(`Found <b>${st.groups.toLocaleString()}</b> group(s) of the same recording.`);
  } else if (st.computed_at) {
    parts.push("Nothing found — no two tracks sound the same.");
  } else {
    parts.push("Finds the same recording even when the file names and tags differ.");
  }
  const left = st.total - st.fingerprinted;
  if (st.fpcalc && st.total) {
    parts.push(`${st.fingerprinted.toLocaleString()} of ${st.total.toLocaleString()} tracks analysed.` +
      (left > 0 ? ` The first run listens to each new track once (about ${Math.max(1, Math.round(left * 0.07 / 60))} min for ${left.toLocaleString()}), in the background.` : ""));
  }
  el("dup-audio-text").innerHTML = parts.join(" ");
  scanBtn.disabled = !st.fpcalc;
  scanBtn.textContent = st.computed_at ? "Run again" : "Find duplicates by sound";
  el("dup-audio-review").textContent = `Review ${st.groups.toLocaleString()} group(s)`;
  el("dup-audio-review").classList.toggle("hidden", !st.groups);
  el("dup-dismissed-note").textContent = st.dismissed_pairs ? `${st.dismissed_pairs.toLocaleString()} pair(s) marked “not duplicates”.` : "";
  el("dup-dismissed-reset").classList.toggle("hidden", !st.dismissed_pairs);
}

let audioDupesFollowing = false;
async function followAudioDupes() {
  if (audioDupesFollowing) return;
  audioDupesFollowing = true;
  const scanBtn = el("dup-audio-scan");
  const fill = el("dup-audio-progress-fill");
  scanBtn.disabled = true;
  el("dup-audio-cancel").disabled = false;
  el("dup-audio-cancel").classList.remove("hidden");
  el("dup-audio-review").classList.add("hidden");
  el("dup-audio-progress").classList.remove("hidden");
  try {
    const status = await pollProgress("/audio-dupes/progress", (s) => {
      if (s.stage === "comparing") {
        el("dup-audio-text").textContent = "Comparing recordings…";
        fill.style.width = s.total ? `${Math.min(100, (s.done / s.total) * 100)}%` : "0%";
      } else {
        el("dup-audio-text").textContent = s.total
          ? `Listening to your library… ${s.done.toLocaleString()} / ${s.total.toLocaleString()}`
          : "Starting…";
        fill.style.width = s.total ? `${Math.min(100, (s.done / s.total) * 100)}%` : "0%";
      }
      return s.running;
    });
    if (status.error) showToast(`Couldn't finish: ${status.error}`, { kind: "error" });
    else if (status.cancelled) showToast("Stopped. What was analysed is kept — run it again to continue.", { kind: "info" });
  } catch (e) {
    showToast(e.message, { kind: "error" });
  } finally {
    audioDupesFollowing = false;
    el("dup-audio-cancel").classList.add("hidden");
    el("dup-audio-progress").classList.add("hidden");
    refreshAudioDupes();
  }
}

el("dup-audio-scan").addEventListener("click", async () => {
  let started;
  try {
    started = await api("/audio-dupes/scan", { method: "POST" });
  } catch (e) {
    showToast(e.message, { kind: "error" });
    return;
  }
  if (!started.started) {
    const msg = started.error === "fpcalc_missing"
      ? "Needs “fpcalc” installed (brew install chromaprint) — same requirement as Live Radio's song ID."
      : (started.error === "Already running" ? "This is already running." : started.error);
    showToast(msg || "Couldn't start.", { kind: "error" });
    if (started.error !== "Already running") return;
  }
  followAudioDupes();
});
el("dup-audio-cancel").addEventListener("click", async () => {
  el("dup-audio-cancel").disabled = true;
  await api("/jobs/audio_dupes/cancel", { method: "POST" }).catch(() => {});
});
el("dup-audio-review").addEventListener("click", () => openDupReview("audio"));
el("dup-dismissed-reset").addEventListener("click", async () => {
  const r = await api("/duplicates/dismissed/clear", { method: "POST" }).catch((e) => { showToast(e.message, { kind: "error" }); return null; });
  if (r) {
    showToast(`${r.cleared.toLocaleString()} pair(s) will be suggested again.`, { kind: "info" });
    scanDuplicates();
    refreshAudioDupes();
  }
});

el("find-duplicates").addEventListener("click", async () => {
  dupState.anyDeleted = false;
  el("dup-backdrop").classList.remove("hidden");
  scanDuplicates();
  await refreshAudioDupes();
  try {
    if ((await api("/audio-dupes/progress")).running) followAudioDupes();  // it kept running after the panel was closed
  } catch (e) { /* ignore */ }
});
function closeDupPanel() {
  el("dup-backdrop").classList.add("hidden");
  if (dupState.anyDeleted) {
    loadFacets();
    loadTracks(true);
  }
}
el("dup-close").addEventListener("click", closeDupPanel);
el("dup-backdrop").addEventListener("click", (e) => { if (e.target.id === "dup-backdrop") closeDupPanel(); });

el("organize-by-artist").addEventListener("click", async () => {
  const btn = el("organize-by-artist");
  const original = getTileText(btn);
  // This physically moves files on disk (not just a DB change) -- worth a
  // real confirmation, same reasoning as the Duplicates tool's own preview
  // step, just via customConfirm instead of a full modal since there's
  // nothing meaningful to preview beforehand (the artist tag IS the plan).
  const ok = await customConfirm(
    "Move every music file on disk into a folder named after its Artist tag?\n\n" +
    "Files with no artist tag go under \"Unknown Artist\". This can take a while on a large library.",
    { okLabel: "Organize" }
  );
  if (!ok) return;

  setTileText(btn, "Organizing…");
  btn.disabled = true;
  try {
    const started = await api("/organize-by-artist", { method: "POST" });
    if (!started.started) {
      showToast(started.error === "Already running" ? "An organize job is already running." : (started.error || "Couldn't start."), { kind: "error" });
      setTileText(btn, original);
      return;
    }
    const status = await pollProgress("/organize-progress", (s) => {
      setTileText(btn, s.total ? `Organizing… ${s.done}/${s.total}` : "Organizing…");
      return s.running;
    });
    if (status.error) {
      setTileText(btn, "Failed");
      showToast(`Organize failed: ${status.error}`, { kind: "error" });
      setTimeout(() => { setTileText(btn, original); }, 4000);
      return;
    }
    const stats = status.result || {};
    await loadFacets();
    await loadTracks(true);
    await loadCurrentFolder();
    setTileText(btn, `Moved ${stats.moved || 0}`);
    if (stats.error_count) {
      showToast(`Organized with ${stats.error_count} error(s) -- some files may not have been readable or writable. Check the console/log for details.`, { kind: "error" });
    }
    setTimeout(() => { setTileText(btn, original); }, 4000);
  } catch (e) {
    setTileText(btn, "Failed");
    setTimeout(() => { setTileText(btn, original); }, 3000);
  } finally {
    btn.disabled = false;
  }
});

// ----------------------------------------------------------------- volume --
(function initVolume() {
  let saved = null;
  try { saved = localStorage.getItem("jukebox-volume"); } catch (e) { /* private browsing */ }
  const vol = saved !== null ? Number(saved) : 100;
  audio.volume = vol / 100;
  el("volume-slider").value = vol;
  el("vinyl-volume").value = vol;
  el("vinyl-volume").style.setProperty("--fill", `${vol}%`);
  updateVolumeIcon(vol);
})();
function updateVolumeIcon(vol) {
  el("volume-icon").textContent = vol == 0 ? "🔇" : vol < 50 ? "🔉" : "🔊";
}
el("volume-slider").addEventListener("input", (e) => {
  const vol = Number(e.target.value);
  audio.volume = vol / 100;
  updateVolumeIcon(vol);
  el("vinyl-volume").value = vol;
  el("vinyl-volume").style.setProperty("--fill", `${vol}%`);
  try { localStorage.setItem("jukebox-volume", vol); } catch (e2) { /* private browsing */ }
});
// The vinyl deck's own volume fader -- a second control surface for the
// same value, same convention as the seek bars (hifi/cassette/vinyl each
// mirroring one canonical value). Proxies through #volume-slider's own
// listener rather than duplicating the audio.volume/icon/localStorage
// logic here.
el("vinyl-volume").addEventListener("input", (e) => {
  el("volume-slider").value = e.target.value;
  el("volume-slider").dispatchEvent(new Event("input"));
});
el("volume-icon").addEventListener("click", () => {
  const slider = el("volume-slider");
  if (Number(slider.value) > 0) {
    slider.dataset.prevVol = slider.value;
    slider.value = 0;
  } else {
    slider.value = slider.dataset.prevVol || 100;
  }
  slider.dispatchEvent(new Event("input"));
});

// ----------------------------------------------------------------- shuffle --
el("shuffle-toggle").addEventListener("click", () => {
  state.shuffle = !state.shuffle;
  el("shuffle-toggle").classList.toggle("active", state.shuffle);
});

// ------------------------------------------------------------------- queue --
// Up-next queue is always FIFO from the front (index 0 plays next); "play
// next" (queueTrackNext, one click on a track row) puts a track at that
// front spot, while the bulk "Add to queue" selection action appends to the
// end, so queueing a handful of songs from a list keeps their order.
function updateQueueBadge() {
  el("queue-btn").textContent = state.queue.length ? `Queue · ${state.queue.length}` : "Queue";
}

function queueTrackNext(track, btn) {
  state.queue.unshift(track);
  updateQueueBadge();
  if (!el("queue-backdrop").classList.contains("hidden")) renderQueue();
  if (btn) {
    const original = btn.textContent;
    btn.textContent = "✓";
    btn.classList.add("col-queue-flash");
    setTimeout(() => {
      btn.textContent = original;
      btn.classList.remove("col-queue-flash");
    }, 700);
  }
}

function renderQueue() {
  const list = el("queue-list");
  if (!state.queue.length) {
    list.innerHTML = `<div class="tags-empty">Nothing queued yet. Click ▸ on a track to play it next, or select tracks and use "Add to queue".</div>`;
    return;
  }
  list.innerHTML = "";
  state.queue.forEach((t, i) => {
    const row = document.createElement("div");
    row.className = "tag-issue-row";
    row.innerHTML = `
      <div class="tag-issue-info">${i + 1}. ${escapeHtml(t.artist || "")} — ${escapeHtml(t.title || "")}</div>
      <button class="btn-small queue-remove">Remove</button>
    `;
    row.querySelector(".queue-remove").addEventListener("click", () => {
      state.queue.splice(i, 1);
      renderQueue();
      updateQueueBadge();
    });
    list.appendChild(row);
  });
}
el("queue-btn").addEventListener("click", () => {
  renderQueue();
  el("queue-backdrop").classList.remove("hidden");
});
el("queue-close").addEventListener("click", () => el("queue-backdrop").classList.add("hidden"));
el("queue-backdrop").addEventListener("click", (e) => { if (e.target.id === "queue-backdrop") el("queue-backdrop").classList.add("hidden"); });
el("queue-clear-btn").addEventListener("click", () => { state.queue = []; renderQueue(); updateQueueBadge(); });

// Central "what plays after this" resolver -- the explicit queue always
// wins, then shuffle (random pick from whatever list is currently browsed).
// Otherwise this plays the literal next track in whichever list is
// currently being browsed/played from (library, a playlist, or a radio
// list all set state.currentList the same way) -- ordinary "hit next, hear
// the next track" behavior, not a recommendation. Only once that list is
// genuinely exhausted (and, in the paginated library view, there's truly
// nothing left to load) does it fall back to similarity-based autoplay,
// and only if that's turned on.
async function playNext() {
  if (state.queue.length) {
    playTrack(state.queue.shift(), null, { addHistory: true });
    updateQueueBadge();
    return;
  }
  if (state.shuffle && state.currentList.length) {
    const pool = state.currentList.filter((t) => !state.currentTrack || t.id !== state.currentTrack.id);
    if (pool.length) {
      playTrack(pool[Math.floor(Math.random() * pool.length)], state.currentList);
      return;
    }
  }

  const idx = state.currentTrack ? state.currentList.findIndex((t) => t.id === state.currentTrack.id) : -1;
  if (idx >= 0 && idx + 1 < state.currentList.length) {
    playTrack(state.currentList[idx + 1], state.currentList);
    return;
  }
  // End of what's loaded -- in the paginated library view (not a playlist
  // or radio list, which load in full up front) there may be more of the
  // same filtered list still to fetch before this is really the end.
  if (idx >= 0 && state.hasMore && el("playlist-view").classList.contains("hidden")) {
    const beforeLen = state.currentList.length;
    await loadTracks(false);
    if (state.currentList.length > beforeLen) {
      playTrack(state.currentList[beforeLen], state.currentList);
      return;
    }
  }

  if (el("autoplay-similar").checked) {
    await playNextSimilar();
  }
}
el("skip-next").addEventListener("click", playNext);
el("prev-track").addEventListener("click", playPrevious);

// -------------------------------------------------------- keyboard shortcuts --
document.addEventListener("keydown", (e) => {
  const tag = (e.target.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable) return;
  // A bare modifier held down turns most of these into something else
  // entirely on the OS/browser level (cmd/ctrl+1..5 switches tabs or
  // desktops in a lot of software) -- worth guarding specifically for the
  // rating keys below, so that isn't misread as "rate this track".
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  switch (e.key) {
    case "1": case "2": case "3": case "4": case "5":
      rateCurrentTrack(Number(e.key));
      break;
    case " ":
      e.preventDefault();
      togglePlayPause();
      break;
    case "ArrowRight":
      if (isFinite(audio.duration)) audio.currentTime = Math.min(audio.duration, audio.currentTime + 5);
      break;
    case "ArrowLeft":
      if (isFinite(audio.duration)) audio.currentTime = Math.max(0, audio.currentTime - 5);
      break;
    case "ArrowUp":
      e.preventDefault();
      el("volume-slider").value = Math.min(100, Number(el("volume-slider").value) + 5);
      el("volume-slider").dispatchEvent(new Event("input"));
      break;
    case "ArrowDown":
      e.preventDefault();
      el("volume-slider").value = Math.max(0, Number(el("volume-slider").value) - 5);
      el("volume-slider").dispatchEvent(new Event("input"));
      break;
    case "n": case "N":
      playNext();
      break;
    case "p": case "P":
      playPrevious();
      break;
    case "s": case "S":
      el("shuffle-toggle").click();
      break;
    case "q": case "Q":
      el("queue-btn").click();
      break;
    case "l": case "L":
      el("lyrics-btn").click();
      break;
    case "m": case "M":
      el("volume-icon").click();
      break;
  }
});

// ------------------------------------------------------------ media session --
if ("mediaSession" in navigator) {
  navigator.mediaSession.setActionHandler("play", () => audio.play());
  navigator.mediaSession.setActionHandler("pause", () => audio.pause());
  navigator.mediaSession.setActionHandler("previoustrack", () => playPrevious());
  navigator.mediaSession.setActionHandler("nexttrack", () => playNext());
  try {
    navigator.mediaSession.setActionHandler("seekto", (details) => {
      if (details.seekTime != null && isFinite(audio.duration)) audio.currentTime = details.seekTime;
    });
  } catch (e) { /* not supported everywhere */ }
}
function updateMediaSession(track, artworkUrl) {
  if (!("mediaSession" in navigator)) return;
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: track.title || "Untitled",
      artist: track.artist || "",
      album: track.album || "",
      artwork: artworkUrl ? [{ src: artworkUrl, sizes: "512x512", type: "image/jpeg" }] : [],
    });
  } catch (e) { /* ignore */ }
}

// -------------------------------------------------------------------- lyrics --
el("lyrics-btn").addEventListener("click", async () => {
  const track = state.currentTrack;
  if (!track) return;
  if (track.isRadio && !track.hasRealMetadata) {
    showToast('Lyrics need to know the actual artist and track first — wait for the station to report it, or use "Identify this song".', { kind: "info" });
    return;
  }
  el("lyrics-title").textContent = `${track.artist || ""} — ${track.title || ""}`;
  el("lyrics-body").innerHTML = "Loading…";
  el("lyrics-backdrop").classList.remove("hidden");
  try {
    const data = track.isRadio
      ? await api(`/lyrics-by-name?artist=${encodeURIComponent(track.artist || "")}&title=${encodeURIComponent(track.title || "")}`)
      : await api(`/lyrics/${track.id}`);
    if (data.instrumental) {
      el("lyrics-body").innerHTML = `<div class="tags-empty">Instrumental — no lyrics.</div>`;
    } else if (data.found) {
      el("lyrics-body").innerHTML = `<pre class="lyrics-text">${escapeHtml(data.lyrics)}</pre>`;
    } else {
      el("lyrics-body").innerHTML = `<div class="tags-empty">No lyrics found for this track.</div>`;
    }
  } catch (e) {
    el("lyrics-body").innerHTML = `<div class="tags-empty">Couldn't load lyrics right now.</div>`;
  }
});
el("lyrics-close").addEventListener("click", () => el("lyrics-backdrop").classList.add("hidden"));
el("lyrics-backdrop").addEventListener("click", (e) => { if (e.target.id === "lyrics-backdrop") el("lyrics-backdrop").classList.add("hidden"); });

// ---------------------------------------------------------------- cover art --
// Shared by the now-playing panel's own fetch button and the library row's
// right-click "Fetch cover art" -- either way, a fetched image needs two
// repaints: the row's <img> (set once at render time, never repaints itself
// just because the DB's has_art flag changed out from under it) and, only
// when the fetched track happens to also be the one currently playing, the
// now-playing panel's own art.
async function fetchArtForTrack(trackId) {
  await api(`/art/${trackId}/fetch`, { method: "POST" });
  const known = state.currentList.find((t) => t.id === trackId);
  if (known) known.has_art = 1;
  const freshUrl = `${artUrl(trackId, true)}&t=${Date.now()}`;
  // Not scoped to #track-list -- the same row markup (and this same fetch
  // path, via the right-click menu) also appears inside #playlist-tracks.
  document.querySelectorAll(`.track-row[data-id="${trackId}"] .col-art`).forEach((cell) => {
    cell.innerHTML = `<img loading="lazy" src="${freshUrl}" alt="">`;
  });
  if (state.currentTrack && state.currentTrack.id === trackId) {
    el("np-art").src = freshUrl;
    el("np-art").classList.remove("hidden");
  }
}

// Bulk equivalent for the right-click menu's "Fetch cover art" over a
// multi-selection -- reuses the same /api/fill-art job the Tag Checker's
// "Find missing cover art" button drives (has_art=0 tracks in the selection
// only, so re-selecting a large chunk of the library doesn't re-hit art
// that's already there). No dedicated progress bar for a context-menu
// action with nowhere obvious to put one, so a single toast is updated in
// place for the life of the job instead of the usual fire-and-forget one.
async function fetchArtForSelection(trackIds) {
  const started = await api("/fill-art", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ track_ids: trackIds }),
  });
  if (started.error) {
    showToast(started.error === "Already running" ? "A cover art lookup is already running." : started.error, { kind: "error" });
    return;
  }
  const toast = showToast(`Looking up cover art… 0/${trackIds.length}`, { duration: 0 });
  let status;
  try {
    status = await pollProgress("/fill-art/progress", (s) => {
      toast.update(s.total ? `Looking up cover art… ${s.done}/${s.total} (${s.fixed} found)` : "Looking up cover art…");
      return s.running;
    });
  } catch (e) {
    toast.remove();
    showToast(e.message, { kind: "error" });
    return;
  }
  toast.remove();
  if (status.error) {
    showToast(`Cover art lookup failed: ${status.error}`, { kind: "error" });
    return;
  }
  const stats = status.result || {};
  showToast(`Found cover art for ${stats.fixed || 0}/${stats.checked || 0} track(s).`, { kind: "success" });
  await loadFacets();
  await loadTracks(true);
}

el("art-fetch-btn").addEventListener("click", async () => {
  if (!state.currentTrack || state.currentTrack.isRadio) return;
  const btn = el("art-fetch-btn");
  btn.disabled = true;
  try {
    await fetchArtForTrack(state.currentTrack.id);
  } catch (e) {
    showToast("Couldn't find cover art for this track.", { kind: "error" });
  } finally {
    btn.disabled = false;
  }
});

// --------------------------------------------------------- internet radio --
// Backed by Radio Browser (radio-browser.info) -- see app.py's radio routes
// for why this carries no copyright exposure (we only look up and play each
// station's own public stream URL, never host/cache any audio ourselves).
let radioFiltersLoaded = false;

function radioStationIcon(station) {
  if (station.favicon) {
    const img = document.createElement("img");
    img.className = "radio-station-favicon";
    img.alt = "";
    img.onerror = () => { img.replaceWith(radioStationIconFallback()); };
    img.src = station.favicon;
    return img;
  }
  return radioStationIconFallback();
}
function radioStationIconFallback() {
  const span = document.createElement("span");
  span.className = "radio-station-favicon-fallback";
  span.textContent = "📡";
  return span;
}

function renderRadioStations(stations) {
  const container = el("radio-results");
  if (!stations.length) {
    container.innerHTML = `<div class="tags-empty">No stations match those filters.</div>`;
    return;
  }
  container.innerHTML = "";
  stations.forEach((station) => {
    const row = document.createElement("div");
    row.className = "radio-station-row";
    row.appendChild(radioStationIcon(station));

    const info = document.createElement("div");
    info.className = "radio-station-info";
    const name = document.createElement("div");
    name.className = "radio-station-name";
    name.textContent = station.name;
    const meta = document.createElement("div");
    meta.className = "radio-station-meta";
    meta.textContent = [station.country, station.bitrate ? `${station.bitrate}kbps` : null, station.tags]
      .filter(Boolean).join(" · ");
    info.append(name, meta);
    row.appendChild(info);

    const playBtn = document.createElement("button");
    playBtn.className = "btn-small radio-station-play";
    playBtn.textContent = "▶";
    playBtn.title = `Play ${station.name}`;
    playBtn.addEventListener("click", () => playRadioStation(station));
    row.appendChild(playBtn);

    container.appendChild(row);
  });
}

async function loadRadioStations() {
  const container = el("radio-results");
  container.innerHTML = `<div class="tags-empty">Loading stations…</div>`;
  const params = new URLSearchParams({
    genre: el("radio-genre").value,
    country: el("radio-country").value,
    sort: el("radio-sort").value,
    limit: "60",
  });
  try {
    const data = await api(`/radio/stations?${params}`);
    if (!data.ok) throw new Error(data.error || "Couldn't load stations");
    renderRadioStations(data.stations);
  } catch (e) {
    container.innerHTML = `<div class="tags-empty">Couldn't reach the radio directory. Check your connection and try again.</div>`;
  }
}

async function ensureRadioFiltersLoaded() {
  if (radioFiltersLoaded) return;
  radioFiltersLoaded = true;
  try {
    const genres = await api("/radio/genres");
    const genreSel = el("radio-genre");
    genres.forEach((g) => {
      const opt = document.createElement("option");
      opt.value = g;
      opt.textContent = g.replace(/\b\w/g, (c) => c.toUpperCase());
      genreSel.appendChild(opt);
    });
  } catch (e) { /* genre list is a nice-to-have -- "Any genre" still works */ }
  try {
    const data = await api("/radio/countries");
    const countrySel = el("radio-country");
    (data.countries || []).forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.code;
      opt.textContent = c.name;
      countrySel.appendChild(opt);
    });
  } catch (e) { /* country list is a nice-to-have -- "Any location" still works */ }
}

el("open-internet-radio").addEventListener("click", async () => {
  el("radio-backdrop").classList.remove("hidden");
  await ensureRadioFiltersLoaded();
  loadRadioStations();
});
el("radio-close").addEventListener("click", () => el("radio-backdrop").classList.add("hidden"));
el("radio-backdrop").addEventListener("click", (e) => { if (e.target.id === "radio-backdrop") el("radio-backdrop").classList.add("hidden"); });
function clearRadioMatchMsg() { el("radio-match-msg").textContent = ""; }
el("radio-genre").addEventListener("change", () => { clearRadioMatchMsg(); loadRadioStations(); });
el("radio-country").addEventListener("change", () => { clearRadioMatchMsg(); loadRadioStations(); });
el("radio-sort").addEventListener("change", loadRadioStations);

el("radio-match-btn").addEventListener("click", async () => {
  const btn = el("radio-match-btn");
  const msg = el("radio-match-msg");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Matching…";
  msg.textContent = "";
  try {
    const data = await api("/radio/match");
    if (!data.ok) {
      msg.textContent = data.error === "no_match"
        ? "Couldn't match your library's genres to a radio category -- try filtering by genre yourself instead."
        : "Couldn't reach the radio directory right now.";
      el("radio-results").innerHTML = "";
      return;
    }
    // Reflects the match in the ordinary filter dropdowns too, so it's
    // obvious what's being shown and easy to tweak from there.
    el("radio-genre").value = data.matched_tag;
    el("radio-country").value = "";
    msg.textContent = `Because your library leans on "${data.library_genre}", here's live radio playing similar music.`;
    renderRadioStations(data.stations);
  } catch (e) {
    msg.textContent = "Couldn't reach the radio directory right now.";
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

let radioNowPlayingTimer = null;
function stopRadioNowPlayingPolling() {
  if (radioNowPlayingTimer) {
    clearInterval(radioNowPlayingTimer);
    radioNowPlayingTimer = null;
  }
}

function applyRadioNowPlayingDisplay(pseudo, favicon) {
  el("np-title").textContent = pseudo.title;
  el("np-sub").textContent = pseudo.artist;
  refreshPlayingHighlight();
  themeOnTrackChange(pseudo, favicon || null);
  updateMediaSession(pseudo, favicon || null);
}

el("radio-identify-btn").addEventListener("click", async () => {
  const track = state.currentTrack;
  if (!track || !track.isRadio || !track.streamUrl) return;
  const btn = el("radio-identify-btn");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "…";
  try {
    const result = await api("/radio/identify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: track.streamUrl }),
    });
    if (result.ok) {
      track.artist = result.artist || track.artist;
      track.title = result.title || track.title;
      track.hasRealMetadata = true;
      applyRadioNowPlayingDisplay(track, el("np-art").classList.contains("hidden") ? null : el("np-art").src);
    } else if (result.error === "no_api_key") {
      const key = prompt(
        "Song identification needs a free AcoustID API key (the same free, open lookup MusicBrainz Picard uses).\n\n" +
        "Get one in under a minute at acoustid.org/new-application, then paste it here:"
      );
      if (key && key.trim()) {
        await api("/radio/acoustid-key", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ acoustidApiKey: key.trim() }),
        });
        showToast("Saved — click Identify again to try now.", { kind: "success" });
      }
    } else if (result.error === "fpcalc_missing") {
      showToast('Song identification needs "chromaprint" installed. Run `brew install chromaprint` in Terminal, then restart the app.', { kind: "error" });
    } else if (result.error === "no_match") {
      showToast("Couldn't identify this song — no confident match found (about 15 seconds of audio was checked).", { kind: "info" });
    } else {
      showToast(`Couldn't identify this song: ${result.error || "unknown error"}`, { kind: "error" });
    }
  } catch (e) {
    showToast("Couldn't reach the identification service.", { kind: "error" });
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

function playRadioStation(station) {
  stopRadioNowPlayingPolling();
  const pseudo = {
    id: null,
    isRadio: true,
    title: station.name || "Unnamed station",
    artist: [station.tags, station.country].filter(Boolean).join(" · ") || "Internet Radio",
    rating: 0,
    streamUrl: station.url,
    // True once we know the ACTUAL on-air artist/title (from ICY metadata
    // or Identify), not just the station's own name/tags -- lyrics lookup
    // needs a real artist/title, not a station label, to mean anything.
    hasRealMetadata: false,
  };
  state.currentTrack = pseudo;
  // Not pushed into state.history/playHistory -- there's no "similar
  // tracks"/next for a live stream, and this keeps Previous falling back
  // to whatever real track was last playing rather than another station.

  // Routed through our own /api/radio/proxy rather than station.url
  // directly -- makes the stream same-origin (some browsers otherwise
  // refuse cross-origin audio outright) and it's the only way to see each
  // station's ICY "now playing" metadata at all, since that's interleaved
  // inside the audio bytes themselves and browsers give JS no access to it
  // directly. (The VU meter/spectum bars do NOT come from this stream via
  // Web Audio -- see startRadioLevelsPolling for why and where those
  // actually come from.)
  const sid = Math.random().toString(36).slice(2);
  audio.src = `${API}/radio/proxy?url=${encodeURIComponent(station.url)}&sid=${sid}`;
  audio.play().catch(() => {
    showToast(`Couldn't play "${pseudo.title}" — the stream may be offline right now.`, { kind: "error" });
  });
  startRadioLevelsPolling(sid);

  const artEl = el("np-art");
  if (station.favicon) {
    artEl.classList.remove("hidden");
    artEl.onerror = () => artEl.classList.add("hidden");
    artEl.src = station.favicon;
  } else {
    artEl.classList.add("hidden");
  }
  const artFetchBtn = el("art-fetch-btn");
  if (artFetchBtn) artFetchBtn.classList.add("hidden");
  updateStars(0);
  el("time-display").textContent = "🔴 LIVE";
  el("radio-identify-btn").classList.remove("hidden");
  applyRadioNowPlayingDisplay(pseudo, station.favicon);

  document.querySelectorAll(".radio-station-row").forEach((row) => row.classList.remove("playing"));
  const rows = [...el("radio-results").children];
  const idx = rows.findIndex((row) => row.querySelector(".radio-station-name")?.textContent === station.name);
  if (idx >= 0) rows[idx].classList.add("playing");

  // Best-effort popularity signal back to Radio Browser -- never blocks
  // playback, which has already started against the URL we already have.
  if (station.uuid) api(`/radio/click/${station.uuid}`, { method: "POST" }).catch(() => {});

  // Polls for the actual on-air artist/track (parsed server-side from ICY
  // metadata) so the theme displays what's really playing instead of just
  // sitting on the station's own name/tags the whole time.
  let lastTitle = null;
  radioNowPlayingTimer = setInterval(async () => {
    if (state.currentTrack !== pseudo) { stopRadioNowPlayingPolling(); return; }
    let data;
    try {
      data = await api(`/radio/now-playing?sid=${sid}`);
    } catch (e) {
      return;
    }
    const raw = (data.title || "").trim();
    if (!raw || raw === lastTitle) return;
    lastTitle = raw;
    // Most stations send "Artist - Title"; anything else (ads, slogans,
    // stations that just repeat their own name) is shown as-is rather than
    // guessed at.
    const parts = raw.split(" - ");
    if (parts.length >= 2 && parts[0].trim() && parts.slice(1).join(" - ").trim()) {
      pseudo.artist = parts[0].trim();
      pseudo.title = parts.slice(1).join(" - ").trim();
      pseudo.hasRealMetadata = true;
    } else {
      // Didn't parse into "Artist - Title" (a slogan, an ad, the station
      // just repeating its own name) -- shown as-is, but not reliable
      // enough to treat as real metadata for a lyrics lookup.
      pseudo.title = raw;
      pseudo.artist = station.name;
    }
    applyRadioNowPlayingDisplay(pseudo, station.favicon);
  }, 4000);
}

// ----------------------------------------------------------------------- trash --
async function renderTrash() {
  const items = await api("/trash");
  const list = el("trash-list");
  if (!items.length) {
    list.innerHTML = `<div class="tags-empty">Trash is empty.</div>`;
    return;
  }
  list.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "tag-issue-row";
    row.innerHTML = `
      <div class="tag-issue-info">${escapeHtml(item.artist || "Unknown artist")} — ${escapeHtml(item.title || item.original_path)}</div>
      <button class="btn-small trash-restore">Restore</button>
      <button class="btn-small danger-btn trash-purge">Delete forever</button>
    `;
    row.querySelector(".trash-restore").addEventListener("click", async () => {
      try {
        await api(`/trash/${item.id}/restore`, { method: "POST" });
        await loadFacets();
        await loadTracks(true);
        renderTrash();
      } catch (e) {
        showToast("Couldn't restore that file — a file may already exist at its original location.", { kind: "error" });
      }
    });
    row.querySelector(".trash-purge").addEventListener("click", async () => {
      if (!(await customConfirm(`Permanently delete "${item.title || item.original_path}"? This cannot be undone.`, { okLabel: "Delete forever", danger: true }))) return;
      await api(`/trash/${item.id}`, { method: "DELETE" });
      renderTrash();
    });
    list.appendChild(row);
  });
}
el("open-trash").addEventListener("click", () => {
  el("trash-backdrop").classList.remove("hidden");
  renderTrash();
});
el("trash-close").addEventListener("click", () => el("trash-backdrop").classList.add("hidden"));
el("trash-backdrop").addEventListener("click", (e) => { if (e.target.id === "trash-backdrop") el("trash-backdrop").classList.add("hidden"); });
el("trash-empty-btn").addEventListener("click", async () => {
  if (!(await customConfirm("Permanently delete everything in the trash? This cannot be undone.", { okLabel: "Empty trash", danger: true }))) return;
  const result = await api("/trash/empty", { method: "POST" });
  showToast(`Permanently deleted ${result.purged} file(s).`, { kind: "success" });
  renderTrash();
});

// ----------------------------------------------------------------------- stats --
el("open-stats").addEventListener("click", async () => {
  el("stats-backdrop").classList.remove("hidden");
  el("stats-body").innerHTML = "Loading…";
  const data = await api("/stats");
  const rowsHtml = (items, render) => items.length
    ? items.map(render).join("")
    : `<div class="tag-issue-row"><div class="tag-issue-info">Nothing played yet.</div></div>`;
  let html = `<p class="dup-summary-text"><b>${data.total_plays.toLocaleString()}</b> total plays logged.</p>`;
  html += `<div class="tag-issue-section"><div class="tag-issue-header"><span>Most played</span></div><div class="tag-issue-list">`;
  html += rowsHtml(data.top_tracks, (t) => `<div class="tag-issue-row"><div class="tag-issue-info">${escapeHtml(t.artist || "")} — ${escapeHtml(t.title || "")}</div><span>${t.play_count}×</span></div>`);
  html += `</div></div>`;
  html += `<div class="tag-issue-section"><div class="tag-issue-header"><span>Top artists</span></div><div class="tag-issue-list">`;
  html += rowsHtml(data.top_artists, (a) => `<div class="tag-issue-row"><div class="tag-issue-info">${escapeHtml(a.artist || "Unknown")}</div><span>${a.plays}×</span></div>`);
  html += `</div></div>`;
  el("stats-body").innerHTML = html;
});
el("stats-close").addEventListener("click", () => el("stats-backdrop").classList.add("hidden"));
el("stats-backdrop").addEventListener("click", (e) => { if (e.target.id === "stats-backdrop") el("stats-backdrop").classList.add("hidden"); });

// -------------------------------------------------------------------- help --
el("open-help").addEventListener("click", () => el("help-backdrop").classList.remove("hidden"));
el("help-close").addEventListener("click", () => el("help-backdrop").classList.add("hidden"));
el("help-backdrop").addEventListener("click", (e) => { if (e.target.id === "help-backdrop") el("help-backdrop").classList.add("hidden"); });

// ------------------------------------------------------------------- about --
el("open-about").addEventListener("click", () => el("about-backdrop").classList.remove("hidden"));
el("about-close").addEventListener("click", () => el("about-backdrop").classList.add("hidden"));
el("about-backdrop").addEventListener("click", (e) => { if (e.target.id === "about-backdrop") el("about-backdrop").classList.add("hidden"); });

// A plain target="_blank" link is unreliable inside pywebview's native
// window (it can silently fail, or navigate the app's own window away from
// itself instead of opening a real browser) -- routed through the same
// native-bridge-with-browser-tab-fallback pattern as save_export, shared by
// both the About panel's and the topbar's donate links.
function openExternalLink(e) {
  const url = e.currentTarget.href;
  if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external_url) {
    e.preventDefault();
    window.pywebview.api.open_external_url(url);
  }
  // else: plain browser tab, let the normal <a href> navigation happen.
}
el("about-donate-link").addEventListener("click", openExternalLink);
el("topbar-donate-link").addEventListener("click", openExternalLink);

// --------------------------------------------------------------- export/import --
el("open-export").addEventListener("click", () => {
  el("import-result").textContent = "";
  el("export-backdrop").classList.remove("hidden");
});
el("export-close").addEventListener("click", () => el("export-backdrop").classList.add("hidden"));
el("export-backdrop").addEventListener("click", (e) => { if (e.target.id === "export-backdrop") el("export-backdrop").classList.add("hidden"); });
el("export-btn").addEventListener("click", async () => {
  const data = await api("/export");
  const json = JSON.stringify(data, null, 2);
  // A plain Blob+<a download> (the whole of this handler, before) silently
  // does nothing in pywebview's WKWebView -- it never wires up the delegate
  // methods a real download needs, so the button just looked broken with
  // no error at all. Go through the native bridge for a real Save dialog
  // there; a plain browser tab (no bridge) keeps using the Blob approach,
  // which works fine there.
  if (window.pywebview && window.pywebview.api && window.pywebview.api.save_export) {
    const result = await window.pywebview.api.save_export(json);
    if (!result.ok && !result.cancelled) showToast(result.error || "Couldn't save the backup.", { kind: "error" });
    return;
  }
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "notorious-bpm-backup.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});
el("import-file-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch (err) {
    el("import-result").textContent = "That doesn't look like a valid export file.";
    return;
  }
  const result = await api("/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  el("import-result").textContent = `Applied ${result.ratings_applied} rating(s), created ${result.playlists_created} playlist(s).`;
  await loadPlaylists();
  await loadTracks(true);
});

// --------------------------------------------------------- smart playlists --
async function loadSmartPlaylists() {
  const items = await api("/smart-playlists");
  const list = el("smart-playlist-list");
  list.innerHTML = "";
  items.forEach((p) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="pl-name">🧠 ${escapeHtml(p.name)}</span>
      <button class="pl-delete" title="Delete smart playlist">🗑</button>
    `;
    li.addEventListener("click", () => openSmartPlaylist(p.id));
    li.querySelector(".pl-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!(await customConfirm(`Delete smart playlist "${p.name}"?`, { okLabel: "Delete", danger: true }))) return;
      await api(`/smart-playlists/${p.id}`, { method: "DELETE" });
      await loadSmartPlaylists();
    });
    list.appendChild(li);
  });
}
async function openSmartPlaylist(id) {
  const data = await api(`/smart-playlists/${id}/tracks`);
  state.activePlaylistId = null;
  state.activeSmartPlaylistId = id;
  el("library-view").classList.add("hidden");
  el("playlist-view").classList.remove("hidden");
  el("playlist-view-title").textContent = `🧠 ${data.name}`;
  clearSelection();
  renderTrackList(el("playlist-tracks"), data.tracks, { showAdd: false });
  state.currentList = data.tracks;
  el("delete-playlist").textContent = "Delete smart playlist";
  el("delete-playlist").classList.add("danger");
  el("delete-playlist").onclick = async () => {
    if (!(await customConfirm(`Delete smart playlist "${data.name}"?`, { okLabel: "Delete", danger: true }))) return;
    await api(`/smart-playlists/${id}`, { method: "DELETE" });
    state.activeSmartPlaylistId = null;
    el("playlist-view").classList.add("hidden");
    el("library-view").classList.remove("hidden");
    loadSmartPlaylists();
  };
}
el("new-smart-playlist").addEventListener("click", () => {
  el("smart-name").value = "";
  el("smart-rating").value = "";
  el("smart-genre").value = "";
  el("smart-decade").value = "";
  el("smart-playlist-backdrop").classList.remove("hidden");
});
el("smart-playlist-close").addEventListener("click", () => el("smart-playlist-backdrop").classList.add("hidden"));
el("smart-playlist-backdrop").addEventListener("click", (e) => { if (e.target.id === "smart-playlist-backdrop") el("smart-playlist-backdrop").classList.add("hidden"); });
el("smart-playlist-create-btn").addEventListener("click", async () => {
  const name = el("smart-name").value.trim();
  if (!name) { showToast("Give the playlist a name first.", { kind: "info" }); return; }
  const rules = {};
  if (el("smart-rating").value) rules.rating_min = el("smart-rating").value;
  if (el("smart-genre").value) rules.genre = el("smart-genre").value;
  if (el("smart-decade").value) rules.decade = el("smart-decade").value;
  await api("/smart-playlists", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, rules }),
  });
  el("smart-playlist-backdrop").classList.add("hidden");
  await loadSmartPlaylists();
});

// ---------------------------------------------------------- bulk selection --
el("selection-queue").addEventListener("click", () => {
  const ids = new Set(state.selected);
  const tracks = state.currentList.filter((t) => ids.has(t.id));
  tracks.forEach((t) => state.queue.push(t));
  renderQueue();
  updateQueueBadge();
  showToast(`Added ${tracks.length} track(s) to the queue.`, { kind: "success" });
});
el("selection-edit-tags").addEventListener("click", () => {
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  // Artist/title default to the single selected track's own current
  // values (editing them only makes sense one track at a time -- with
  // several selected, showing the first one's values as a starting point
  // and leaving them blank/unchanged for the others would be confusing,
  // so those two fields are hidden once more than one track is selected).
  const single = ids.length === 1 ? state.currentList.find((t) => t.id === ids[0]) : null;
  // "Fix artist & track names" only confirms the current tags name a
  // real, existing song -- equally true whether or not this file's audio
  // actually is that song, so a file mislabeled with a different real
  // song's tags passes that check. Verifying the audio itself (Chromaprint/
  // AcoustID, same tools/account as Live Radio's song-ID feature) catches
  // that instead -- pre-fills Artist/Title with what it finds so accepting
  // the fix is just clicking OK.
  const artistTitleHtml = single ? `
    <div class="filter-group" style="margin-bottom:10px"><label>Artist</label><input type="text" id="bulk-tag-artist" value="${escapeHtml(single.artist || "")}"></div>
    <div class="filter-group" style="margin-bottom:6px"><label>Title</label><input type="text" id="bulk-tag-title" value="${escapeHtml(single.title || "")}"></div>
    <div style="margin-bottom:10px">
      <button type="button" id="verify-audio-btn" class="btn-small">🎧 Verify against audio</button>
      <span id="verify-audio-status" class="dup-summary-note"></span>
    </div>
  ` : "";
  const body = `
    <p style="color:var(--text-dim);font-size:12px;margin:0 0 12px">${ids.length} track(s) selected — blank fields are left unchanged.</p>
    ${artistTitleHtml}
    <div class="filter-group" style="margin-bottom:10px"><label>Genre</label><input type="text" id="bulk-tag-genre"></div>
    <div class="filter-group" style="margin-bottom:10px"><label>Album</label><input type="text" id="bulk-tag-album"></div>
    <div class="filter-group"><label>Year</label><input type="text" id="bulk-tag-year"></div>
  `;
  openModal("Edit tags for selected tracks", body, async () => {
    const fields = { genre: el("bulk-tag-genre").value.trim(), album: el("bulk-tag-album").value.trim(), year: el("bulk-tag-year").value.trim() };
    if (single) {
      fields.artist = el("bulk-tag-artist").value.trim();
      fields.title = el("bulk-tag-title").value.trim();
    }
    let applied = 0;
    for (const id of ids) {
      for (const [field, value] of Object.entries(fields)) {
        if (!value) continue;
        try {
          await api(`/tags/${id}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ field, value }),
          });
          applied++;
        } catch (e) { /* keep going for the rest of the selection */ }
      }
    }
    closeModal();
    await loadFacets();
    await loadTracks(true);
    showToast(`Applied ${applied} tag update(s).`, { kind: "success" });
  });
  if (single) {
    el("verify-audio-btn").addEventListener("click", async () => {
      const btn = el("verify-audio-btn");
      const status = el("verify-audio-status");
      btn.disabled = true;
      status.textContent = "Listening…";
      try {
        const result = await api(`/tracks/${single.id}/verify-audio`, { method: "POST" });
        if (!result.ok) {
          const messages = {
            fpcalc_missing: "Needs “fpcalc” installed (brew install chromaprint) — same requirement as Live Radio's song ID.",
            no_api_key: "Needs a free AcoustID API key — set one in Live Radio's settings first.",
            no_match: "Couldn't identify this recording.",
          };
          status.textContent = messages[result.error] || `Couldn't verify: ${result.error}`;
          return;
        }
        if (result.mismatched) {
          el("bulk-tag-artist").value = result.found_artist;
          el("bulk-tag-title").value = result.found_title;
          status.textContent = `Audio matches "${result.found_artist} — ${result.found_title}" instead — fields updated, click OK to apply.`;
        } else {
          status.textContent = "Audio matches the current tags.";
        }
      } catch (e) {
        status.textContent = "Couldn't verify (network error).";
      } finally {
        btn.disabled = false;
      }
    });
  }
});

async function loadCurrentFolder() {
  const data = await api("/config");
  const el2 = el("current-folder");
  el2.textContent = data.music_dir || "No folder set";
  el2.title = data.music_dir || "";
}

// ------------------------------------------------------------------- init --
(async function init() {
  // The server-saved theme (see /api/theme) is authoritative when present
  // -- it's what actually survives a relaunch of the pywebview desktop
  // app, unlike localStorage there. Falls back to whatever localStorage
  // already gave state.theme above if the request fails for any reason.
  try {
    const saved = await api("/theme");
    if (saved && saved.theme) state.theme = saved.theme;
  } catch (e) { /* offline/first run -- localStorage-derived default stands */ }
  try {
    const savedWood = await api("/wood-finish");
    if (savedWood && savedWood.woodFinish) state.woodFinish = savedWood.woodFinish;
  } catch (e) { /* offline/first run -- localStorage-derived default stands */ }
  try {
    const savedCasDesign = await api("/cassette-design");
    if (savedCasDesign && savedCasDesign.cassetteDesign) state.cassetteDesign = savedCasDesign.cassetteDesign;
  } catch (e) { /* offline/first run -- localStorage-derived default stands */ }
  try {
    const savedVuColor = await api("/vu-color");
    if (savedVuColor && savedVuColor.vuColor) state.vuColor = savedVuColor.vuColor;
  } catch (e) { /* offline/first run -- localStorage-derived default stands */ }
  try {
    const savedLayout = await api("/layout-mode");
    if (savedLayout && savedLayout.layoutMode) state.layoutMode = savedLayout.layoutMode;
  } catch (e) { /* offline/first run -- localStorage-derived default stands */ }
  applyLayoutMode(state.layoutMode);
  applyTheme(state.theme);
  applyWoodFinish(state.woodFinish);
  applyCassetteDesign(state.cassetteDesign);
  applyVuColor(state.vuColor);
  ensureCasVisBars();
  await loadFacets();
  await loadPlaylists();
  await loadSmartPlaylists();
  await loadTracks();
  await loadCurrentFolder();
  await loadLibraryName();
})();
