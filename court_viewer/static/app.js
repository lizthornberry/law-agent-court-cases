"use strict";

// ----------------------------------------------------------------------------
// State
// ----------------------------------------------------------------------------
const state = {
  meta: null,
  currentCase: null,
  dirty: false,
  lastListMode: { type: "all", status: "" },
  lightbox: { open: false, pageIndex: -1, filename: null },
  transcriptPanelOpen: false,
  imageZoom: { fitScale: 1, userScale: 1 },
};

const LONG_FIELDS = new Set(["claim", "plea_verbatim"]);
const PANEL_FIELDS = new Set(["full_transcript"]);
const YES_NO_FIELDS = new Set(["lawyer_or_agent_for_plaintiff", "lawyer_or_agent_for_defendant"]);

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

function isFormFocus() {
  const ae = document.activeElement;
  if (!ae) return false;
  const tag = ae.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (ae.isContentEditable) return true;
  return false;
}

function isPageReviewTranscriptFocus() {
  return document.activeElement === $("#lightbox-transcript");
}

function orderedCaseIds() {
  return [...document.querySelectorAll("#case-list li[data-id]")].map((li) => li.getAttribute("data-id"));
}

function sortedPages(c) {
  return [...(c.pages || [])].sort((a, b) => (a.order || 0) - (b.order || 0));
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

async function guarded(fn) {
  if (!state.dirty) { await fn(); return true; }
  const choice = await askGuard();
  if (choice === "cancel") return false;
  if (choice === "save") {
    const ok = await saveCase();
    if (!ok) return false;
  } else {
    setDirty(false);
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
// Search scope UI
// ----------------------------------------------------------------------------
function buildScopeChecks() {
  const wrap = $("#scope-fields-checks");
  wrap.innerHTML = "";
  const addScope = (value, label) => {
    const cb = el("input", { type: "checkbox", "data-scope": value });
    cb.addEventListener("change", onScopeCheckChange);
    wrap.append(el("label", {}, cb, label));
  };
  state.meta.fields.forEach((f) => addScope(f, f));
  addScope("transcript", "Page transcripts");
  addScope("notes", "Notes");
}

function onScopeAllChange() {
  const all = $("#scope-all");
  if (all.checked) {
    document.querySelectorAll("#scope-fields-checks input[data-scope]").forEach((cb) => { cb.checked = false; });
  }
}

function onScopeCheckChange() {
  const any = [...document.querySelectorAll("#scope-fields-checks input[data-scope]")].some((cb) => cb.checked);
  $("#scope-all").checked = !any;
}

function selectedScopes() {
  if ($("#scope-all").checked) return [];
  return [...document.querySelectorAll("#scope-fields-checks input[data-scope]:checked")]
    .map((cb) => cb.getAttribute("data-scope"));
}

// ----------------------------------------------------------------------------
// Init
// ----------------------------------------------------------------------------
async function init() {
  state.meta = await api("/api/meta");
  buildScopeChecks();
  $("#scope-all").addEventListener("change", onScopeAllChange);

  const sf = $("#status-filter");
  state.meta.statuses.forEach((s) => {
    const c = state.meta.status_counts[s] || 0;
    sf.append(el("option", { value: s }, `${s} (${c})`));
  });

  $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); runSearch(); });
  $("#clear-search").addEventListener("click", () => {
    $("#q").value = "";
    $("#scope-all").checked = true;
    document.querySelectorAll("#scope-fields-checks input[data-scope]").forEach((cb) => { cb.checked = false; });
    loadCaseList();
  });
  $("#status-filter").addEventListener("change", () => {
    if ($("#q").value.trim()) runSearch(); else loadCaseList();
  });

  ensureLightboxDom();
  $("#lightbox-close").addEventListener("click", closeLightbox);
  $("#lightbox-transcript").addEventListener("input", onLightboxTranscriptInput);
  initLightboxZoom();

  $("#detail").addEventListener("click", onDetailImageClick);

  $("#transcript-panel-close").addEventListener("click", closeTranscriptPanel);
  $("#transcript-panel-body").addEventListener("input", onTranscriptPanelInput);

  document.addEventListener("keydown", onGlobalKeydown);

  await loadCaseList();
}

