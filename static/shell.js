// The 1.3 shell: View menu, sortable column headers, filter chips, tag-checker
// tabs, bulk rate/delete, "go to artist/album", the auto-play toggle and the
// About version line. Loaded last (shares app.js's globals: state, el, api,
// showToast, loadTracks, clearSelection, customConfirm, escapeHtml, ...).

// -------------------------------------------------------------- View menu --
// Layout, theme and its finish used to take a permanent row of the header.
// They're set once in a while, so they live behind one button now.
function closeViewMenu() {
  el("view-menu").classList.add("hidden");
  el("view-menu-btn").setAttribute("aria-expanded", "false");
}
el("view-menu-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const open = el("view-menu").classList.toggle("hidden") === false;
  el("view-menu-btn").setAttribute("aria-expanded", String(open));
  if (open) syncViewMenuFinish();
});
document.addEventListener("click", (e) => { if (!e.target.closest("#view-menu")) closeViewMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeViewMenu(); });

// The "Finish" row only exists for themes that have one (wood, shell, VU colour).
function syncViewMenuFinish() {
  const any = ["wood-select-wrap", "cassette-design-select-wrap", "vu-color-select-wrap"]
    .some((id) => !el(id).classList.contains("hidden"));
  document.querySelector(".view-menu-finish").classList.toggle("hidden", !any);
}
el("theme-select").addEventListener("change", () => setTimeout(syncViewMenuFinish, 0));

// ------------------------------------------------- ratings inside the files --
(async function initRatingsInFiles() {
  const box = el("ratings-in-files");
  try { box.checked = (await api("/settings/ratings-in-files")).enabled; } catch (e) { /* leave it off */ }
  box.addEventListener("change", async () => {
    const want = box.checked;
    try {
      const r = await api("/settings/ratings-in-files", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: want }),
      });
      if (r.ok === false) throw new Error(r.error || "Couldn't change the setting.");
      if (!want) { showToast("Ratings stay in the library only from now on. Ratings already written into files are left there.", { kind: "info", duration: 6000 }); return; }
      showToast("Writing your ratings into the music files…", { kind: "info" });
      if (r.started) {
        const status = await pollProgress("/settings/ratings-in-files/progress", (s) => s.running);
        const res = (status && status.result) || {};
        showToast(`Ratings saved in ${(res.written || 0).toLocaleString()} file(s)` +
          (res.adopted ? `, and ${res.adopted.toLocaleString()} rating(s) found in files were brought in.` : "."), { kind: "success", duration: 6000 });
        if (res.adopted) loadTracks(true);
      }
    } catch (e) {
      box.checked = !want;
      showToast(e.message, { kind: "error" });
    }
  });
})();

// ---------------------------------------------------------- column headers --
// Click a heading to sort by it; click it again to reverse.
function updateTrackHead() {
  document.querySelectorAll("#track-head .th").forEach((th) => {
    const on = th.dataset.sort === state.sort;
    th.classList.toggle("sorted", on);
    const natural = th.dataset.sort === "rating" ? "desc" : "asc";
    const dir = on ? (state.sortDir || natural) : "";
    th.dataset.dir = dir;
    th.setAttribute("aria-sort", !on ? "none" : dir === "asc" ? "ascending" : "descending");
  });
  if ([...el("sort").options].some((o) => o.value === state.sort)) el("sort").value = state.sort;
}
el("track-head").addEventListener("click", (e) => {
  const th = e.target.closest(".th");
  if (!th) return;
  const natural = th.dataset.sort === "rating" ? "desc" : "asc";
  if (state.sort === th.dataset.sort) {
    state.sortDir = (state.sortDir || natural) === "asc" ? "desc" : "asc";
  } else {
    state.sort = th.dataset.sort;
    state.sortDir = "";
  }
  updateTrackHead();
  loadTracks(true);
});
updateTrackHead();

// The headings belong to the track list only -- not the album/artist grids.
function syncTrackHeadVisibility() {
  el("track-head").classList.toggle("hidden", state.view !== "tracks");
}
document.querySelectorAll(".view-tab").forEach((b) => b.addEventListener("click", () => setTimeout(syncTrackHeadVisibility, 0)));
new MutationObserver(syncTrackHeadVisibility).observe(el("grid-view"), { attributes: true, attributeFilter: ["class"] });

