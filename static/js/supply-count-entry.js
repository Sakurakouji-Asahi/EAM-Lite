(() => {
  const parseQuantity = (value) => {
    const match = String(value ?? "").trim().match(/^\+?(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:[eE]([+-]?\d+))?$/);
    if (!match) return null;
    const fraction = match[2] ?? match[3] ?? "";
    const digits = ((match[1] ?? "0") + fraction).replace(/^0+/, "") || "0";
    const exponent = Number(match[4] || 0) - fraction.length;
    const decimals = Math.max(-exponent, 0);
    const wholeDigits = Math.max(digits.length + exponent, 0);
    if (!Number.isInteger(exponent) || decimals > 4 || wholeDigits > 14 || Math.max(digits.length, decimals) + Math.max(exponent, 0) > 18) return null;
    return BigInt(digits) * (10n ** BigInt(exponent + 4));
  };

  const formatQuantity = (quantity) => {
    const absolute = quantity < 0n ? -quantity : quantity;
    const fraction = String(absolute % 10000n).padStart(4, "0").replace(/0+$/, "");
    return `${quantity < 0n ? "-" : ""}${absolute / 10000n}${fraction ? "." + fraction : ""}`;
  };

  const inspectLine = ({ expected, counted, remark, badInput }) => {
    if (badInput) return { state: "invalid" };
    if (!String(counted ?? "").trim()) return { state: "blank" };
    const expectedQuantity = parseQuantity(expected);
    const countedQuantity = parseQuantity(counted);
    if (expectedQuantity === null || countedQuantity === null) return { state: "invalid" };
    const difference = countedQuantity - expectedQuantity;
    return { state: difference === 0n ? "same" : "different", expectedQuantity, countedQuantity,
      difference, needsReason: difference !== 0n && !String(remark ?? "").trim() };
  };

  const summarizeLines = (lines) => {
    const summary = { total: lines.length, blank: 0, invalid: 0, recorded: 0, different: 0, needsReason: 0, units: new Map() };
    for (const line of lines) {
      const result = inspectLine(line);
      if (result.state === "blank" || result.state === "invalid") {
        summary[result.state] += 1;
        continue;
      }
      summary.recorded += 1;
      if (result.state === "different") summary.different += 1;
      if (result.needsReason) summary.needsReason += 1;
      const unit = line.unit || "未设单位";
      const totals = summary.units.get(unit) || { expected: 0n, counted: 0n, difference: 0n };
      totals.expected += result.expectedQuantity;
      totals.counted += result.countedQuantity;
      totals.difference += result.difference;
      summary.units.set(unit, totals);
    }
    return summary;
  };

  if (typeof module !== "undefined" && module.exports) module.exports = { parseQuantity, formatQuantity, inspectLine, summarizeLines };
  if (typeof document === "undefined") return;

  document.addEventListener("DOMContentLoaded", () => {
    for (const form of document.querySelectorAll("[data-count-entry]")) {
      const preview = form.querySelector("[data-count-preview]");
      const progress = form.querySelector("[data-count-progress]");
      const unitTotals = form.querySelector("[data-count-unit-totals]");
      const rows = Array.from(form.querySelectorAll("[data-count-row]"));
      if (!preview || !progress || !unitTotals || !rows.length) continue;
      const rowValues = (row) => {
        const quantity = row.querySelector("[name$='counted_quantity']");
        return { expected: row.dataset.countExpected, unit: row.dataset.countUnit, counted: quantity?.value,
          remark: row.querySelector("[name$='remark']")?.value, badInput: Boolean(quantity?.validity.badInput) };
      };
      const nextBlank = form.querySelector("[data-count-next-blank]");
      const nextReason = form.querySelector("[data-count-next-reason]");
      let activeRowIndex = -1;
      form.addEventListener("focusin", (event) => {
        const index = rows.findIndex((row) => row.contains(event.target));
        if (index >= 0) activeRowIndex = index;
      });
      const update = () => {
        const values = rows.map(rowValues);
        const summary = summarizeLines(values);
        progress.textContent = `本页已填写 ${summary.recorded} / ${summary.total} 行 · 空白 ${summary.blank} · 差异 ${summary.different} · 待填原因 ${summary.needsReason}${summary.invalid ? ` · 数量待修正 ${summary.invalid}` : ""}`;
        progress.classList.toggle("text-danger", summary.invalid > 0 || summary.needsReason > 0);
        progress.classList.toggle("text-secondary", summary.invalid === 0 && summary.needsReason === 0);
        unitTotals.replaceChildren();
        for (const [unit, totals] of summary.units) {
          const line = document.createElement("div");
          const difference = formatQuantity(totals.difference);
          line.textContent = `${unit}：已填行应盘 ${formatQuantity(totals.expected)} · 实盘 ${formatQuantity(totals.counted)} · 净差异 ${totals.difference > 0n ? "+" : ""}${difference}`;
          unitTotals.append(line);
        }
        if (summary.units.size === 0) unitTotals.textContent = "填写实盘数量后显示各单位核对结果。";
        rows.forEach((row, index) => {
          const result = inspectLine(values[index]);
          const feedback = row.querySelector("[data-count-feedback]");
          const reasonHint = row.querySelector("[data-count-reason-hint]");
          if (feedback) {
            feedback.hidden = false;
            feedback.classList.toggle("text-danger", result.state === "invalid");
            feedback.classList.toggle("text-warning", result.state === "different");
            feedback.classList.toggle("text-success", result.state === "same");
            feedback.classList.toggle("text-secondary", result.state === "blank");
            if (result.state === "blank") feedback.textContent = "尚未填写；盘为零请填 0。";
            else if (result.state === "invalid") feedback.textContent = "请填写非负数量，最多保留 4 位小数。";
            else if (result.state === "same") feedback.textContent = "实盘与应盘一致。";
            else feedback.textContent = `${result.difference > 0n ? "盘盈 +" : "盘亏 "}${formatQuantity(result.difference)} ${values[index].unit || ""}`;
          }
          if (reasonHint) reasonHint.hidden = !result.needsReason;
        });
        if (nextBlank) nextBlank.disabled = summary.blank === 0;
        if (nextReason) nextReason.disabled = summary.needsReason === 0;
      };
      const jump = (kind) => {
        const ordered = rows.slice(activeRowIndex + 1).concat(rows.slice(0, activeRowIndex + 1));
        const row = ordered.find((candidate) => {
          const result = inspectLine(rowValues(candidate));
          return kind === "blank" ? result.state === "blank" : result.needsReason;
        });
        if (!row) return;
        const field = row.querySelector(kind === "blank" ? "[name$='counted_quantity']" : "[name$='remark']");
        field?.focus();
        field?.scrollIntoView({ block: "center", behavior: "smooth" });
      };
      nextBlank?.addEventListener("click", () => jump("blank"));
      nextReason?.addEventListener("click", () => jump("reason"));
      form.addEventListener("input", update);
      form.addEventListener("change", update);
      preview.hidden = false;
      update();
    }
  });
})();
