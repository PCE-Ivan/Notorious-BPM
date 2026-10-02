// Duplicates: one-click cleanup, manual review, and finding the same recording by sound.
// Split out of app.js; loaded after it and sharing its globals (state, el, api, showToast, ...).

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
