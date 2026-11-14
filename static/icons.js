// One drawn icon set in place of emoji. Loaded first.
//
// Emoji render in full colour, differently on every macOS version, and sit
// oddly on the wood, brushed-metal and cassette decks. These are plain line
// icons that take the colour of the text around them.
//
// Nothing else in the app had to change: the markup and the scripts still say
// "▶" or "🗑", and this swaps the glyph for its drawing wherever it appears in
// a control (buttons, tiles, headings, menu items). The original character is
// kept inside the icon, invisibly, so code that reads a button's text and puts
// it back later ("Scanning…" -> the old label) still round-trips.

const ICON_PATHS = {
  play: '<path d="M7 4.5v15l12.5-7.5z" fill="currentColor" stroke-linejoin="round"/>',
  pause: '<rect x="6" y="4.5" width="4.2" height="15" rx="1" fill="currentColor"/><rect x="13.8" y="4.5" width="4.2" height="15" rx="1" fill="currentColor"/>',
  prev: '<path d="M19 5v14l-10-7z" fill="currentColor" stroke-linejoin="round"/><path d="M5.5 5v14"/>',
  next: '<path d="M5 5v14l10-7z" fill="currentColor" stroke-linejoin="round"/><path d="M18.5 5v14"/>',
  shuffle: '<path d="M3 7h3.5c2.2 0 3.6 1 4.8 2.9l1.4 2.2c1.2 1.9 2.6 2.9 4.8 2.9H21"/><path d="M3 17h3.5c1.3 0 2.3-.4 3.2-1.1"/><path d="M14.3 8.1c.9-.7 1.9-1.1 3.2-1.1H21"/><path d="M18.5 4.5 21 7l-2.5 2.5"/><path d="M18.5 12.5 21 15l-2.5 2.5"/>',
  repeat: '<path d="M17 3.5 20 6.5l-3 3"/><path d="M4 11.5v-1a4 4 0 0 1 4-4h12"/><path d="M7 20.5l-3-3 3-3"/><path d="M20 12.5v1a4 4 0 0 1-4 4H4"/>',
  repeat1: '<path d="M17 3.5 20 6.5l-3 3"/><path d="M4 11.5v-1a4 4 0 0 1 4-4h12"/><path d="M7 20.5l-3-3 3-3"/><path d="M20 12.5v1a4 4 0 0 1-4 4H4"/><path d="M11.2 10.8 12.5 10v4.5"/>',
  moon: '<path d="M20 14.2A8 8 0 0 1 9.8 4a8 8 0 1 0 10.2 10.2z"/>',
  infinity: '<path d="M12 12c-1.7-2.3-3.3-3.5-5-3.5a3.5 3.5 0 0 0 0 7c1.7 0 3.3-1.2 5-3.5zm0 0c1.7 2.3 3.3 3.5 5 3.5a3.5 3.5 0 0 0 0-7c-1.7 0-3.3 1.2-5 3.5z"/>',
  volume: '<path d="M4 9.5v5h3.5l4.5 4v-13l-4.5 4z" fill="currentColor" stroke-linejoin="round"/><path d="M15.5 9a4 4 0 0 1 0 6"/><path d="M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  volumeLow: '<path d="M4 9.5v5h3.5l4.5 4v-13l-4.5 4z" fill="currentColor" stroke-linejoin="round"/><path d="M15.5 9a4 4 0 0 1 0 6"/>',
  mute: '<path d="M4 9.5v5h3.5l4.5 4v-13l-4.5 4z" fill="currentColor" stroke-linejoin="round"/><path d="m16 9.5 5 5m0-5-5 5"/>',
  level: '<path d="M5 20V10m7 10V4m7 16v-7"/><path d="M3 10h4m3-6h4m3 9h4"/>',
  lyrics: '<path d="M5 6h14M5 10.5h14M5 15h9M5 19.5h6"/>',
  image: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="m4.5 18 5-5 3.5 3.5 2.5-2.5 4.5 4.5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5.5 5.5"/>',
  tag: '<path d="M3.5 12.2V5a1.5 1.5 0 0 1 1.5-1.5h7.2a1.5 1.5 0 0 1 1.1.4l7.3 7.3a1.5 1.5 0 0 1 0 2.1l-7.3 7.3a1.5 1.5 0 0 1-2.1 0L3.9 13.3a1.5 1.5 0 0 1-.4-1.1z"/><circle cx="8" cy="8" r="1.3" fill="currentColor"/>',
  trash: '<path d="M4 6.5h16M9.5 6.5v-2h5v2M6.5 6.5l.8 13h9.4l.8-13M10 10.5v5.5m4-5.5v5.5"/>',
  music: '<path d="M9 17.5V5.5l10-2v12"/><circle cx="6.5" cy="17.5" r="2.5"/><circle cx="16.5" cy="15.5" r="2.5"/>',
  folder: '<path d="M3.5 7a1.5 1.5 0 0 1 1.5-1.5h4l2 2.5h8a1.5 1.5 0 0 1 1.5 1.5v8.5a1.5 1.5 0 0 1-1.5 1.5H5a1.5 1.5 0 0 1-1.5-1.5z"/>',
  expand: '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
  popout: '<rect x="3.5" y="8.5" width="12" height="12" rx="1.5"/><path d="M8.5 8.5v-3a1.5 1.5 0 0 1 1.5-1.5h9a1.5 1.5 0 0 1 1.5 1.5v9a1.5 1.5 0 0 1-1.5 1.5h-3.5"/>',
  copies: '<rect x="8.5" y="8.5" width="12" height="12" rx="1.5"/><path d="M15.5 8.5V5a1.5 1.5 0 0 0-1.5-1.5H5A1.5 1.5 0 0 0 3.5 5v9A1.5 1.5 0 0 0 5 15.5h3.5"/>',
  organize: '<path d="M3.5 6.5A1.5 1.5 0 0 1 5 5h3.5l1.5 2h4.5A1.5 1.5 0 0 1 16 8.5V9"/><path d="M7 10.5A1.5 1.5 0 0 1 8.5 9h3.5l1.5 2h5.5a1.5 1.5 0 0 1 1.5 1.5v5A1.5 1.5 0 0 1 19 19H8.5A1.5 1.5 0 0 1 7 17.5z"/><path d="M3.5 6.5V15A1.5 1.5 0 0 0 5 16.5h2"/>',
  broadcast: '<circle cx="12" cy="12" r="1.8" fill="currentColor"/><path d="M8.3 15.7a5.2 5.2 0 0 1 0-7.4m7.4 0a5.2 5.2 0 0 1 0 7.4"/><path d="M5.5 18.5a9.2 9.2 0 0 1 0-13m13 0a9.2 9.2 0 0 1 0 13"/>',
  heart: '<path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7.3a4.3 4.3 0 0 1 7.5 2.5C19.5 15.4 12 20 12 20z"/>',
  target: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/><circle cx="12" cy="12" r="1" fill="currentColor"/>',
  library: '<path d="M5 4.5v15M9.5 4.5v15"/><path d="m13.5 5.5 5 13.5"/><path d="M3.5 19.5h17"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4v4h-4"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.5m0 13V21m9-9h-2.5m-13 0H3m15.4-6.4-1.8 1.8m-9.2 9.2-1.8 1.8m0-12.8 1.8 1.8m9.2 9.2 1.8 1.8"/>',
  checkSquare: '<rect x="4" y="4" width="16" height="16" rx="2.5"/><path d="m8 12.3 2.8 2.8L16.2 9"/>',
  download: '<path d="M12 4v11m0 0-4-4m4 4 4-4"/><path d="M4.5 16v2.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V16"/>',
  radio: '<rect x="3.5" y="9" width="17" height="11" rx="2"/><circle cx="8.5" cy="14.5" r="2.5"/><path d="M14 13h3.5M14 16h3.5M7 9l9-5"/>',
  chart: '<path d="M4 20V4m0 16h16"/><path d="M8 20v-6m4 6V8m4 12v-9"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5l3.5 2"/>',
  pulse: '<path d="M3 12.5h4l2.5-6 4 12 2.5-6h5"/>',
  help: '<circle cx="12" cy="12" r="8.5"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.7 2.2c-.8.5-1.2 1-1.2 1.8"/><circle cx="12" cy="16.7" r=".7" fill="currentColor"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r=".7" fill="currentColor"/>',
  swap: '<path d="M4 8.5h15m0 0-3.5-3.5M19 8.5 15.5 12"/><path d="M20 15.5H5m0 0 3.5-3.5M5 15.5 8.5 19"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01M7.5 14h9"/>',
  wand: '<path d="m5 19 9.5-9.5"/><path d="M16.5 3.5v3m-1.5-1.5h3M19.5 10v2m-1-1h2M10 3.5v2m-1-1h2"/>',
  headphones: '<path d="M4.5 15v-3a7.5 7.5 0 0 1 15 0v3"/><rect x="3.5" y="14" width="4" height="6" rx="1.5"/><rect x="16.5" y="14" width="4" height="6" rx="1.5"/>',
  calendar: '<rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4m7-4v4"/>',
  pencil: '<path d="M4 20l1-4.5L15.5 5a2 2 0 0 1 2.8 0l.7.7a2 2 0 0 1 0 2.8L8.5 19z"/>',
  sparkle: '<path d="M12 4l1.8 5.2L19 11l-5.2 1.8L12 18l-1.8-5.2L5 11l5.2-1.8z"/>',
  palette: '<path d="M12 3.5a8.5 8.5 0 1 0 0 17c1.2 0 1.8-.8 1.8-1.7 0-1.3-1-1.6-1-2.8 0-1 .8-1.7 1.9-1.7H17a3.5 3.5 0 0 0 3.5-3.5c0-4-3.8-7.3-8.5-7.3z"/><circle cx="8" cy="11" r="1" fill="currentColor"/><circle cx="11" cy="7.5" r="1" fill="currentColor"/><circle cx="15.5" cy="8.5" r="1" fill="currentColor"/>',
  person: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
  disc: '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.5"/>',
  circle: '<circle cx="12" cy="12" r="7.5"/>',
};