function onGlobalKeydown(e) {
  if (e.key === "Escape") {
    if (state.lightbox.open) { e.preventDefault(); closeLightbox(); return; }
    if (state.transcriptPanelOpen) { e.preventDefault(); closeTranscriptPanel(); return; }
  }

  if (state.lightbox.open && (e.key === "ArrowLeft" || e.key === "ArrowRight")) {
    if (!isPageReviewTranscriptFocus() && state.lightbox.pageIndex >= 0) {
      e.preventDefault();
      navigateLightboxPage(e.key === "ArrowLeft" ? -1 : 1);
    }
    return;
  }

  if (state.lightbox.open && !isFormFocus() && !e.ctrlKey && !e.metaKey && !e.altKey) {
    if (e.key === "+" || e.key === "=") {
      e.preventDefault();
      lightboxZoomIn();
      return;
    }
    if (e.key === "-") {
      e.preventDefault();
      lightboxZoomOut();
      return;
    }
    if (e.key === "0") {
      e.preventDefault();
      lightboxZoomFit();
      return;
    }
  }

  if ((e.key === "ArrowUp" || e.key === "ArrowDown") && state.currentCase && !isFormFocus()) {
    const ids = orderedCaseIds();
    if (!ids.length) return;
    const idx = ids.indexOf(state.currentCase.case_id);
    if (idx < 0) return;
    const next = e.key === "ArrowUp" ? idx - 1 : idx + 1;
    if (next < 0 || next >= ids.length) return;
    e.preventDefault();
    guarded(() => openCase(ids[next]));
  }
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
  renderCaseList(data.cases.map((c) => ({
    case_id: c.case_id,
    case_number: c.case_number,
    box: c.box,
    page_count: c.page_count,
    review_status: c.review_status,
    plaintiff: c.plaintiff,
    defendant: c.defendant,
  })));
}

async function runSearch() {
  const q = $("#q").value.trim();
  if (!q) return loadCaseList();
  const scopes = selectedScopes();
  const status = $("#status-filter").value;
  const params = new URLSearchParams({ q });
  scopes.forEach((s) => params.append("scopes", s));
  if (status) params.set("status", status);
  const data = await api("/api/search?" + params.toString());
  state.lastListMode = { type: "search", q, scopes, status };
  $("#list-title").textContent = scopes.length ? `Search (${scopes.join(", ")})` : "Search results";
  $("#list-count").textContent = `${data.count}`;
  const ul = $("#case-list");
  ul.innerHTML = "";
  if (!data.hits.length) {
    ul.append(el("li", { class: "muted" }, "No matches."));
    return;
  }
  data.hits.forEach((h) => {
    ul.append(el("li", { "data-id": h.case_id, onclick: () => guarded(() => openCase(h.case_id)) },
      el("div", { class: "ci-title" }, h.case_number || h.case_id),
      el("div", { class: "ci-sub" },
        `${h.box} · ${h.field}`,
        el("span", { class: statusClass(h.review_status), style: "margin-left:8px" }, h.review_status)),
      el("div", { class: "ci-sub" }, [h.plaintiff, h.defendant].filter(Boolean).join(" v. ")),
      el("div", { class: "snippet", html: h.snippet || "" }),
    ));
  });
  highlightActive();
}

