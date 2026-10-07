(() => {
  "use strict";

  const initialize = () => {
    document.querySelectorAll("[data-scope-assets]").forEach((panel) => {
      if (panel.dataset.inventorySelectionReady) return;
      const search = panel.querySelector("[data-inventory-asset-search]");
      const tools = panel.querySelector("[data-inventory-selection-tools]");
      const summary = panel.querySelector("[data-inventory-selection-summary]");
      const selectVisible = panel.querySelector("[data-inventory-select-visible]");
      const clearVisible = panel.querySelector("[data-inventory-clear-visible]");
      const empty = panel.querySelector("[data-inventory-selection-empty]");
      if (!search || !tools || !summary || !selectVisible || !clearVisible) return;
      const normalize = (value) => value.toLocaleLowerCase().replace(/\s+/g, " ").trim();
      const rows = Array.from(panel.querySelectorAll("[data-inventory-asset-row]")).map((row) => ({
        row, checkbox: row.querySelector('input[name="selected_asset_ids_ui"]'),
        text: normalize(row.textContent),
      })).filter((item) => item.checkbox);

      const updateSummary = () => {
        const visible = rows.filter((item) => !item.row.hidden);
        const selected = rows.filter((item) => item.checkbox.checked);
        const hiddenSelected = selected.filter((item) => item.row.hidden).length;
        summary.textContent = `显示 ${visible.length} / ${rows.length} 项，已选 ${selected.length} 项`
          + (hiddenSelected ? `（含未显示 ${hiddenSelected} 项）` : "");
        selectVisible.disabled = visible.every((item) => item.checkbox.checked);
        clearVisible.disabled = !visible.some((item) => item.checkbox.checked);
        if (empty) empty.hidden = visible.length > 0 || rows.length === 0;
      };
      const filterRows = () => {
        const terms = normalize(search.value).split(" ").filter(Boolean);
        rows.forEach((item) => {
          // Keep every checkbox enabled and checked state intact across searches.
          item.row.hidden = !terms.every((term) => item.text.includes(term));
        });
        updateSummary();
      };
      const setVisible = (checked) => {
        rows.filter((item) => !item.row.hidden && !item.checkbox.disabled).forEach((item) => {
          item.checkbox.checked = checked;
        });
        panel.dispatchEvent(new Event("change", { bubbles: true }));
      };

      search.addEventListener("input", filterRows);
      search.addEventListener("search", filterRows);
      search.addEventListener("keydown", (event) => {
        if (event.key === "Enter") event.preventDefault();
      });
      selectVisible.addEventListener("click", () => setVisible(true));
      clearVisible.addEventListener("click", () => setVisible(false));
      panel.addEventListener("change", updateSummary);
      window.addEventListener("pageshow", filterRows);
      tools.hidden = false;
      panel.dataset.inventorySelectionReady = "true";
      filterRows();
    });
  };

  initialize();
  document.addEventListener("htmx:load", initialize);
})();
