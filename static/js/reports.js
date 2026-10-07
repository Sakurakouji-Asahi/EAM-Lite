"use strict";
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-report-page-controls]").forEach((form) => {
    const size = form.querySelector('[name="page_size"]');
    const page = form.querySelector('[name="page"]');
    size.addEventListener("change", () => {
      page.max = String(Math.max(1, Math.ceil(Number(form.dataset.rowCount) / Number(size.value))));
      page.value = "1";
    });
  });
  document.querySelectorAll("[data-report-filters]").forEach((form) => {
    const applied = new URLSearchParams(new FormData(form)).toString();
    const notice = form.querySelector("[data-report-filter-notice]");
    const appliedActions = document.querySelectorAll("[data-report-export]");
    const refreshPendingState = () => {
      const changed = new URLSearchParams(new FormData(form)).toString() !== applied;
      if (notice) notice.hidden = !changed;
      appliedActions.forEach((button) => { button.disabled = changed; });
    };
    form.addEventListener("input", refreshPendingState);
    form.addEventListener("change", refreshPendingState);
    form.querySelectorAll("[data-report-filter-undo]").forEach((button) => {
      button.addEventListener("click", () => {
        form.reset();
        refreshPendingState();
        form.querySelector('[type="submit"]').focus();
      });
    });
    const start = form.querySelector('[name="period_start"], [name="date_from"]');
    const end = form.querySelector('[name="period_end"], [name="date_to"]');
    if (!start || !end) return;
    const format = (date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
    form.querySelectorAll("[data-report-period]").forEach((button) => {
      button.addEventListener("click", () => {
        const today = new Date();
        const year = today.getFullYear();
        const month = today.getMonth();
        const mode = button.dataset.reportPeriod;
        const first = mode === "year" ? new Date(year, 0, 1) : new Date(year, month - (mode === "previous-month" ? 1 : 0), 1);
        const last = mode === "year" ? new Date(year, 11, 31) : new Date(year, month + (mode === "previous-month" ? 0 : 1), 0);
        start.value = format(first);
        end.value = format(last);
        start.dispatchEvent(new Event("change", {bubbles: true}));
        end.dispatchEvent(new Event("change", {bubbles: true}));
      });
    });
  });
  document.querySelectorAll("[data-report-columns]").forEach((controls) => {
    const table = document.getElementById("report-detail-table");
    if (!table) return;
    const toggles = Array.from(controls.querySelectorAll("[data-report-column-toggle]"));
    if (!toggles.length) return;
    const storageKey = ["eam-report-columns-v1", controls.dataset.reportOwner, controls.dataset.reportCompany, controls.dataset.reportKey].join(":");
    let hiddenColumns = [];
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "[]");
      if (Array.isArray(saved)) hiddenColumns = saved.filter((key) => typeof key === "string");
    } catch (_) { /* Column controls also work when browser storage is unavailable. */ }
    const cells = Array.from(table.querySelectorAll("[data-report-column]"));
    const count = controls.querySelector("[data-report-column-count]");
    const apply = (save = true) => {
      const hidden = new Set(toggles.filter((toggle) => !toggle.checked && !toggle.disabled).map((toggle) => toggle.value));
      cells.forEach((cell) => { cell.hidden = hidden.has(cell.dataset.reportColumn); });
      table.toggleAttribute("data-report-columns-reduced", hidden.size > 0);
      const visibleCount = toggles.length - hidden.size;
      if (count) count.textContent = `（${visibleCount}/${toggles.length}）`;
      table.querySelectorAll("[data-report-empty-row]").forEach((cell) => { cell.colSpan = visibleCount; });
      if (save) {
        try { localStorage.setItem(storageKey, JSON.stringify(Array.from(hidden))); }
        catch (_) { /* Keep the current page usable without persisted preferences. */ }
      }
    };
    toggles.forEach((toggle) => {
      toggle.checked = toggle.disabled || !hiddenColumns.includes(toggle.value);
      toggle.addEventListener("change", () => apply());
    });
    controls.querySelector("[data-report-columns-reset]").addEventListener("click", () => {
      toggles.forEach((toggle) => { toggle.checked = true; });
      apply();
    });
    apply(false);
    controls.hidden = false;
  });
});