function renderCaseList(items) {
  const ul = $("#case-list");
  ul.innerHTML = "";
  items.forEach((c) => {
    ul.append(el("li", { "data-id": c.case_id, onclick: () => guarded(() => openCase(c.case_id)) },
      el("div", { class: "ci-title" }, c.case_number || c.case_id),
      el("div", { class: "ci-sub" },
        `${c.box} · ${c.page_count ?? "?"} pp`,
        el("span", { class: statusClass(c.review_status), style: "margin-left:8px" }, c.review_status)),
      el("div", { class: "ci-sub" }, [c.plaintiff, c.defendant].filter(Boolean).join(" v. ")),
    ));
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
  syncTranscriptPanelFromCase();
  if (state.lightbox.open) {
    const idx = findPageIndexByFilename(state.lightbox.filename);
    if (idx >= 0) showLightboxPage(idx);
    else closeLightbox();
  }
}

function yesNoSelect(name, tri) {
  const eff = effective(tri) ?? "";
  const sel = el("select", { "data-field": name });
  [["", "—"], ["Yes", "Yes"], ["No", "No"]].forEach(([val, label]) => {
    sel.append(el("option", { value: val, ...(eff === val ? { selected: "" } : {}) }, label));
  });
  sel.addEventListener("change", () => setDirty(true));
  return sel;
}

function fieldEditor(name, tri, confidence) {
  const eff = effective(tri) ?? "";
  const gem = tri.gemini;
  const isEdited = tri.edited !== null && tri.edited !== undefined;

  let input;
  if (PANEL_FIELDS.has(name)) {
    const preview = eff ? (eff.length > 120 ? eff.slice(0, 120) + "…" : eff) : "(empty)";
    const hidden = el("input", { type: "hidden", "data-field": name, value: eff });
    input = el("div", { class: "field-actions" },
      hidden,
      el("span", { class: "muted", style: "flex:1;font-size:12.5px" }, preview),
      el("button", { type: "button", class: "ghost", onclick: () => openTranscriptPanel() }, "Open full transcript"),
    );
  } else if (YES_NO_FIELDS.has(name)) {
    input = yesNoSelect(name, tri);
  } else if (LONG_FIELDS.has(name)) {
    input = el("textarea", { rows: 4, "data-field": name }, eff);
    input.addEventListener("input", () => setDirty(true));
  } else {
    input = el("input", { type: "text", "data-field": name, value: eff });
    input.addEventListener("input", () => setDirty(true));
  }

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

  const statusSel = el("select", { id: "review-status" });
  state.meta.statuses.forEach((s) =>
    statusSel.append(el("option", { value: s, ...(s === c.review_status ? { selected: "" } : {}) }, s)));
  statusSel.addEventListener("change", () => setDirty(true));

  root.append(el("div", { class: "controls-row" },
    el("strong", {}, "Review status:"), statusSel,
    el("span", { class: statusClass(c.review_status), id: "status-chip" }, c.review_status)));

  const left = el("div", {});
  const right = el("div", {});

  const fieldsCard = el("div", { class: "card" }, el("h3", {}, "Extracted fields"));
  state.meta.fields.forEach((name) => {
    fieldsCard.append(fieldEditor(name, c.fields[name] || { gemini: null, edited: null }, c.field_confidence[name]));
  });
  left.append(fieldsCard);

  const notesTri = c.notes || { gemini: null, edited: null };
  const notes = el("textarea", {
    id: "notes",
    rows: 5,
    placeholder: "Your annotations while reading documents…",
  }, effective(notesTri) ?? "");
  notes.addEventListener("input", () => setDirty(true));
  left.append(el("div", { class: "card" },
    el("h3", {}, "Notes"),
    el("p", { class: "muted notes-hint" }, "Your own annotations only — not filled by the pipeline."),
    notes));

  const thumbs = el("div", { class: "thumbs" });
  c.source_images.forEach((fn) => {
    const params = new URLSearchParams({ box: c.box, filename: fn });
    thumbs.append(el("img", {
      class: "thumb",
      "data-filename": fn,
      loading: "lazy",
      src: "/api/thumb?" + params.toString(),
      title: fn,
      alt: fn,
    }));
  });
  right.append(el("div", { class: "card" }, el("h3", {}, `Images (${c.source_images.length})`), thumbs));

  const pagesCard = el("div", { class: "card" }, el("h3", {}, `Page transcripts (${c.pages.length})`));
  sortedPages(c).forEach((p) => {
    const tri = p.transcript || { gemini: null, edited: null };
    const eff = effective(tri) ?? "";
    const isEdited = tri.edited !== null && tri.edited !== undefined;
    const ta = el("textarea", { rows: 8, "data-page": p.filename }, eff);
    ta.addEventListener("input", () => {
      setDirty(true);
      syncLightboxFromPageTextarea(p.filename);
    });
    const head = el("div", { class: "page-head" },
      el("strong", {}, `#${p.order} ${p.filename}`),
      el("span", { class: "pt" }, p.page_type),
      el("span", { class: "edited-flag" + (isEdited ? "" : " hidden") }, "edited"),
      el("button", {
        type: "button",
        class: "ghost view-image-btn",
        "data-action": "view-image",
        "data-filename": p.filename,
      }, "view image"));
    pagesCard.append(el("div", { class: "page-block" }, head, ta));
  });
  right.append(pagesCard);

  root.append(el("div", { class: "detail-grid" }, left, right));

  const saveBtn = el("button", { id: "save-btn", class: "primary", disabled: "", onclick: () => saveCase() }, "Save");
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
  const notesTri = c.notes || { gemini: null, edited: null };
  const notesGem = notesTri.gemini ?? "";
  const notesVal = $("#notes").value;
  return {
    fields,
    pages,
    review_status: $("#review-status").value,
    notes: (notesVal === notesGem) ? null : notesVal,
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
    syncTranscriptPanelFromCase();
    if (state.lightbox.open) {
      const idx = findPageIndexByFilename(state.lightbox.filename);
      if (idx >= 0) showLightboxPage(idx);
    }
    state.meta = await api("/api/meta");
    refreshStatusCounts();
    if (state.lastListMode.type === "search" && $("#q").value.trim()) {
      await runSearch();
    } else {
      await loadCaseList();
      highlightActive();
    }
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
    syncTranscriptPanelFromCase();
    if (state.lightbox.open) {
      const idx = findPageIndexByFilename(state.lightbox.filename);
      if (idx >= 0) showLightboxPage(idx);
    }
  }
}

// ----------------------------------------------------------------------------
// Full transcript side panel
// ----------------------------------------------------------------------------
function openTranscriptPanel() {
  state.transcriptPanelOpen = true;
  $("#transcript-panel").classList.remove("hidden");
  $("#transcript-panel").setAttribute("aria-hidden", "false");
  document.body.classList.add("transcript-panel-open");
  syncTranscriptPanelFromCase();
}

function closeTranscriptPanel() {
  state.transcriptPanelOpen = false;
  $("#transcript-panel").classList.add("hidden");
  $("#transcript-panel").setAttribute("aria-hidden", "true");
  document.body.classList.remove("transcript-panel-open");
}

function syncTranscriptPanelFromCase() {
  if (!state.transcriptPanelOpen || !state.currentCase) return;
  const tri = state.currentCase.fields.full_transcript || { gemini: null, edited: null };
  const val = effective(tri) ?? "";
  const ta = $("#transcript-panel-body");
  if (document.activeElement !== ta) ta.value = val;
  const hidden = document.querySelector('input[type="hidden"][data-field="full_transcript"]');
  if (hidden) hidden.value = val;
}

function onTranscriptPanelInput() {
  setDirty(true);
  const ta = $("#transcript-panel-body");
  const hidden = document.querySelector('input[type="hidden"][data-field="full_transcript"]');
  if (hidden) hidden.value = ta.value;
}

// ----------------------------------------------------------------------------
// Page review panel (image + page transcript)
// ----------------------------------------------------------------------------
function ensureLightboxDom() {
  const ids = [
    "lightbox", "lightbox-close", "lightbox-img", "lightbox-img-viewport",
    "lightbox-caption", "lightbox-transcript", "lightbox-edited-flag",
    "lightbox-zoom-in", "lightbox-zoom-out", "lightbox-zoom-fit", "lightbox-zoom-label",
  ];
  const missing = ids.filter((id) => !document.getElementById(id));
  if (missing.length) {
    throw new Error(`Lightbox DOM missing: ${missing.join(", ")} (HTML/JS version mismatch — hard-refresh the page)`);
  }
}

// ----------------------------------------------------------------------------
// Page review image zoom + pan
// ----------------------------------------------------------------------------
const ZOOM_STEP = 1.25;
const ZOOM_MIN = 0.25;
const ZOOM_MAX = 8;

function initLightboxZoom() {
  $("#lightbox-zoom-in").addEventListener("click", () => lightboxZoomIn());
  $("#lightbox-zoom-out").addEventListener("click", () => lightboxZoomOut());
  $("#lightbox-zoom-fit").addEventListener("click", () => lightboxZoomFit());
  $("#lightbox-img").addEventListener("load", onLightboxImageLoad);

  const viewport = $("#lightbox-img-viewport");
  viewport.addEventListener("wheel", onLightboxImageWheel, { passive: false });

  let pan = null;
  viewport.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || !viewport.classList.contains("can-pan")) return;
    e.preventDefault();
    pan = {
      x: e.clientX,
      y: e.clientY,
      scrollLeft: viewport.scrollLeft,
      scrollTop: viewport.scrollTop,
    };
    viewport.classList.add("panning");
  });
  document.addEventListener("mousemove", (e) => {
    if (!pan) return;
    viewport.scrollLeft = pan.scrollLeft - (e.clientX - pan.x);
    viewport.scrollTop = pan.scrollTop - (e.clientY - pan.y);
  });
  document.addEventListener("mouseup", () => {
    if (!pan) return;
    pan = null;
    viewport.classList.remove("panning");
  });

  window.addEventListener("resize", () => {
    if (!state.lightbox.open || !$("#lightbox-img").naturalWidth) return;
    state.imageZoom.fitScale = computeLightboxFitScale();
    applyLightboxImageZoom();
    if (state.imageZoom.userScale <= 1.001) resetLightboxViewportScroll();
  });
}

