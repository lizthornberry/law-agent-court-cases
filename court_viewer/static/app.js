"use strict";

// ----------------------------------------------------------------------------
// State
// ----------------------------------------------------------------------------
const state = {
  meta: null,
  currentCase: null,   // the loaded (server) case object
  dirty: false,
  lastListMode: { type: "all", status: "" }, // remember how the list was built
};

// Short fields use <input>; long ones use <textarea>.
const LONG_FIELDS = new Set(["claim", "plea_verbatim", "full_transcript"]);

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, ...children) => {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, v);
  });
  children.flat().forEach((c) => node.append(c && c.nodeType ? c : document.createTextNode(c ?? "")));
  return node;
};

const effective = (tri) => (tri && tri.edited !== null && tri.edited !== undefined ? tri.edited : (tri ? tri.gemini : null));
const statusClass = (s) => "chip " + String(s || "").replace(/\s+/g, "");

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 2200);
}

// ----------------------------------------------------------------------------
// Dirty state + unsaved-changes guard
// ----------------------------------------------------------------------------
function setDirty(on) {
  state.dirty = on;
  $("#dirty-indicator").classList.toggle("hidden", !on);
  const sb = $("#save-btn");
  const db = $("#discard-btn");
  if (sb) sb.disabled = !on;
  if (db) db.disabled = !on;
}

// Show the Save/Discard/Cancel modal; resolves to "save" | "discard" | "cancel".
function askGuard() {
  return new Promise((resolve) => {
    const modal = $("#guard");
    modal.classList.remove("hidden");
    const done = (choice) => {
      modal.classList.add("hidden");
      $("#guard-save").onclick = $("#guard-discard").onclick = $("#guard-cancel").onclick = null;
      resolve(choice);
    };
    $("#guard-save").onclick = () => done("save");
    $("#guard-discard").onclick = () => done("discard");
    $("#guard-cancel").onclick = () => done("cancel");
  });
}

// Run `fn` only after resolving any unsaved changes. Returns true if proceeded.
async function guarded(fn) {
  if (!state.dirty) { await fn(); return true; }
  const choice = await askGuard();
  if (choice === "cancel") return false;
  if (choice === "save") {
    const ok = await saveCase();
    if (!ok) return false;
  } else {
    setDirty(false); // discard
  }
  await fn();
  return true;
}

window.addEventListener("beforeunload", (e) => {
  if (state.dirty) { e.preventDefault(); e.returnValue = ""; return ""; }
});

// ----------------------------------------------------------------------------
// API
// ----------------------------------------------------------------------------
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

// ----------------------------------------------------------------------------
// Init
// ----------------------------------------------------------------------------
async function init() {
  state.meta = await api("/api/meta");

  // search-scope field options
  const fg = $("#scope-fields");
  state.meta.fields.forEach((f) => fg.append(el("option", { value: f }, f)));

  // status filters (both the search filter and the list builder reuse this)
  const sf = $("#status-filter");
  state.meta.statuses.forEach((s) => {
    const c = state.meta.status_counts[s] || 0;
    sf.append(el("option", { value: s }, `${s} (${c})`));
  });

  $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });
  $("#clear-search").addEventListener("click", () => {
    $("#q").value = "";
    loadCaseList();
  });
  $("#status-filter").addEventListener("change", () => {
    if ($("#q").value.trim()) runSearch(); else loadCaseList();
  });
  $("#lightbox-close").addEventListener("click", () => $("#lightbox").classList.add("hidden"));
  $("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") $("#lightbox").classList.add("hidden"); });

  await loadCaseList();
}

// ----------------------------------------------------------------------------
// Case list / search
// ----------------------------------------------------------------------------
async function loadCaseList() {
  const status = $("#status-filter").value;
  state.lastListMode = { type: "all", status };
  const data = await api("/api/cases" + (status ? `?status=${encodeURIComponent(status)}` : ""));
  $("#list-title").textContent = "Cases";
  $("#list-count").textContent = `${data.cases.length}`;
  const ul = $("#case-list");
  ul.innerHTML = "";
  data.cases.forEach((c) => {
    const li = el("li", { "data-id": c.case_id, onclick: () => guarded(() => openCase(c.case_id)) },
      el("div", { class: "ci-title" }, c.case_number || c.case_id),
      el("div", { class: "ci-sub" },
        `${c.box} · ${c.page_count} pp`,
        el("span", { class: statusClass(c.review_status), style: "margin-left:8px" }, c.review_status)),
      el("div", { class: "ci-sub" }, [c.plaintiff, c.defendant].filter(Boolean).join(" v. ")),
    );
    ul.append(li);
  });
  highlightActive();
}

