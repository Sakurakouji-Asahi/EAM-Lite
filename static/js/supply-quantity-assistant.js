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
    const whole = quantity / 10000n;
    const fraction = String(quantity % 10000n).padStart(4, "0").replace(/0+$/, "");
    return fraction ? `${whole}.${fraction}` : String(whole);
  };

  const quantityFeedback = (limitValue, enteredValue) => {
    const limit = parseQuantity(limitValue);
    if (limit === null) return { state: "invalid" };
    if (limit === 0n) return { state: "none" };
    if (!String(enteredValue ?? "").trim()) return { state: "empty" };
    const entered = parseQuantity(enteredValue);
    if (entered === null || entered === 0n) return { state: "invalid" };
    if (entered > limit) return { state: "over", excess: formatQuantity(entered - limit) };
    return { state: entered === limit ? "full" : "partial", remaining: formatQuantity(limit - entered) };
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseQuantity, formatQuantity, quantityFeedback };
  }
  if (typeof document === "undefined") return;

  document.addEventListener("DOMContentLoaded", () => {
    for (const assistant of document.querySelectorAll("[data-supply-quantity-assistant]")) {
      const field = document.getElementById(assistant.dataset.quantityField);
      const feedback = assistant.querySelector("[data-quantity-feedback]");
      const fillButton = assistant.querySelector("[data-quantity-fill-all]");
      if (!field || !feedback || field.dataset.quantityLimit === undefined) continue;
      const limit = field.dataset.quantityLimit;
      const unit = field.dataset.quantityUnit || "";
      const remainingLabel = assistant.dataset.remainingLabel || "处理后预计剩余";
      const update = () => {
        const result = quantityFeedback(limit, field.value);
        feedback.classList.toggle("text-danger", result.state === "invalid" || result.state === "over" || result.state === "none");
        feedback.classList.toggle("text-secondary", result.state !== "invalid" && result.state !== "over" && result.state !== "none");
        if (result.state === "none") {
          feedback.textContent = "当前已无可处理数量，请返回来源记录核对。";
        } else if (result.state === "empty") {
          feedback.textContent = `可填写部分数量，当前最多 ${limit} ${unit}。`;
        } else if (result.state === "invalid") {
          feedback.textContent = "请填写大于 0 的数量，最多保留 4 位小数。";
        } else if (result.state === "over") {
          feedback.textContent = `超出当前可处理数量 ${result.excess} ${unit}，请调整后再提交。`;
        } else {
          feedback.textContent = `${remainingLabel}：${result.remaining} ${unit}${result.state === "full" ? "（本次处理全部数量）" : "（本次仅处理部分数量）"}。`;
        }
      };
      field.addEventListener("input", update);
      field.addEventListener("change", update);
      if (fillButton) {
        fillButton.disabled = !parseQuantity(limit);
        fillButton.addEventListener("click", () => {
          field.value = limit;
          field.dispatchEvent(new Event("input", { bubbles: true }));
          field.focus();
        });
      }
      assistant.hidden = false;
      update();
    }
  });
})();