function computeLightboxFitScale() {
  const viewport = $("#lightbox-img-viewport");
  const img = $("#lightbox-img");
  if (!img.naturalWidth || !img.naturalHeight) return 1;
  const pad = 20;
  const cw = Math.max(1, viewport.clientWidth - pad);
  const ch = Math.max(1, viewport.clientHeight - pad);
  return Math.min(cw / img.naturalWidth, ch / img.naturalHeight);
}

function applyLightboxImageZoom() {
  const img = $("#lightbox-img");
  const viewport = $("#lightbox-img-viewport");
  if (!img.naturalWidth) return;
  const scale = state.imageZoom.fitScale * state.imageZoom.userScale;
  img.style.width = Math.round(img.naturalWidth * scale) + "px";
  img.style.height = Math.round(img.naturalHeight * scale) + "px";
  const canPan = state.imageZoom.userScale > 1.001
    || img.offsetWidth > viewport.clientWidth
    || img.offsetHeight > viewport.clientHeight;
  viewport.classList.toggle("can-pan", canPan);
  updateLightboxZoomLabel();
}

function resetLightboxViewportScroll() {
  const viewport = $("#lightbox-img-viewport");
  if (state.imageZoom.userScale <= 1.001) {
    viewport.scrollLeft = 0;
    viewport.scrollTop = 0;
  } else {
    viewport.scrollLeft = Math.max(0, (viewport.scrollWidth - viewport.clientWidth) / 2);
    viewport.scrollTop = Math.max(0, (viewport.scrollHeight - viewport.clientHeight) / 2);
  }
}

