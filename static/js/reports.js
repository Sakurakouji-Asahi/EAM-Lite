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
});
