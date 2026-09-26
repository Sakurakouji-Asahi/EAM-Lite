"use strict";
(() => {
  const workspace = document.getElementById("bulk-workspace");
  if (!workspace) return;
  const key = workspace.dataset.selectionKey;
  const maxSelection = 200;
  let persistent = true;
  let selected = new Set();
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || "[]");
    if (Array.isArray(saved)) {
      selected = new Set(saved.filter(value => typeof value === "string" && /^[0-9a-f-]{36}$/i.test(value)).slice(0, maxSelection));
    }
  } catch (_) { persistent = false; }
  const completed = document.getElementById("bulk-cleared-ids");
  if (completed) JSON.parse(completed.textContent).forEach(id => selected.delete(id));
  const save = () => {
    try { sessionStorage.setItem(key, JSON.stringify(Array.from(selected))); }
    catch (_) { persistent = false; }
  };
  save();
  const form = document.getElementById("bulk-selection-form");
  if (!form) return;
  const all = document.getElementById("bulk-select-all");
  const rows = Array.from(form.querySelectorAll('input[type="checkbox"][name="assets"]'));
  rows.filter(row => row.checked).forEach(row => selected.add(row.value));
  const count = document.getElementById("bulk-selected-count");
  const error = document.getElementById("bulk-selection-error");
  const showError = message => { error.textContent = message; error.hidden = !message; };
  const sync = () => {
    save();
    rows.forEach(row => { row.checked = selected.has(row.value); });
    all.checked = rows.length > 0 && rows.every(row => row.checked);
    all.indeterminate = rows.some(row => row.checked) && !all.checked;
    count.textContent = `已选 ${selected.size} 件${persistent ? "（可跨页，最多200件）" : "（仅本页）"}`;
  };
  all.addEventListener("change", () => {
    const next = new Set(selected);
    rows.forEach(row => { if (all.checked) next.add(row.value); else next.delete(row.value); });
    if (next.size > maxSelection) showError("每批最多选择200件，请先办理当前所选资产。");
    else { selected = next; showError(""); }
    sync();
  });
  rows.forEach(row => row.addEventListener("change", () => {
    if (row.checked && selected.size >= maxSelection && !selected.has(row.value)) {
      showError("每批最多选择200件，请先办理当前所选资产。");
    } else {
      if (row.checked) selected.add(row.value); else selected.delete(row.value);
      showError("");
    }
    sync();
  }));
  document.getElementById("bulk-clear-selection").addEventListener("click", () => {
    selected.clear(); showError(""); sync();
  });
  form.addEventListener("submit", event => {
    if (form.dataset.filteredSelection === "true" &&
        event.submitter?.name === "selection_scope" && event.submitter.value === "filtered") return;
    if (!selected.size || selected.size > maxSelection) {
      event.preventDefault(); showError("请先选择1—200件资产。"); return;
    }
    form.querySelectorAll('[data-cross-page-selection]').forEach(field => field.remove());
    const visible = new Set(rows.map(row => row.value));
    selected.forEach(id => {
      if (!visible.has(id)) {
        const field = document.createElement("input");
        field.type = "hidden"; field.name = "assets"; field.value = id;
        field.dataset.crossPageSelection = "true"; form.appendChild(field);
      }
    });
    save();
  });
  sync();
})();
