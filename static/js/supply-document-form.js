(() => {
  // Quantities use four decimal places. Integer arithmetic preserves large
  // quantities and fractional additions without changing submitted inputs.
  const parseQuantity = (value) => {
    const match = String(value ?? "").trim().match(/^\+?(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/);
    if (!match) return null;
    const fraction = match[2] ?? match[3] ?? "";
    const digits = ((match[1] ?? "0") + fraction).replace(/^0+/, "") || "0";
    const exponent = Number(match[4] || 0) - fraction.length;
    const decimals = Math.max(-exponent, 0);
    const wholeDigits = Math.max(digits.length + exponent, 0);
    if (!Number.isInteger(exponent) || decimals > 4 || wholeDigits > 14 || Math.max(digits.length, decimals) + Math.max(exponent, 0) > 18) return null;
    const quantity = BigInt(digits) * (10n ** BigInt(exponent + 4));
    return quantity > 0n ? quantity : null;
  };

  const formatQuantity = (quantity) => {
    const whole = quantity / 10000n;
    const fraction = String(quantity % 10000n).padStart(4, "0").replace(/0+$/, "");
    return fraction ? `${whole}.${fraction}` : String(whole);
  };

  const summarizeLines = (lines) => {
    const summary = { activeRows: 0, itemCount: 0, deletedRows: 0, blankRows: 0, incompleteRows: 0, totals: [] };
    const items = new Set();
    const units = new Map();
    for (const line of lines) {
      if (line.deleted) {
        summary.deletedRows += 1;
        continue;
      }
      if (!line.item && !String(line.quantity || "").trim() && !line.hasOtherInput) {
        summary.blankRows += 1;
        continue;
      }
      const quantity = parseQuantity(line.quantity);
      if (!line.item || !line.unit || quantity === null) {
        summary.incompleteRows += 1;
        continue;
      }
      summary.activeRows += 1;
      items.add(line.item);
      units.set(line.unit, (units.get(line.unit) || 0n) + quantity);
    }
    summary.itemCount = items.size;
    summary.totals = Array.from(units, ([unit, quantity]) => ({ unit, quantity: formatQuantity(quantity) }));
    return summary;
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseQuantity, formatQuantity, summarizeLines };
  }
  if (typeof document === "undefined") return;

  document.addEventListener("DOMContentLoaded", () => {
    const addButton = document.getElementById("add-supply-line");
    const rows = document.getElementById("supply-line-rows");
    const template = document.getElementById("empty-supply-line");
    const totalForms = document.getElementById("id_lines-TOTAL_FORMS");
    const maxForms = document.getElementById("id_lines-MAX_NUM_FORMS");
    const status = document.getElementById("supply-line-status");
    const preview = document.getElementById("supply-line-preview");
    const count = document.getElementById("supply-line-count");
    const totals = document.getElementById("supply-unit-totals");
    if (!addButton || !rows || !template || !totalForms || !maxForms) return;

    const refreshPreview = () => {
      const lines = Array.from(rows.children, (row, index) => {
        const lineNumber = row.querySelector("[data-line-number]");
        if (lineNumber) lineNumber.textContent = `第 ${index + 1} 行`;
        const item = row.querySelector("select[name$='-item']");
        const quantity = row.querySelector("input[name$='-quantity']");
        const deletion = row.querySelector("input[name$='-DELETE']");
        const deleted = Boolean(deletion?.checked);
        const unit = item?.selectedOptions[0]?.dataset.itemUnit || "";
        const unitHint = row.querySelector("[data-line-unit]");
        const deletedHint = row.querySelector("[data-line-deleted]");
        row.classList.toggle("supply-line-is-deleted", deleted);
        if (deletedHint) deletedHint.hidden = !deleted;
        if (unitHint) {
          unitHint.textContent = unit ? `计量单位：${unit}` : "";
          unitHint.hidden = !unit;
        }
        const hasOtherInput = Array.from(row.querySelectorAll("input[name$='-entered_unit_cost'], input[name$='-line_remark']"))
          .some((field) => field.value.trim() !== "");
        return { item: item?.value || "", unit, quantity: quantity?.value || "", deleted, hasOtherInput };
      });
      if (!preview || !count || !totals) return;
      const summary = summarizeLines(lines);
      const pieces = [`数量完整 ${summary.activeRows} 行`, `${summary.itemCount} 种物品`];
      if (summary.incompleteRows) pieces.push(`${summary.incompleteRows} 行物品/数量待补全`);
      if (summary.deletedRows) pieces.push(`${summary.deletedRows} 行已标记删除`);
      count.textContent = pieces.join(" · ");
      totals.replaceChildren();
      for (const total of summary.totals) {
        const badge = document.createElement("span");
        badge.className = "badge text-bg-light border";
        badge.textContent = `${total.quantity} ${total.unit}`;
        totals.appendChild(badge);
      }
      if (!summary.totals.length) {
        const hint = document.createElement("span");
        hint.className = "text-secondary small";
        hint.textContent = "选择物品并填写数量后显示单位合计。";
        totals.appendChild(hint);
      }
      preview.hidden = false;
    };

    rows.addEventListener("input", refreshPreview);
    rows.addEventListener("change", refreshPreview);
    refreshPreview();

    addButton.addEventListener("click", () => {
      const nextIndex = Number.parseInt(totalForms.value, 10);
      const maximum = Number.parseInt(maxForms.value, 10);
      if (!Number.isInteger(nextIndex) || nextIndex >= maximum) {
        addButton.disabled = true;
        if (status) status.textContent = `最多可添加 ${maximum} 行明细。`;
        return;
      }
      rows.insertAdjacentHTML("beforeend", template.innerHTML.replaceAll("__prefix__", String(nextIndex)));
      totalForms.value = String(nextIndex + 1);
      const newRow = rows.lastElementChild;
      const firstField = newRow?.querySelector("select, input:not([type='hidden'])");
      const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      newRow?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
      firstField?.focus({ preventScroll: true });
      if (status) status.textContent = `已新增第 ${nextIndex + 1} 行明细。`;
      if (nextIndex + 1 >= maximum) addButton.disabled = true;
      refreshPreview();
    });
  });
})();