// ------------------------------------------------------------ filter chips --
// Genre and Decade were list boxes that needed a Cmd-click to pick a second
// value. The <select> stays as the thing the rest of the app reads; the chips
// are its face: click to toggle, as many as you like.
function setUpChips(selectId) {
  const select = el(selectId);
  const box = document.createElement("div");
  box.className = "chip-list";
  box.setAttribute("role", "group");
  box.setAttribute("aria-label", selectId === "filter-genre" ? "Genre" : "Decade");
  select.classList.add("chips-source");
  select.insertAdjacentElement("afterend", box);

  const render = () => {
    const options = [...select.options].filter((o) => o.value);
    box.innerHTML = options.length ? "" : `<span class="chip-empty">None yet</span>`;
    options.forEach((o) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (o.selected ? " on" : "");
      chip.setAttribute("aria-pressed", String(o.selected));
      chip.textContent = o.textContent;
      chip.addEventListener("click", () => {
        o.selected = !o.selected;
        select.dispatchEvent(new Event("change"));
      });
      box.appendChild(chip);
    });
  };
  select.addEventListener("change", render);
  new MutationObserver(() => setTimeout(render, 0)).observe(select, { childList: true });
  el("clear-filters").addEventListener("click", () => setTimeout(render, 0));
  render();
}
setUpChips("filter-genre");
setUpChips("filter-decade");

// -------------------------------------------------------- tag checker tabs --
// Six tools in one long scroll became six tabs. The markup is untouched (every
// id the tag checker's own script uses is where it was); the sections between
// the dividers are simply wrapped and shown one at a time.
(function tabTagChecker() {
  const body = document.querySelector("#tags-backdrop .tags-body");
  if (!body) return;
  const labels = ["Missing tags", "Deep scan", "Release years", "Genres", "Names", "Check by sound"];
  const panels = [[]];
  [...body.children].forEach((node) => {
    if (node.classList.contains("tags-deep-divider")) { node.remove(); panels.push([]); } else panels[panels.length - 1].push(node);
  });
  const strip = document.createElement("div");
  strip.className = "tabs-strip";
  strip.setAttribute("role", "tablist");
  const sections = panels.map((nodes, i) => {
    const section = document.createElement("section");
    section.className = "tabs-panel";
    section.setAttribute("role", "tabpanel");
    nodes.forEach((n) => section.appendChild(n));
    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = "tabs-tab";
    tab.setAttribute("role", "tab");
    tab.textContent = labels[i] || `Tool ${i + 1}`;
    tab.addEventListener("click", () => show(i));
    strip.appendChild(tab);
    body.appendChild(section);
    return section;
  });
  function show(index) {
    sections.forEach((s, i) => s.classList.toggle("hidden", i !== index));
    [...strip.children].forEach((t, i) => { t.classList.toggle("active", i === index); t.setAttribute("aria-selected", String(i === index)); });
    body.scrollTop = 0;
  }
  body.insertBefore(strip, body.firstChild);
  show(0);
})();

// ------------------------------------------------ selection: rate & delete --
async function rateSelection(ids, rating) {
  if (!ids.length) return;
  try {
    const r = await api("/rate-tracks", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ track_ids: ids, rating }),
    });
    const idSet = new Set(ids);
    [state.libraryRows, state.currentList, state.queue].forEach((list) => (list || []).forEach((t) => { if (idSet.has(t.id)) t.rating = rating || null; }));
    if (state.currentTrack && idSet.has(state.currentTrack.id)) { state.currentTrack.rating = rating || null; updateStars(rating); }
    syncSelectionView();
    showToast(rating ? `Rated ${r.updated.toLocaleString()} track(s) ${"★".repeat(rating)}.` : `Cleared the rating on ${r.updated.toLocaleString()} track(s).`, { kind: "success" });
  } catch (e) {
    showToast(e.message, { kind: "error" });
  }
}
el("selection-rate").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rate]");
  if (b) rateSelection([...state.selected], Number(b.dataset.rate));
});
// Light the stars up to the one under the pointer, like the row stars do.
document.querySelectorAll(".selection-rate, .context-menu-rate").forEach((group) => {
  group.addEventListener("mouseover", (e) => {
    const b = e.target.closest("[data-rate]");
    const n = b ? Number(b.dataset.rate) : 0;
    group.querySelectorAll("[data-rate]").forEach((s) => s.classList.toggle("lit", Number(s.dataset.rate) > 0 && Number(s.dataset.rate) <= n));
  });
  group.addEventListener("mouseleave", () => group.querySelectorAll(".lit").forEach((s) => s.classList.remove("lit")));
});