// glyph -> icon name
const ICON_FOR = {
  "▶": "play", "⏸": "pause", "⏮": "prev", "⏭": "next", "🔀": "shuffle", "🔁": "repeat", "🔂": "repeat1",
  "🌙": "moon", "∞": "infinity", "🔊": "volume", "🔉": "volumeLow", "🔇": "mute", "⚖": "level",
  "📜": "lyrics", "🖼": "image", "🔍": "search", "🏷": "tag", "🗑": "trash", "🎵": "music", "♪": "music",
  "📁": "folder", "⤢": "expand", "⧉": "popout", "📑": "copies", "🗂": "organize", "📡": "broadcast",
  "💛": "heart", "🎯": "target", "📚": "library", "⟳": "refresh", "⚙": "gear", "☑": "checkSquare",
  "📥": "download", "📻": "radio", "📊": "chart", "🕘": "clock", "🩺": "pulse", "❓": "help", "ℹ": "info",
  "⇄": "swap", "⌨": "keyboard", "🧠": "wand", "🎧": "headphones", "🎼": "music", "📅": "calendar",
  "🎚": "level", "✏": "pencil", "🧹": "sparkle", "🎨": "palette", "👤": "person", "💿": "disc", "○": "circle",
};

function iconSvg(name) {
  return `<svg class="ic-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name]}</svg>`;
}
function iconHtml(glyph) {
  const name = ICON_FOR[glyph];
  return name ? `<span class="ic" data-glyph="${glyph}">${iconSvg(name)}<span class="ic-ch">${glyph}</span></span>` : glyph;
}
// For rows built thousands of times: the magnifier, ready-made.
const ICON_SEARCH_HTML = iconHtml("🔍");