function updateLightboxZoomLabel() {
  const label = $("#lightbox-zoom-label");
  const pct = Math.round(state.imageZoom.userScale * 100);
  label.textContent = pct === 100 ? "Fit" : `${pct}%`;
}

function resetLightboxImageZoom() {
  state.imageZoom = { fitScale: 1, userScale: 1 };
  const viewport = $("#lightbox-img-viewport");
  viewport.classList.remove("can-pan", "panning");
  viewport.scrollLeft = 0;
  viewport.scrollTop = 0;
  updateLightboxZoomLabel();
}

function onLightboxImageLoad() {
  state.imageZoom.fitScale = computeLightboxFitScale();
  state.imageZoom.userScale = 1;
  applyLightboxImageZoom();
  resetLightboxViewportScroll();
}

function lightboxZoomIn() {
  if (!state.lightbox.open) return;
  state.imageZoom.userScale = Math.min(state.imageZoom.userScale * ZOOM_STEP, ZOOM_MAX);
  applyLightboxImageZoom();
}

function lightboxZoomOut() {
  if (!state.lightbox.open) return;
  state.imageZoom.userScale = Math.max(state.imageZoom.userScale / ZOOM_STEP, ZOOM_MIN);
  applyLightboxImageZoom();
  resetLightboxViewportScroll();
}

function lightboxZoomFit() {
  if (!state.lightbox.open) return;
  state.imageZoom.userScale = 1;
  if ($("#lightbox-img").naturalWidth) {
    state.imageZoom.fitScale = computeLightboxFitScale();
    applyLightboxImageZoom();
  }
  resetLightboxViewportScroll();
}

function onLightboxImageWheel(e) {
  if (!state.lightbox.open) return;
  if (!(e.ctrlKey || e.metaKey)) return;
  e.preventDefault();
  if (e.deltaY < 0) lightboxZoomIn();
  else lightboxZoomOut();
}

function setLightboxImageSrc(src) {
  resetLightboxImageZoom();
  const img = $("#lightbox-img");
  img.removeAttribute("style");
  img.src = src;
  if (img.complete && img.naturalWidth) onLightboxImageLoad();
}

function onDetailImageClick(e) {
  const thumb = e.target.closest(".thumb[data-filename]");
  if (thumb) {
    openLightboxForFilename(thumb.getAttribute("data-filename"));
    return;
  }
  const btn = e.target.closest('[data-action="view-image"]');
  if (btn) {
    openLightboxForFilename(btn.getAttribute("data-filename"));
  }
}

function findPageIndexByFilename(filename) {
  if (!state.currentCase) return -1;
  const pages = sortedPages(state.currentCase);
  return pages.findIndex((p) => p.filename === filename);
}