el("selection-delete").addEventListener("click", async () => {
  const ids = [...state.selected];
  if (!ids.length) return;
  const ok = await customConfirm(
    ids.length > 1 ? `Move ${ids.length.toLocaleString()} tracks to Trash?\n\nThey can be restored from Trash.` : "Move this track to Trash?\n\nIt can be restored from Trash.",
    { okLabel: "Delete", danger: true });
  if (!ok) return;
  const status = await deleteTracksWithProgress(ids);
  if (status && !status.error) {
    clearSelection();
    await loadFacets();
    await loadTracks(true);
  }
});

// ------------------------------------- right-click: rate, go to artist/album --
// showTrackContextMenu (app.js) collapses the selection to the clicked row or
// keeps a multi-selection, so by the time the menu is up, state.selected is
// what these act on; the row under the pointer supplies artist and album.
let ctxTrack = null;
document.addEventListener("contextmenu", (e) => {
  const row = e.target.closest(".track-row");
  if (!row) return;
  const id = Number(row.dataset.id);
  ctxTrack = (state.libraryRows || []).find((t) => t.id === id) || (state.currentList || []).find((t) => t.id === id) || null;
  el("ctx-go-artist").disabled = !(ctxTrack && ctxTrack.artist);
  el("ctx-go-album").disabled = !(ctxTrack && ctxTrack.album);
}, true);

function goToFilter({ artist, album }) {
  hideTrackContextMenu();
  clearSelection();
  if (!el("playlist-view").classList.contains("hidden")) el("back-to-library").click();
  state.artist = artist || "";
  state.album = album || "";
  el("filter-artist").value = state.artist;
  if (state.view !== "tracks") setBrowseView("tracks");
  else { updateBrowseChrome(); loadTracks(true); }
  el("main-scroll").scrollTop = 0;
}
el("ctx-go-artist").addEventListener("click", () => ctxTrack && goToFilter({ artist: ctxTrack.artist }));
el("ctx-go-album").addEventListener("click", () => ctxTrack && goToFilter({ artist: ctxTrack.artist, album: ctxTrack.album }));
el("ctx-rate").addEventListener("click", (e) => {
  const b = e.target.closest("[data-rate]");
  if (!b) return;
  const ids = [...state.selected];
  hideTrackContextMenu();
  rateSelection(ids.length ? ids : (ctxTrack ? [ctxTrack.id] : []), Number(b.dataset.rate));
});

// -------------------------------------------------------- auto-play toggle --
// Was a labelled checkbox in the middle of the transport. The checkbox is
// still what everything reads (hidden); this button is its face.
function syncAutoplayBtn() {
  const on = el("autoplay-similar").checked;
  el("autoplay-btn").classList.toggle("active", on);
  el("autoplay-btn").setAttribute("aria-pressed", String(on));
  el("autoplay-btn").title = on
    ? "Auto-play is on: when the list ends, similar tracks keep playing"
    : "Auto-play similar tracks when the list ends";
}
el("autoplay-btn").addEventListener("click", () => {
  const box = el("autoplay-similar");
  box.checked = !box.checked;
  box.dispatchEvent(new Event("change"));
  showToast(box.checked ? "Auto-play on: similar tracks follow when the list ends." : "Auto-play off.", { kind: "info", duration: 2200 });
});
el("autoplay-similar").addEventListener("change", syncAutoplayBtn);
syncAutoplayBtn();

// ------------------------------------------------- About: version, updates --
let aboutLoaded = false;
async function loadAboutVersion() {
  try {
    if (!aboutLoaded) {
      el("about-version-number").textContent = (await api("/about")).version;
      aboutLoaded = true;
    }
    const note = el("about-update");
    note.textContent = "· checking for updates…";
    const u = await api("/update-check");
    if (!u.ok) { note.textContent = "· couldn't check for updates (offline?)"; return; }
    if (u.newer) {
      note.innerHTML = `· <b>${escapeHtml(u.latest)}</b> is available — <a id="about-update-link" href="${escapeHtml(u.url)}" rel="noopener noreferrer">download</a>`;
      el("about-update-link").addEventListener("click", openExternalLink);
    } else {
      note.textContent = "· up to date";
    }
  } catch (e) {
    el("about-update").textContent = "";
  }
}

// Once per new release, a quiet word at launch that there's an update.
setTimeout(async () => {
  try {
    const u = await api("/update-check");
    if (!u.ok || !u.newer) return;
    let told = null;
    try { told = localStorage.getItem("jukebox-update-told"); } catch (e) { /* fine */ }
    if (told === u.latest) return;
    try { localStorage.setItem("jukebox-update-told", u.latest); } catch (e) { /* fine */ }
    showToast(`Notorious B.P.M. ${u.latest} is available — see About to download it.`, { kind: "info", duration: 9000 });
  } catch (e) { /* offline: nothing to say */ }
}, 15000);
