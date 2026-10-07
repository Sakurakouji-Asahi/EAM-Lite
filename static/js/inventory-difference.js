(() => {
  "use strict";
  const form = document.querySelector("[data-inventory-difference]");
  if (!form) return;
  const type = form.elements.namedItem("resolution_type");
  const targets = form.querySelector("[data-difference-master-fields]");
  const effectiveAt = form.elements.namedItem("effective_at");
  const hint = form.querySelector("[data-difference-selection-hint]");
  if (!type || !targets || !effectiveAt) return;
  const explanations = {
    master_updated: "保存将执行正式资产主档变动，请核对目标值和生效时间。",
    master_confirmed: "保存核实结论，资产主档保留。请说明现场差异的原因和核实依据。",
    loss_confirmed: "保存盘亏结论，资产主档保留；后续处置需另行办理。",
    other: "保存其他处理结论，资产主档保留。请说明核实结果和后续安排。"
  };
  const update = () => {
    const changesMaster = type.value === "master_updated";
    // Keep entered targets and visible field errors when switching outcomes.
    targets.hidden = !changesMaster && targets.dataset.hasErrors !== "true";
    effectiveAt.required = changesMaster;
    effectiveAt.setAttribute("aria-required", String(changesMaster));
    if (hint) hint.textContent = explanations[type.value] || "请先选择与核实情况相符的处理结论。";
  };
  type.addEventListener("change", update);
  update();
})();
