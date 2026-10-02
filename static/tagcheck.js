// Tag checker: missing-tag scan, deep scan, bulk lookups (genre, year, artist/title), audio verification.
// Split out of app.js; loaded after it and sharing its globals (state, el, api, showToast, ...).

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
