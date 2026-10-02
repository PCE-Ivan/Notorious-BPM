// Albums and Artists as cover grids. Loaded after app.js (shares its globals:
// state, el, api, artUrl, playTrack, loadTracks, escapeHtml, showToast).
//
// The grids honour every filter the track list does (search, genre, decade,
// artist...). Clicking a card doesn't open a second kind of list: it sets the
// album/artist filter and switches back to Tracks, so selecting, rating,
// right-click and playback all work exactly as they do everywhere else.

const browse = {
  items: [], total: 0, offset: 0, hasMore: true, loading: false, token: 0,
  sort: { albums: "artist", artists: "name" },
};

const GRID_SORTS = {
  albums: [["artist", "Artist"], ["album", "Album"], ["year", "Newest"], ["count", "Most tracks"]],
  artists: [["name", "Name"], ["count", "Most tracks"]],
};

function setBrowseView(view) {
  if (state.view === view) return;
  state.view = view;
  document.querySelectorAll(".view-tab").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  const grid = view !== "tracks";
  el("track-list").classList.toggle("hidden", grid);
  el("grid-view").classList.toggle("hidden", !grid);
  el("grid-sort").classList.toggle("hidden", !grid);
  if (grid) {
    // An album filter would show a grid of one album -- the grids are for
    // choosing one. (The artist filter stays: "this artist's albums" is useful.)
    state.album = "";
    const sel = el("grid-sort");
    sel.innerHTML = GRID_SORTS[view].map(([v, label]) => `<option value="${v}">${label}</option>`).join("");
    sel.value = browse.sort[view];
    reloadGrid();
  } else {
    el("grid-view").innerHTML = "";
    el("load-sentinel").textContent = "";
    loadTracks(true);
  }
  updateBrowseChrome();
}

function updateBrowseChrome() {
  const chip = el("album-chip");
  if (state.album) {
    chip.innerHTML = `<span>Album: ${escapeHtml(state.album)}${state.artist ? ` — ${escapeHtml(state.artist)}` : ""}</span><button title="Show all tracks" aria-label="Clear album filter">✕</button>`;
    chip.classList.remove("hidden");
  } else {
    chip.classList.add("hidden");
    chip.innerHTML = "";
  }
}

el("album-chip").addEventListener("click", (e) => {
  if (!e.target.closest("button")) return;
  state.album = "";
  state.artist = "";
  el("filter-artist").value = "";
  updateBrowseChrome();
  loadTracks(true);
});

document.querySelectorAll(".view-tab").forEach((b) => b.addEventListener("click", () => setBrowseView(b.dataset.view)));
el("grid-sort").addEventListener("change", (e) => {
  browse.sort[state.view] = e.target.value;
  reloadGrid();
});

// ---------------------------------------------------------------- loading --
function reloadGrid() {
  browse.items = [];
  browse.offset = 0;
  browse.hasMore = true;
  browse.token++;
  el("grid-view").innerHTML = "";
  loadGridPage();
}

async function loadGridPage() {
  if (!browse.hasMore || browse.loading || state.view === "tracks") return;
  browse.loading = true;
  const token = browse.token;
  const kind = state.view;
  el("load-sentinel").textContent = browse.offset ? "Loading more…" : "";
  try {
    const data = await api(`/${kind}?${trackQueryParams({ sort: browse.sort[kind], limit: 120, offset: browse.offset })}`);
    if (token !== browse.token) return;  // the filters/view changed while this was in flight
    browse.total = data.total;
    data.items.forEach((it) => el("grid-view").appendChild(gridCard(kind, it)));
    browse.items.push(...data.items);
    browse.offset += data.items.length;
    browse.hasMore = browse.offset < browse.total;
    if (!browse.total) {
      el("grid-view").innerHTML = `<div class="empty-state"><div class="empty-state-title">Nothing here</div>` +
        `<div class="empty-state-hint">${kind === "albums" ? "No albums match the current filters — tracks without an album tag aren't shown here." : "No artists match the current filters."}</div></div>`;
    }
    el("load-sentinel").textContent = browse.hasMore ? "" : (browse.total ? `${browse.total.toLocaleString()} ${kind} — end of list` : "");
  } catch (e) {
    showToast(e.message, { kind: "error" });
  } finally {
    browse.loading = false;
  }
  // Few cards (or a tall window) can leave no scrollbar to trigger the next page.
  const c = el("main-scroll");
  if (browse.hasMore && token === browse.token && c.scrollHeight <= c.clientHeight + 200) loadGridPage();
}

function browseScroll() {
  const c = el("main-scroll");
  if (c.scrollTop + c.clientHeight > c.scrollHeight - 600) loadGridPage();
}

// ------------------------------------------------------------------ cards --
function gridCard(kind, it) {
  const card = document.createElement("div");
  card.className = "grid-card";
  card.tabIndex = 0;
  const artId = it.art_id || it.maybe_id;
  const title = kind === "albums" ? it.album : it.artist;
  const sub = kind === "albums"
    ? (it.artist || "Unknown artist")
    : (it.albums ? `${it.albums.toLocaleString()} album${it.albums === 1 ? "" : "s"}` : "Singles");
  const meta = kind === "albums"
    ? [it.year, `${it.n} track${it.n === 1 ? "" : "s"}`].filter(Boolean).join(" · ")
    : `${it.n.toLocaleString()} track${it.n === 1 ? "" : "s"}`;
  card.innerHTML = `
    <div class="grid-art">
      <div class="grid-art-fallback">♪</div>
      ${artId ? `<img loading="lazy" src="${artUrl(artId, true)}" alt="" onerror="this.remove()">` : ""}
      <button class="grid-play" title="Play" aria-label="Play">▶</button>
    </div>
    <div class="grid-title" title="${escapeHtml(title || "")}">${escapeHtml(title || "")}</div>
    <div class="grid-sub" title="${escapeHtml(sub)}">${escapeHtml(sub)}</div>
    <div class="grid-meta">${escapeHtml(meta)}</div>`;
  card.addEventListener("click", (e) => {
    if (e.target.closest(".grid-play")) {
      e.stopPropagation();
      playGroup(kind, it);
    } else {
      openGroup(kind, it);
    }
  });
  card.addEventListener("keydown", (e) => { if (e.key === "Enter") openGroup(kind, it); });
  return card;
}

function openGroup(kind, it) {
  if (kind === "albums") {
    state.album = it.album;
    state.artist = it.artist || "";
  } else {
    state.album = "";
    state.artist = it.artist;
  }
  el("filter-artist").value = state.artist;
  setBrowseView("tracks");
}

async function playGroup(kind, it) {
  try {
    const p = new URLSearchParams({ sort: "title", limit: 500, offset: 0 });
    if (kind === "albums") p.set("album", it.album);
    if (it.artist) p.set("artist", it.artist);
    const data = await api(`/tracks?${p}`);
    if (!data.tracks.length) return;
    playTrack(data.tracks[0], data.tracks);
  } catch (e) {
    showToast(e.message, { kind: "error" });
  }
}
