(() => {
  "use strict";
  const form = document.querySelector("form[data-transfer-review]");
  const summary = document.querySelector("[data-transfer-summary]");
  if (!form || !summary) return;
  const rows = Array.from(summary.querySelectorAll("[data-transfer-field]"));
  const message = summary.querySelector("[data-transfer-message]");
  const update = () => {
    let changed = 0;
    let missing = 0;
    rows.forEach(row => {
      const field = form.elements.namedItem(row.dataset.transferField);
      if (!(field instanceof HTMLSelectElement)) return;
      const option = field.selectedOptions[0];
      const valid = Boolean(field.value && option && !option.disabled && !option.hidden);
      const isChanged = valid && field.value !== row.dataset.originalId;
      const state = row.querySelector("[data-transfer-state]");
      row.querySelector("[data-transfer-target]").textContent = valid ? option.textContent.trim() : "请选择有效目标";
      state.textContent = !valid ? "待选择" : isChanged ? "将变更" : "保持不变";
      state.className = "badge " + (!valid ? "text-bg-warning" : isChanged ? "text-bg-primary" : "text-bg-secondary");
      if (!valid) missing += 1;
      else if (isChanged) changed += 1;
    });
    message.textContent = missing ? `还有 ${missing} 项待选择。换部门后，请重新选择该部门的责任人。`
      : changed ? `将变更 ${changed} 项归属资料。请核对目标和下方原因后保存。`
      : "三项均保持不变。只修改实际变化的项目，再填写生效时间和原因。";
  };
  rows.forEach(row => {
    const field = form.elements.namedItem(row.dataset.transferField);
    field?.addEventListener("input", update);
    field?.addEventListener("change", update);
  });
  update();
})();