async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return loadCaseList();
  const scope = $("#scope").value;
  const status = $("#status-filter").value;
  const params = new URLSearchParams({ q });
  if (scope) params.set("scope", scope);
  if (status) params.set("status", status);
  const data = await api("/api/search?" + params.toString());
  state.lastListMode = { type: "search", q, scope, status };
  $("#list-title").textContent = "Search results";
  $("#list-count").textContent = `${data.count}`;
  const ul = $("#case-list");
  ul.innerHTML = "";
  if (!data.hits.length) {
    ul.append(el("li", { class: "muted" }, "No matches."));
    return;
  }
  data.hits.forEach((h) => {
    const li = el("li", { "data-id": h.case_id, onclick: () => guarded(() => openCase(h.case_id)) },
      el("div", { class: "ci-title" }, h.case_number || h.case_id),
      el("div", { class: "ci-sub" },
        `${h.box} · field: ${h.field}`,
        el("span", { class: statusClass(h.review_status), style: "margin-left:8px" }, h.review_status)),
      el("div", { class: "snippet", html: h.snippet || "" }),
    );
    ul.append(li);
  });
  highlightActive();
}

function highlightActive() {
  const id = state.currentCase ? state.currentCase.case_id : null;
  document.querySelectorAll("#case-list li").forEach((li) => {
    li.classList.toggle("active", li.getAttribute("data-id") === id);
  });
}

// ----------------------------------------------------------------------------
// Case detail + editor
// ----------------------------------------------------------------------------
async function openCase(caseId) {
  const c = await api(`/api/cases/${encodeURIComponent(caseId)}`);
  state.currentCase = c;
  renderDetail(c);
  setDirty(false);
  highlightActive();
}

function fieldEditor(name, tri, confidence) {
  const eff = effective(tri) ?? "";
  const gem = tri.gemini;
  const isEdited = tri.edited !== null && tri.edited !== undefined;
  const input = LONG_FIELDS.has(name)
    ? el("textarea", { rows: name === "full_transcript" ? 14 : 4, "data-field": name }, eff)
    : el("input", { type: "text", "data-field": name, value: eff });
  input.addEventListener("input", () => setDirty(true));

  const labelKids = [name];
  if (confidence) labelKids.push(el("span", { class: "conf " + confidence }, `(${confidence})`));
  const editedFlag = el("span", { class: "edited-flag" + (isEdited ? "" : " hidden") }, "edited");

  const row = el("div", { class: "field-row" },
    el("label", {}, ...labelKids, editedFlag),
    input,
  );
  if (isEdited && gem !== null && gem !== undefined && gem !== "") {
    row.append(el("div", { class: "gemini-hint" }, `gemini: ${gem}`));
  }
  return row;
}

