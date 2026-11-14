// Repeat, sleep timer, picking up where you left off, and a safety net for
// script errors. Loaded after app.js (shares its globals: state, audio, el,
// api, playTrack, showToast, updateQueueBadge, setOutputVolume).
//
// app.js calls two things defined here, both guarded with typeof so a failure
// to load this file can't break playback:
//   saveSession()       after a track change or a queue change
//   sleepShouldStop()   from the "ended" handler, for "end of this track"

// ------------------------------------------------------------ page errors --
// A script error used to vanish into a web inspector nobody has open. Send it
// to the app's log (what "Copy diagnostics" points at) and, at most every few
// seconds, say something went wrong instead of leaving a dead button.
function reportPageError(message, where, stack) {
  try {
    fetch(`${API}/client-log`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: String(message || ""), where: where || "", stack: stack || "" }),
    }).catch(() => {});
  } catch (e) { /* nothing more to do */ }
}

let lastErrorToastAt = 0;
function toastPageError(message) {
  const now = Date.now();
  if (now - lastErrorToastAt < 8000) return;
  lastErrorToastAt = now;
  showToast(`Something went wrong: ${message}`, { kind: "error" });
}

window.addEventListener("error", (e) => {
  // "Script error." with no file is a cross-origin script we don't control; ResizeObserver
  // loop notices are browser chatter.
  if (!e.filename || /ResizeObserver/.test(e.message || "")) return;
  reportPageError(e.message, `${e.filename}:${e.lineno}`, e.error && e.error.stack);
  toastPageError(e.message);
});

window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  // A play() call interrupted by the next track, or refused before the first click, isn't a fault.
  if (r && (r.name === "AbortError" || r.name === "NotAllowedError")) return;
  const message = r && r.message ? r.message : String(r);
  reportPageError(message, "unhandled promise rejection", r && r.stack);
  toastPageError(message);
});

// ----------------------------------------------------------------- repeat --
const REPEAT_ORDER = ["off", "all", "one"];
const REPEAT_UI = {
  off: { icon: "🔁", label: "Repeat: off" },
  all: { icon: "🔁", label: "Repeat: the whole list" },
  one: { icon: "🔂", label: "Repeat: this track" },
};

function setRepeat(mode) {
  if (!REPEAT_UI[mode]) mode = "off";
  state.repeat = mode;
  const btn = el("repeat-toggle");
  btn.textContent = REPEAT_UI[mode].icon;
  btn.title = `${REPEAT_UI[mode].label} (R)`;
  btn.setAttribute("aria-label", REPEAT_UI[mode].label);
  btn.classList.toggle("active", mode !== "off");
  saveSession();
}

el("repeat-toggle").addEventListener("click", () => {
  const next = REPEAT_ORDER[(REPEAT_ORDER.indexOf(state.repeat) + 1) % REPEAT_ORDER.length];
  setRepeat(next);
  showToast(REPEAT_UI[next].label + ".", { kind: "info", duration: 1800 });
});
el("shuffle-toggle").addEventListener("click", () => saveSession());
el("autoplay-similar").addEventListener("change", () => saveSession());

// ------------------------------------------------------------ sleep timer --
const sleepTimer = { endsAt: 0, endOfTrack: false, timeout: null, label: null, fading: null };

function sleepActive() { return sleepTimer.endOfTrack || sleepTimer.endsAt > 0; }

function renderSleepButton() {
  const btn = el("sleep-btn");
  btn.classList.toggle("active", sleepActive());
  if (sleepTimer.endOfTrack) {
    btn.textContent = "🌙 end of track";
    btn.title = "Sleep timer: stops after this track";
  } else if (sleepTimer.endsAt) {
    const mins = Math.max(1, Math.ceil((sleepTimer.endsAt - Date.now()) / 60000));
    btn.textContent = `🌙 ${mins}m`;
    btn.title = `Sleep timer: playback fades out in about ${mins} minute${mins === 1 ? "" : "s"}`;
  } else {
    btn.textContent = "🌙";
    btn.title = "Sleep timer";
  }
}

function clearSleepTimer({ restoreVolume = true } = {}) {
  clearTimeout(sleepTimer.timeout);
  clearInterval(sleepTimer.label);
  clearInterval(sleepTimer.fading);
  sleepTimer.endsAt = 0;
  sleepTimer.endOfTrack = false;
  sleepTimer.timeout = sleepTimer.label = sleepTimer.fading = null;
  if (restoreVolume) { state.fade = 1; setOutputVolume(); }
  renderSleepButton();
}

function startSleepTimer(minutes) {
  clearSleepTimer();
  if (minutes === "end") {
    sleepTimer.endOfTrack = true;
    showToast("Sleep timer: playback will stop after this track.", { kind: "info" });
  } else {
    sleepTimer.endsAt = Date.now() + minutes * 60000;
    sleepTimer.timeout = setTimeout(fadeOutAndStop, minutes * 60000);
    sleepTimer.label = setInterval(renderSleepButton, 15000);
    showToast(`Sleep timer: playback will fade out in ${minutes} minutes.`, { kind: "info" });
  }
  renderSleepButton();
}

