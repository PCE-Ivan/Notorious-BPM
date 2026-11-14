// Tidy file names: give a kept copy its plain name back ("Song (2).mp3" ->
// "Song.mp3"). Loaded after app.js and duplicates.js (shares their globals:
// el, api, showToast, escapeHtml). All the "is that number really a copy
// number?" judgement is server-side (filename_cleanup.py); this is a
// look -> tick -> rename dialog. duplicates.js calls refreshTidyHint() after
// it deletes anything, guarded with typeof.

const tidyState = { items: [], blocked: [], busy: false };

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

function tidySelectedIds() {
  return [...el("tidy-list").querySelectorAll("input[type=checkbox]:checked:not(:disabled)")].map((c) => Number(c.dataset.id));
}

function updateTidyControls() {
  const boxes = [...el("tidy-list").querySelectorAll("input[type=checkbox]:not(:disabled)")];
  const picked = boxes.filter((c) => c.checked).length;
  el("tidy-all").checked = boxes.length > 0 && picked === boxes.length;
  el("tidy-all").indeterminate = picked > 0 && picked < boxes.length;
  el("tidy-all-label").textContent = `Select all (${boxes.length.toLocaleString()})`;
  const apply = el("tidy-apply");
  apply.disabled = !picked || tidyState.busy;
  apply.textContent = tidyState.busy ? "Renaming…" : (picked ? `Rename ${tidyCount(picked)}` : "Rename");
}

function renderTidy() {
  const { items, blocked } = tidyState;
  const list = el("tidy-list");
  list.innerHTML = "";
  items.forEach((it) => {
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

  const any = items.length > 0;
  ["tidy-list", "tidy-all-row", "tidy-apply", "tidy-note", "tidy-actions"].forEach((id) => el(id).classList.toggle("hidden", !any));
  el("tidy-summary").textContent = any
    ? `${tidyCount(items.length)} can go back to ${items.length === 1 ? "its" : "their"} plain name. Untick any you'd rather leave.`
    : (blocked.length ? "Nothing can be renamed right now." : "Every file already has its plain name — nothing to tidy.");

  const blockedEl = el("tidy-blocked");
  blockedEl.classList.toggle("hidden", !blocked.length);
  if (blocked.length) {
    const shown = blocked.slice(0, 20);
    blockedEl.innerHTML =
      `<b>${tidyCount(blocked.length)} can't be renamed yet.</b> A file with the plain name is still in the same folder, ` +
      `so these are probably duplicates — remove the extra copy first, then come back.` +
      `<ul>${shown.map((b) => `<li>${escapeHtml(b.old_name)}</li>`).join("")}` +
      `${blocked.length > shown.length ? `<li>…and ${(blocked.length - shown.length).toLocaleString()} more</li>` : ""}</ul>`;
  }
  updateTidyControls();
}

async function loadTidy() {
  el("tidy-summary").textContent = "Looking for leftover copy numbers…";
  ["tidy-list", "tidy-all-row", "tidy-apply", "tidy-note", "tidy-actions", "tidy-blocked"].forEach((id) => el(id).classList.add("hidden"));
  try {
    const data = await api("/tidy-names/preview");
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

el("tidy-open").addEventListener("click", openTidy);
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
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
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