const ICON_GLYPH_RE = new RegExp("(" + Object.keys(ICON_FOR).map((g) => g.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")\\uFE0F?", "u");
// Where a glyph is decoration on a control. Song titles, lyrics, file names
// and anything typed are left exactly as they are.
const ICON_HOSTS = "button, a.tile, .tile, h3, h4, summary, #volume-icon, .ipod-step-icon, .health-issue-head, .tag-issue-header, .library-menu-item, .command-palette-item, .brand";
const ICON_SKIP = ".ic, input, textarea, select, option, script, style, [contenteditable], .np-text, .col-title, .col-artist, .col-album, .grid-title, .grid-sub, #lyrics-body";

function iconizeTextNode(node) {
  const parent = node.parentElement;
  if (!parent || !ICON_GLYPH_RE.test(node.nodeValue) || parent.closest(ICON_SKIP) || !parent.closest(ICON_HOSTS)) return;
  const frag = document.createDocumentFragment();
  let rest = node.nodeValue;
  let m;
  while ((m = ICON_GLYPH_RE.exec(rest))) {
    if (m.index) frag.appendChild(document.createTextNode(rest.slice(0, m.index)));
    const holder = document.createElement("span");
    holder.innerHTML = iconHtml(m[1]);
    frag.appendChild(holder.firstChild);
    rest = rest.slice(m.index + m[0].length);
  }
  if (rest) frag.appendChild(document.createTextNode(rest));
  parent.replaceChild(frag, node);
}

function iconize(root) {
  if (root.nodeType === Node.TEXT_NODE) { iconizeTextNode(root); return; }
  if (root.nodeType !== Node.ELEMENT_NODE || root.matches(ICON_SKIP)) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const found = [];
  while (walker.nextNode()) if (ICON_GLYPH_RE.test(walker.currentNode.nodeValue)) found.push(walker.currentNode);
  found.forEach(iconizeTextNode);
}

function watchForIcons(doc) {
  iconize(doc.body);
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === "characterData") iconize(r.target);
      else r.addedNodes.forEach(iconize);
    }
  }).observe(doc.body, { childList: true, subtree: true, characterData: true });
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => watchForIcons(document));
else watchForIcons(document);
