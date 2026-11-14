// Tidy file names: give a kept copy its plain name back ("Song (2).mp3" ->
// "Song.mp3"). Loaded after app.js and duplicates.js (shares their globals:
// el, api, showToast, escapeHtml). All the "is that number really a copy
// number?" judgement is server-side (filename_cleanup.py); this is a
// look -> tick -> rename dialog. duplicates.js calls refreshTidyHint() after
// it deletes anything, guarded with typeof.

const tidyState = { items: [], blocked: [], busy: false };
const TIDY_MAX_ROWS = 400;  // a whole library renamed to a pattern can be thousands of rows

function tidyMode() { return el("tidy-mode").value; }

function tidyCount(n) { return `${n.toLocaleString()} file${n === 1 ? "" : "s"}`; }

// The line in the Remove duplicates panel: says whether there is anything to do.
async function refreshTidyHint() {
  const text = el("tidy-text");
  const btn = el("tidy-open");
  try {
    const data = await api("/tidy-names/preview");
    const n = data.items.length;
    const blocked = data.blocked.length;
    if (n) {
      text.innerHTML = `<b>${tidyCount(n)}</b> still ${n === 1 ? "has" : "have"} a copy number in ${n === 1 ? "its" : "their"} name, like “${escapeHtml(data.items[0].old_name)}”.`;
      btn.textContent = `Tidy ${tidyCount(n)}…`;
    } else {
      text.textContent = blocked
        ? `${tidyCount(blocked)} with a copy number can't be renamed yet — the plain-named copy is still there.`
        : "No leftover copy numbers — every file has its plain name.";
      btn.textContent = blocked ? "See which…" : "Tidy file names…";
    }
    btn.disabled = !n && !blocked;
  } catch (e) {
    text.textContent = "Couldn't check the file names just now.";
    btn.disabled = false;
  }
}

// Ticked rows -- plus, when everything shown is ticked, the rows past what's shown.
function tidySelectedIds() {
  const boxes = [...el("tidy-list").querySelectorAll("input[type=checkbox]:not(:disabled)")];
  const ids = boxes.filter((c) => c.checked).map((c) => Number(c.dataset.id));
  if (boxes.length && ids.length === boxes.length) tidyState.items.slice(TIDY_MAX_ROWS).forEach((it) => ids.push(it.id));
  return ids;
}

function updateTidyControls() {
  const boxes = [...el("tidy-list").querySelectorAll("input[type=checkbox]:not(:disabled)")];
  const shown = boxes.filter((c) => c.checked).length;
  const picked = tidySelectedIds().length;
  el("tidy-all").checked = boxes.length > 0 && shown === boxes.length;
  el("tidy-all").indeterminate = shown > 0 && shown < boxes.length;
  el("tidy-all-label").textContent = `Select all (${tidyState.items.length.toLocaleString()})`;
  const apply = el("tidy-apply");
  apply.disabled = !picked || tidyState.busy;
  apply.textContent = tidyState.busy ? "Renaming…" : (picked ? `Rename ${tidyCount(picked)}` : "Rename");
}

function renderTidy() {
  const { items, blocked } = tidyState;
  const list = el("tidy-list");
  list.innerHTML = "";
  items.slice(0, TIDY_MAX_ROWS).forEach((it) => {
    const row = document.createElement("label");
    row.className = "tidy-row";
    row.title = it.folder ? `In ${it.folder}` : "";
    row.innerHTML = `
      <input type="checkbox" data-id="${it.id}" checked>
      <div class="tidy-names">
        <div class="tidy-old">${escapeHtml(it.old_name)}</div>
        <div class="tidy-new">${escapeHtml(it.new_name)}</div>
      </div>`;
    list.appendChild(row);
  });
  if (items.length > TIDY_MAX_ROWS) {
    const more = document.createElement("div");
    more.className = "tidy-more";
    more.textContent = `…and ${(items.length - TIDY_MAX_ROWS).toLocaleString()} more, included when everything above is ticked.`;
    list.appendChild(more);
  }

  const any = items.length > 0;
  const pattern = tidyMode() !== "copy-numbers";
  ["tidy-list", "tidy-all-row", "tidy-apply", "tidy-note", "tidy-actions"].forEach((id) => el(id).classList.toggle("hidden", !any));
  if (pattern) {
    el("tidy-summary").textContent = any
      ? `${tidyCount(items.length)} would be renamed from ${items.length === 1 ? "its" : "their"} tags. Files without the tags the pattern needs are left alone. Untick any you'd rather leave.`
      : (blocked.length ? "Nothing can be renamed right now." : "Every file that has the tags for it is already named this way.");
  } else {
    el("tidy-summary").textContent = any
      ? `${tidyCount(items.length)} can go back to ${items.length === 1 ? "its" : "their"} plain name. Untick any you'd rather leave.`
      : (blocked.length ? "Nothing can be renamed right now." : "Every file already has its plain name — nothing to tidy.");
  }

  const blockedEl = el("tidy-blocked");
  blockedEl.classList.toggle("hidden", !blocked.length);
  if (blocked.length) {
    const shown = blocked.slice(0, 20);
    blockedEl.innerHTML =
      `<b>${tidyCount(blocked.length)} can't be renamed yet.</b> A file with that name is already in the same folder, ` +
      `so these are probably duplicates — remove the extra copy first, then come back.` +
      `<ul>${shown.map((b) => `<li>${escapeHtml(b.old_name)}</li>`).join("")}` +
      `${blocked.length > shown.length ? `<li>…and ${(blocked.length - shown.length).toLocaleString()} more</li>` : ""}</ul>`;
  }
  updateTidyControls();
}

