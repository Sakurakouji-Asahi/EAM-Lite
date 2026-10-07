"use strict";

// Only for reviewing entered quantities. Accounting remains on the server.
function parseUsageMicroUnits(raw) {
  const match = String(raw).trim().match(/^([+-])?(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/);
  if (!match) return null;
  const fraction = match[3] === undefined ? (match[4] || "") : match[3];
  const exponent = Number(match[5] || "0");
  if (!Number.isSafeInteger(exponent) || fraction.length - exponent > 6) return null;
  const digits = ((match[2] || "0") + fraction).replace(/^0+/, "") || "0";
  if (digits === "0") return 0n;
  if (match[1] === "-") return null;
  const shift = 6 + exponent - fraction.length;
  if (shift < 0 || digits.length + shift > 24) return null;
  return BigInt(digits + "0".repeat(shift));
}

function formatUsageMicroUnits(value) {
  const whole = value / 1000000n;
  const fraction = String(value % 1000000n).padStart(6, "0").replace(/0+$/, "");
  return String(whole) + (fraction ? "." + fraction : "");
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {parseUsageMicroUnits, formatUsageMicroUnits};
}

if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-monthly-usage]").forEach((form) => {
    const rows = Array.from(form.querySelectorAll("[data-usage-row]")).map((element) => ({
      element, units: element.querySelector("[data-usage-units]"), remark: element.querySelector("[data-usage-remark]"),
      state: element.querySelector("[data-usage-row-state]"), unit: element.dataset.usageUnit,
      existing: element.dataset.usageExisting === "true",
    }));
    const progress = form.querySelector("[data-usage-progress]");
    const totals = form.querySelector("[data-usage-totals]");
    const status = form.querySelector("[data-usage-bulk-status]");
    const undo = form.querySelector("[data-usage-undo-zero]");
    let filledZero = [];
    const refresh = () => {
      let filled = 0, blank = 0, invalid = 0;
      const byUnit = new Map();
      rows.forEach((row) => {
        const text = row.units.value.trim();
        const value = parseUsageMicroUnits(text);
        const remarkTooLong = row.remark.value.length > 2000;
        const remarkWithoutUnits = text === "" && row.remark.value.trim() !== "";
        const needsCheck = row.units.validity.badInput || remarkTooLong || (value === null && (text !== "" || remarkWithoutUnits));
        row.element.classList.toggle("usage-row-invalid", needsCheck);
        row.state.classList.toggle("text-danger", needsCheck);
        row.state.classList.toggle("text-secondary", !needsCheck);
        if (needsCheck) {
          invalid += 1;
          row.state.textContent = remarkTooLong ? "备注最多 2000 字" : (remarkWithoutUnits ? "有备注，需填工作量" : "请核对工作量或小数位");
        }
        else if (value === null) { blank += 1; row.state.textContent = "空白不保存"; }
        else {
          filled += 1;
          row.state.textContent = row.existing ? "已填写，请与已有记录核对" : (value === 0n ? "已填写 0" : "已填写，待保存");
          byUnit.set(row.unit, (byUnit.get(row.unit) || 0n) + value);
        }
      });
      progress.textContent = `本页 ${rows.length} 行 · 已填写 ${filled} 行 · 空白 ${blank} 行` + (invalid ? ` · 待核对 ${invalid} 行` : "");
      totals.replaceChildren();
      byUnit.forEach((value, unit) => {
        const item = document.createElement("li");
        item.textContent = `${unit}输入合计：${formatUsageMicroUnits(value)}`;
        totals.appendChild(item);
      });
      if (!byUnit.size) {
        const item = document.createElement("li");
        item.className = "text-secondary";
        item.textContent = "尚无可汇总的有效工作量输入。";
        totals.appendChild(item);
      }
      filledZero = filledZero.filter((entry) => entry.input.value === "0");
      undo.hidden = filledZero.length === 0;
    };
    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    form.querySelector("[data-usage-fill-zero]").addEventListener("click", () => {
      const blankRows = rows.filter((row) => !row.existing && row.units.value.trim() === "" && !row.units.validity.badInput);
      blankRows.forEach((row) => {
        filledZero.push({input: row.units, previous: row.units.value});
        row.units.value = "0";
        row.units.dispatchEvent(new Event("input", {bubbles: true}));
      });
      status.textContent = blankRows.length ? `已将 ${blankRows.length} 行空白工作量填为 0；其他输入保留。` : "没有可填 0 的空白行。";
      refresh();
    });
    undo.addEventListener("click", () => {
      const entries = filledZero;
      filledZero = [];
      entries.forEach((entry) => {
        if (entry.input.value === "0") {
          entry.input.value = entry.previous;
          entry.input.dispatchEvent(new Event("input", {bubbles: true}));
        }
      });
      status.textContent = "已撤销快捷填 0；随后手动修改的工作量保留。";
      refresh();
    });
    refresh();
    form.querySelector("[data-usage-review]").hidden = false;
  });
});