// Eight seconds down to silence, then pause and put the volume back, so the
// next time you press play it isn't mysteriously quiet.
function fadeOutAndStop() {
  clearInterval(sleepTimer.label);
  const steps = 40;
  let n = 0;
  sleepTimer.fading = setInterval(() => {
    n++;
    state.fade = Math.max(0, 1 - n / steps);
    setOutputVolume();
    if (n >= steps) {
      audio.pause();
      clearSleepTimer();
      showToast("Sleep timer: goodnight.", { kind: "info" });
    }
  }, 200);
}

// Called by app.js when a track finishes; true means "don't start the next one".
function sleepShouldStop() {
  if (!sleepTimer.endOfTrack) return false;
  clearSleepTimer();
  showToast("Sleep timer: stopped after this track.", { kind: "info" });
  return true;
}

function hideSleepMenu() { el("sleep-menu").classList.add("hidden"); }

el("sleep-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const menu = el("sleep-menu");
  if (!menu.classList.contains("hidden")) { hideSleepMenu(); return; }
  el("sleep-cancel").disabled = !sleepActive();
  menu.classList.remove("hidden");
  // Open upward from the button -- it lives in the bottom player bar.
  const r = el("sleep-btn").getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = "auto";
  menu.style.bottom = `${window.innerHeight - r.top + 8}px`;
});
el("sleep-menu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-minutes]");
  if (item) startSleepTimer(item.dataset.minutes === "end" ? "end" : Number(item.dataset.minutes));
  if (e.target.closest("#sleep-cancel")) {
    clearSleepTimer();
    showToast("Sleep timer off.", { kind: "info", duration: 1800 });
  }
  hideSleepMenu();
});
document.addEventListener("click", (e) => { if (!e.target.closest("#sleep-menu")) hideSleepMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideSleepMenu(); });

// ---------------------------------------------------------------- session --
// What was playing, the queue, shuffle/repeat: kept in the page's local storage
// and put back on the next launch -- cued and paused, never blaring on startup.
// Only ids are stored; the tracks come back from the server (/tracks/by-ids), so
// a song deleted since then is simply skipped, and a library from a different
// file is recognised and ignored.
const SESSION_KEY = "jukebox-session";
let sessionReady = false;     // saving before the restore has run would wipe what's about to be restored
let sessionSaveTimer = null;
let lastSavedPosition = 0;
let libraryIdPromise = null;

function libraryId() {
  if (!libraryIdPromise) libraryIdPromise = api("/instance").then((i) => i.library || "").catch(() => "");
  return libraryIdPromise;
}

function saveSession() {
  if (!sessionReady) return;
  clearTimeout(sessionSaveTimer);
  sessionSaveTimer = setTimeout(writeSession, 400);
}

async function writeSession() {
  if (!sessionReady) return;
  try {
    const t = state.currentTrack && !state.currentTrack.isRadio ? state.currentTrack : null;
    const idx = t ? state.currentList.findIndex((x) => x.id === t.id) : -1;
    const library = await libraryId();
    localStorage.setItem(SESSION_KEY, JSON.stringify({
      v: 1, library,
      track: t ? t.id : null,
      position: t ? Math.floor(audio.currentTime || 0) : 0,
      upcoming: idx >= 0 ? state.currentList.slice(idx + 1, idx + 101).map((x) => x.id) : [],
      queue: state.queue.filter((x) => !x.isRadio).map((x) => x.id),
      shuffle: state.shuffle, repeat: state.repeat, autoplay: el("autoplay-similar").checked,
    }));
  } catch (e) { /* storage full or unavailable -- the session just isn't remembered */ }
}

function writeSessionNow() {
  clearTimeout(sessionSaveTimer);
  writeSession();
}

audio.addEventListener("timeupdate", () => {
  if (Math.abs(audio.currentTime - lastSavedPosition) >= 5) {
    lastSavedPosition = audio.currentTime;
    saveSession();
  }
});
audio.addEventListener("pause", writeSessionNow);
window.addEventListener("pagehide", writeSessionNow);

async function restoreSession() {
  try {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(SESSION_KEY) || "null"); } catch (e) { /* unreadable: start fresh */ }
    if (!saved || saved.v !== 1) return;

    state.shuffle = !!saved.shuffle;
    el("shuffle-toggle").classList.toggle("active", state.shuffle);
    el("autoplay-similar").checked = !!saved.autoplay;
    state.autoplaySimilar = !!saved.autoplay;
    if (typeof syncAutoplayBtn === "function") syncAutoplayBtn();
    setRepeat(saved.repeat);

    const library = await libraryId();
    if (saved.library && library && saved.library !== library) return;  // someone else's track ids

    const wanted = [];
    for (const id of [saved.track, ...(saved.queue || []), ...(saved.upcoming || [])]) {
      if (Number.isInteger(id) && !wanted.includes(id)) wanted.push(id);
    }
    if (!wanted.length) return;
    const data = await api(`/tracks/by-ids?ids=${wanted.join(",")}`);
    const byId = new Map(data.tracks.map((t) => [t.id, t]));

    state.queue = (saved.queue || []).map((id) => byId.get(id)).filter(Boolean);
    updateQueueBadge();

    const current = byId.get(saved.track);
    if (current && !state.currentTrack) {
      const list = [current, ...(saved.upcoming || []).map((id) => byId.get(id)).filter(Boolean)];
      playTrack(current, list, { paused: true, startAt: saved.position || 0 });
    }
  } catch (e) {
    /* a failed restore must never get in the way of using the app */
  } finally {
    sessionReady = true;
  }
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", restoreSession);
else restoreSession();