async function loadTidy() {
  el("tidy-summary").textContent = tidyMode() === "copy-numbers" ? "Looking for leftover copy numbers…" : "Working out the new names…";
  ["tidy-list", "tidy-all-row", "tidy-apply", "tidy-note", "tidy-actions", "tidy-blocked"].forEach((id) => el(id).classList.add("hidden"));
  try {
    const data = await api(`/tidy-names/preview?mode=${encodeURIComponent(tidyMode())}`);
    tidyState.items = data.items;
    tidyState.blocked = data.blocked;
    renderTidy();
  } catch (e) {
    el("tidy-summary").textContent = `Couldn't check the file names: ${e.message}`;
  }
}

// Opened from the Remove duplicates panel, this takes its place rather than
// stacking a second dialog on top of it; closing brings that panel back.
function openTidy() {
  const dup = el("dup-backdrop");
  tidyState.returnToDup = !dup.classList.contains("hidden");
  dup.classList.add("hidden");
  el("tidy-backdrop").classList.remove("hidden");
  loadTidy();
}
function closeTidy() {
  el("tidy-backdrop").classList.add("hidden");
  if (tidyState.returnToDup) el("dup-backdrop").classList.remove("hidden");
  tidyState.returnToDup = false;
}

el("tidy-open").addEventListener("click", () => { el("tidy-mode").value = "copy-numbers"; openTidy(); });
el("tidy-mode").addEventListener("change", loadTidy);
el("tidy-close").addEventListener("click", closeTidy);
el("tidy-backdrop").addEventListener("click", (e) => { if (e.target.id === "tidy-backdrop") closeTidy(); });
el("tidy-list").addEventListener("change", updateTidyControls);
el("tidy-all").addEventListener("change", (e) => {
  el("tidy-list").querySelectorAll("input[type=checkbox]:not(:disabled)").forEach((c) => { c.checked = e.target.checked; });
  updateTidyControls();
});

el("tidy-apply").addEventListener("click", async () => {
  const ids = tidySelectedIds();
  if (!ids.length || tidyState.busy) return;
  tidyState.busy = true;
  updateTidyControls();
  try {
    const r = await api("/tidy-names", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, mode: tidyMode() }),
    });
    if (!r.ok) {
      showToast(r.error || "Couldn't rename the files.", { kind: "error" });
      return;
    }
    const failed = r.errors || [];
    if (r.renamed) {
      showToast(`Renamed ${tidyCount(r.renamed)}${failed.length ? `, ${failed.length} couldn't be` : ""}. Undo it from History if you change your mind.`,
        { kind: failed.length ? "info" : "success", duration: 6000 });
    } else if (failed.length) {
      showToast(`Couldn't rename: ${failed[0].error}`, { kind: "error" });
    }
    await loadTidy();
    refreshTidyHint();
  } catch (e) {
    showToast(e.message, { kind: "error" });
  } finally {
    tidyState.busy = false;
    updateTidyControls();
  }
});

// Opening the duplicates panel is when the hint is worth computing.
el("find-duplicates").addEventListener("click", refreshTidyHint);
