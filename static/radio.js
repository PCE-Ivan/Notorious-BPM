// Live internet radio: station browser, playback, song identification.
// Split out of app.js; loaded after it and sharing its globals (state, el, api, showToast, ...).

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
