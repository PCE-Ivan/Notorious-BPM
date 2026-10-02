// Adding music from outside the library: drop files or folders on the window
// (the desktop shell catches the drop and hands over the real paths -- see
// desktop_macos.py; a web page can't learn them), or use the Library menu.
// Loaded after activity.js (shares el, api, showToast, refreshActivity).

// ---------------------------------------------------------- drop overlay --
// Purely visual here, plus stopping the browser from navigating to the file.
let dragDepth = 0;
const hasFiles = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");

window.addEventListener("dragenter", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth++;
  el("drop-overlay").classList.remove("hidden");
});
window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
window.addEventListener("dragleave", (e) => {
  if (!hasFiles(e)) return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) el("drop-overlay").classList.add("hidden");
});
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  dragDepth = 0;
  el("drop-overlay").classList.add("hidden");
  if (window.pywebview) {
    // The shell sends the paths to the server itself; just make the Activity
    // tray notice the new job quickly.
    setTimeout(refreshActivity, 700);
    setTimeout(refreshActivity, 2000);
  } else {
    showToast("Dropping files works in the desktop app. Here, use the Library menu ▸ Add music.", { kind: "info" });
  }
});

// ---------------------------------------------------------- library menu --
async function addMusic(kind) {
  closeLibraryMenu();
  try {
    const r = await api("/import/choose", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind }) });
    if (r.cancelled) return;
    if (!r.started) {
      showToast(r.error === "Already running" ? "Already adding music — see Activity." : (r.error || "Couldn't add that."), { kind: "error" });
      return;
    }
    showToast("Adding music — see Activity for progress.", { kind: "info" });
    refreshActivity();
  } catch (e) {
    showToast(e.message, { kind: "error" });
  }
}
el("library-add-files").addEventListener("click", () => addMusic("files"));
el("library-add-folder").addEventListener("click", () => addMusic("folder"));

// --------------------------------------------------- when an import ends --
// activity.js calls this as the "import" job leaves the running list. The
// import itself starts a rescan (its own job) to index what it copied; once
// that finishes, show the new tracks.
let importAwaitingScan = false;

async function onImportFinished() {
  let r;
  try {
    r = await api("/import/progress");
  } catch (e) {
    return;
  }
  if (r.error) {
    showToast(`Adding music failed: ${r.error}`, { kind: "error" });
    return;
  }
  if (r.cancelled) {
    showToast("Stopped adding music. What was copied so far is kept.", { kind: "info" });
  }
  const res = r.result || {};
  const added = (res.copied || []).length;
  const dupes = (res.duplicates || []).length;
  const bits = [`${added} track${added === 1 ? "" : "s"} added`];
  if (dupes) bits.push(`${dupes} already in your library`);
  if (res.in_library) bits.push(`${res.in_library} already in the music folder`);
  if (res.errors && res.errors.length) bits.push(`${res.errors.length} couldn't be copied`);
  showToast(bits.join(" · "), { kind: res.errors && res.errors.length ? "error" : "success" });
  importAwaitingScan = !!(added || res.in_library);
}

async function onScanFinishedAfterImport() {
  importAwaitingScan = false;
  await loadFacets();
  await loadTracks(true);
  if (typeof loadPlaylists === "function") loadPlaylists();
}
