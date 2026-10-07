(() => {
  "use strict";
  document.querySelectorAll("[data-bulk-review]").forEach(section => {
    const controls = section.querySelector("[data-bulk-review-controls]");
    const state = section.querySelector("[data-bulk-review-state]");
    const search = section.querySelector("[data-bulk-review-search]");
    const summary = section.querySelector("[data-bulk-review-summary]");
    const reset = section.querySelector("[data-bulk-review-reset]");
    const empty = section.querySelector("[data-bulk-review-empty]");
    const rows = Array.from(section.querySelectorAll("[data-bulk-review-row]")).map(element => ({
      element, state: element.dataset.bulkReviewRow,
      text: element.textContent.toLocaleLowerCase().replace(/\s+/g, " "),
    }));
    if (!controls || !state || !search || !summary || !reset || !empty || !rows.length) return;
    const errors = rows.filter(row => row.state === "error").length;
    const totals = { all: rows.length, error: errors, ready: rows.length - errors };
    const labels = { all: "全部", error: "需处理", ready: "通过核对" };
    Array.from(state.options).forEach(option => { option.textContent = `${labels[option.value]}（${totals[option.value]} 件）`; });
    const render = () => {
      const terms = search.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
      let visible = 0;
      rows.forEach(row => {
        row.element.hidden = !(state.value === "all" || row.state === state.value) || !terms.every(term => row.text.includes(term));
        if (!row.element.hidden) visible += 1;
      });
      summary.textContent = `当前显示 ${visible} / ${rows.length} 件；本批通过核对 ${totals.ready} 件，需处理 ${totals.error} 件。`;
      empty.hidden = visible > 0;
    };
    state.addEventListener("change", render);
    search.addEventListener("input", render);
    search.addEventListener("keydown", event => { if (event.key === "Enter") event.preventDefault(); });
    reset.addEventListener("click", () => { state.value = "all"; search.value = ""; render(); search.focus(); });
    controls.hidden = false;
    render();
  });
})();