function renderDetail(c) {
  const root = $("#detail");
  root.innerHTML = "";

  root.append(el("h2", {}, (effective(c.fields.case_number) || c.case_id)));
  root.append(el("div", { class: "case-meta" },
    `${c.box} · pages ${c.page_range.join("–")} · ${c.is_appeal ? "APPEAL" : "civil"} · `,
    `${c.source_images.length} images · provider: ${c.provenance.provider || "?"} / ${c.provenance.model || "?"}`));

  // controls: review status + (top) save bar
  const statusSel = el("select", { id: "review-status" });
  state.meta.statuses.forEach((s) =>
    statusSel.append(el("option", { value: s, ...(s === c.review_status ? { selected: "" } : {}) }, s)));
  statusSel.addEventListener("change", () => setDirty(true));

  root.append(el("div", { class: "controls-row" },
    el("strong", {}, "Review status:"), statusSel,
    el("span", { class: statusClass(c.review_status), id: "status-chip" }, c.review_status)));

  // two-column layout: left = fields + notes, right = pages + images
  const left = el("div", {});
  const right = el("div", {});

  // ---- fields card ----
  const fieldsCard = el("div", { class: "card" }, el("h3", {}, "Extracted fields"));
  state.meta.fields.forEach((name) => {
    fieldsCard.append(fieldEditor(name, c.fields[name] || { gemini: null, edited: null }, c.field_confidence[name]));
  });
  left.append(fieldsCard);

  // ---- notes card ----
  const notes = el("textarea", { id: "notes", rows: 5, placeholder: "Reviewer notes…" }, c.notes || "");
  notes.addEventListener("input", () => setDirty(true));
  left.append(el("div", { class: "card" }, el("h3", {}, "Notes"), notes));

  // ---- images card ----
  const thumbs = el("div", { class: "thumbs" });
  c.source_images.forEach((fn) => {
    const params = new URLSearchParams({ box: c.box, filename: fn });
    const img = el("img", { class: "thumb", loading: "lazy", src: "/api/thumb?" + params.toString(), title: fn });
    img.addEventListener("click", () => openLightbox(c.box, fn));
    thumbs.append(img);
  });
  right.append(el("div", { class: "card" }, el("h3", {}, `Images (${c.source_images.length})`), thumbs));

  // ---- pages card ----
  const pagesCard = el("div", { class: "card" }, el("h3", {}, `Page transcripts (${c.pages.length})`));
  c.pages.forEach((p) => {
    const tri = p.transcript || { gemini: null, edited: null };
    const eff = effective(tri) ?? "";
    const isEdited = tri.edited !== null && tri.edited !== undefined;
    const ta = el("textarea", { rows: 8, "data-page": p.filename }, eff);
    ta.addEventListener("input", () => setDirty(true));
    const head = el("div", { class: "page-head" },
      el("strong", {}, `#${p.order} ${p.filename}`),
      el("span", { class: "pt" }, p.page_type),
      el("span", { class: "edited-flag" + (isEdited ? "" : " hidden") }, "edited"),
      el("button", { type: "button", class: "ghost", onclick: () => openLightbox(c.box, p.filename) }, "view image"));
    pagesCard.append(el("div", { class: "page-block" }, head, ta));
  });
  right.append(pagesCard);

  root.append(el("div", { class: "detail-grid" }, left, right));

  // ---- save bar ----
  const saveBtn = el("button", { id: "save-btn", class: "primary", disabled: "" , onclick: () => saveCase() }, "Save");
  const discardBtn = el("button", { id: "discard-btn", class: "ghost", disabled: "", onclick: () => discardChanges() }, "Discard");
  root.append(el("div", { class: "save-bar" }, saveBtn, discardBtn,
    el("span", { class: "muted" }, "Edits set the “edited” slot; the original gemini value is preserved.")));
}

function collectPayload() {
  const c = state.currentCase;
  const fields = {};
  document.querySelectorAll("[data-field]").forEach((inp) => {
    const name = inp.getAttribute("data-field");
    const gem = c.fields[name] ? (c.fields[name].gemini ?? "") : "";
    const val = inp.value;
    // If the value equals the gemini baseline, clear the edit (null); else store it.
    fields[name] = (val === gem) ? null : val;
  });
  const pages = {};
  document.querySelectorAll("[data-page]").forEach((ta) => {
    const fn = ta.getAttribute("data-page");
    const page = c.pages.find((p) => p.filename === fn);
    const gem = page && page.transcript ? (page.transcript.gemini ?? "") : "";
    const val = ta.value;
    pages[fn] = (val === gem) ? null : val;
  });
  return {
    fields,
    pages,
    review_status: $("#review-status").value,
    notes: $("#notes").value,
  };
}

async function saveCase() {
  if (!state.currentCase) return false;
  try {
    const payload = collectPayload();
    const res = await api(`/api/cases/${encodeURIComponent(state.currentCase.case_id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.currentCase = res.case;
    renderDetail(res.case);
    setDirty(false);
    // refresh status counts + list snippets
    state.meta = await api("/api/meta");
    refreshStatusCounts();
    toast("Saved and exported to results.json");
    return true;
  } catch (err) {
    toast("Save failed: " + err.message);
    return false;
  }
}

function refreshStatusCounts() {
  const sf = $("#status-filter");
  const cur = sf.value;
  // rebuild options 1..n (keep the first "Any status")
  while (sf.options.length > 1) sf.remove(1);
  state.meta.statuses.forEach((s) => {
    const c = state.meta.status_counts[s] || 0;
    sf.append(el("option", { value: s, ...(s === cur ? { selected: "" } : {}) }, `${s} (${c})`));
  });
}

function discardChanges() {
  if (state.currentCase) {
    renderDetail(state.currentCase);
    setDirty(false);
  }
}

// ----------------------------------------------------------------------------
// Lightbox
// ----------------------------------------------------------------------------
function openLightbox(box, filename) {
  const params = new URLSearchParams({ box, filename });
  $("#lightbox-img").src = "/api/image?" + params.toString();
  $("#lightbox-caption").textContent = `${box} / ${filename}`;
  $("#lightbox").classList.remove("hidden");
}

init().catch((e) => {
  document.body.innerHTML = `<pre style="padding:20px;color:#dc2626">Failed to start: ${e.message}</pre>`;
});