function pageTranscriptForFilename(filename) {
  if (!state.currentCase) return { gemini: null, edited: null };
  const page = state.currentCase.pages.find((p) => p.filename === filename);
  return page ? (page.transcript || { gemini: null, edited: null }) : { gemini: null, edited: null };
}

function openLightboxForFilename(filename) {
  if (!state.currentCase) return;
  const idx = findPageIndexByFilename(filename);
  if (idx >= 0) {
    showLightboxPage(idx);
    return;
  }
  state.lightbox = { open: true, pageIndex: -1, filename };
  const params = new URLSearchParams({ box: state.currentCase.box, filename });
  setLightboxImageSrc("/api/image?" + params.toString());
  $("#lightbox-caption").textContent = `${filename} · ${state.currentCase.box}`;
  const ta = $("#lightbox-transcript");
  ta.value = "";
  ta.disabled = true;
  $("#lightbox-edited-flag").classList.add("hidden");
  revealLightbox();
}

function showLightboxPage(pageIndex) {
  if (!state.currentCase) return;
  const pages = sortedPages(state.currentCase);
  if (!pages.length) return;
  const idx = Math.max(0, Math.min(pageIndex, pages.length - 1));
  const page = pages[idx];
  state.lightbox = { open: true, pageIndex: idx, filename: page.filename };

  const params = new URLSearchParams({ box: state.currentCase.box, filename: page.filename });
  setLightboxImageSrc("/api/image?" + params.toString());
  $("#lightbox-caption").textContent = `#${page.order} ${page.filename} · ${page.page_type} · ${state.currentCase.box}`;

  const tri = page.transcript || { gemini: null, edited: null };
  const ta = $("#lightbox-transcript");
  ta.value = effective(tri) ?? "";
  ta.disabled = false;
  const isEdited = tri.edited !== null && tri.edited !== undefined;
  $("#lightbox-edited-flag").classList.toggle("hidden", !isEdited);

  revealLightbox();
}

function revealLightbox() {
  const lb = $("#lightbox");
  lb.classList.remove("hidden");
  lb.setAttribute("aria-hidden", "false");
  document.body.classList.add("page-review-open");
}

function closeLightbox() {
  state.lightbox = { open: false, pageIndex: -1, filename: null };
  const lb = $("#lightbox");
  lb.classList.add("hidden");
  lb.setAttribute("aria-hidden", "true");
  document.body.classList.remove("page-review-open");
  resetLightboxImageZoom();
  $("#lightbox-img").removeAttribute("style");
  $("#lightbox-img").src = "";
}

function navigateLightboxPage(delta) {
  if (!state.currentCase) return;
  const pages = sortedPages(state.currentCase);
  if (!pages.length) return;
  const next = state.lightbox.pageIndex + delta;
  if (next < 0 || next >= pages.length) return;
  showLightboxPage(next);
}

function onLightboxTranscriptInput() {
  if (!state.currentCase || !state.lightbox.filename) return;
  const val = $("#lightbox-transcript").value;
  const fn = state.lightbox.filename;
  const pageTa = document.querySelector(`[data-page="${CSS.escape(fn)}"]`);
  if (pageTa && pageTa.value !== val) pageTa.value = val;
  setDirty(true);
  const tri = pageTranscriptForFilename(fn);
  const isEdited = tri.edited !== null && tri.edited !== undefined || val !== (tri.gemini ?? "");
  $("#lightbox-edited-flag").classList.toggle("hidden", !isEdited && val === (tri.gemini ?? ""));
}

function syncLightboxFromPageTextarea(filename) {
  if (!state.lightbox.open || state.lightbox.filename !== filename) return;
  const pageTa = document.querySelector(`[data-page="${CSS.escape(filename)}"]`);
  const lb = $("#lightbox-transcript");
  if (pageTa && document.activeElement !== lb) lb.value = pageTa.value;
  const tri = pageTranscriptForFilename(filename);
  const val = pageTa ? pageTa.value : "";
  const isEdited = tri.edited !== null && tri.edited !== undefined || val !== (tri.gemini ?? "");
  $("#lightbox-edited-flag").classList.toggle("hidden", !isEdited && val === (tri.gemini ?? ""));
}

init().catch((e) => {
  document.body.innerHTML = `<pre style="padding:20px;color:#dc2626">Failed to start: ${e.message}</pre>`;
});
