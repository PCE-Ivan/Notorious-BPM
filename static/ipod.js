// Import from an iPod Classic: detect, copy, review/fix, move into the library.
// Split out of app.js; loaded after it and sharing its globals (state, el, api, showToast, ...).

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
