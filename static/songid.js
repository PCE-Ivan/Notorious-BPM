// Setting the AcoustID key ("Song ID key"). Loaded after app.js (shares el,
// api, showToast, openExternalLink).
//
// The key used to be asked for with a bare browser prompt, and only from the
// radio's Identify button; the tag checker just said "set one in Live Radio's
// settings", which never existed. Now there is one dialog: it says where to
// get a key, checks the pasted one with AcoustID before saving, and opens by
// itself wherever a key turns out to be missing -- then carries on with what
// you were doing (openSongIdSetup(retry)).

const songId = { onSaved: null, signup: "https://acoustid.org/new-application" };

function songIdMessage(text, kind) {
  const m = el("songid-message");
  m.textContent = text || "";
  m.className = "songid-message" + (kind ? ` ${kind}` : "");
}

async function refreshSongIdStatus() {
  const status = el("songid-status");
  const opener = el("songid-open");
  try {
    const s = await api("/radio/acoustid-key");
    songId.signup = s.signup || songId.signup;
    status.textContent = s.set ? `✓ A key is saved${s.hint ? ` (ending …${s.hint})` : ""}. Paste a new one below to replace it.` : "No key saved yet.";
    status.classList.toggle("ok", s.set);
    el("songid-remove").classList.toggle("hidden", !s.set);
    opener.textContent = s.set ? "Change…" : "Set up…";
    return s.set;
  } catch (e) {
    status.textContent = "";
    return false;
  }
}

function openSongIdSetup(onSaved) {
  songId.onSaved = typeof onSaved === "function" ? onSaved : null;
  el("songid-input").value = "";
  songIdMessage("");
  el("songid-backdrop").classList.remove("hidden");
  if (typeof closeViewMenu === "function") closeViewMenu();
  refreshSongIdStatus();
  setTimeout(() => el("songid-input").focus(), 50);
}
function closeSongIdSetup() {
  el("songid-backdrop").classList.add("hidden");
  songId.onSaved = null;
}

async function saveSongIdKey() {
  const key = el("songid-input").value.trim();
  if (!key) { songIdMessage("Paste the key first.", "error"); el("songid-input").focus(); return; }
  const btn = el("songid-save");
  btn.disabled = true;
  btn.textContent = "Checking…";
  songIdMessage("Asking AcoustID about this key…");
  try {
    const r = await api("/radio/acoustid-key", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ acoustidApiKey: key }),
    });
    if (!r.ok) { songIdMessage(r.error || "That key wasn't accepted.", "error"); return; }
    const after = songId.onSaved;
    closeSongIdSetup();
    refreshSongIdStatus();
    showToast(r.state === "valid"
      ? "Song ID key checked and saved."
      : "Key saved. AcoustID couldn't be reached to check it — it will be tested the first time it's used.",
      { kind: r.state === "valid" ? "success" : "info", duration: 6000 });
    if (after) after();
  } catch (e) {
    songIdMessage(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Check and save";
  }
}

el("songid-open").addEventListener("click", () => openSongIdSetup());
el("songid-close").addEventListener("click", closeSongIdSetup);
el("songid-backdrop").addEventListener("click", (e) => { if (e.target.id === "songid-backdrop") closeSongIdSetup(); });
el("songid-save").addEventListener("click", saveSongIdKey);
el("songid-input").addEventListener("keydown", (e) => { if (e.key === "Enter") saveSongIdKey(); });
el("songid-open-site").addEventListener("click", () => {
  // The app's own window can't browse the web; hand the address to the real browser.
  if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external_url) window.pywebview.api.open_external_url(songId.signup);
  else window.open(songId.signup, "_blank", "noopener");
});
el("songid-paste").addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (text && text.trim()) { el("songid-input").value = text.trim(); songIdMessage(""); }
    else songIdMessage("The clipboard is empty — copy the key on acoustid.org first.", "error");
  } catch (e) {
    const paste = /Mac/i.test(navigator.platform || "") ? "⌘V" : "Ctrl+V";
    songIdMessage(`Couldn't read the clipboard here — click in the box and press ${paste}.`, "error");
    el("songid-input").focus();
  }
});
el("songid-remove").addEventListener("click", async () => {
  await api("/radio/acoustid-key", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ acoustidApiKey: "" }) });
  songIdMessage("The saved key was removed.");
  refreshSongIdStatus();
});

refreshSongIdStatus();
