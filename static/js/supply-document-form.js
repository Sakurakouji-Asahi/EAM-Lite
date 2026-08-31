document.addEventListener("DOMContentLoaded", () => {
  const addButton = document.getElementById("add-supply-line");
  const rows = document.getElementById("supply-line-rows");
  const template = document.getElementById("empty-supply-line");
  const totalForms = document.getElementById("id_lines-TOTAL_FORMS");
  const maxForms = document.getElementById("id_lines-MAX_NUM_FORMS");
  const status = document.getElementById("supply-line-status");
  if (!addButton || !rows || !template || !totalForms || !maxForms) return;

  addButton.addEventListener("click", () => {
    const nextIndex = Number.parseInt(totalForms.value, 10);
    const maximum = Number.parseInt(maxForms.value, 10);
    if (!Number.isInteger(nextIndex) || nextIndex >= maximum) {
      addButton.disabled = true;
      return;
    }
    rows.insertAdjacentHTML(
      "beforeend",
      template.innerHTML.replaceAll("__prefix__", String(nextIndex)),
    );
    totalForms.value = String(nextIndex + 1);
    const newRow = rows.lastElementChild;
    const firstField = newRow?.querySelector("select, input:not([type='hidden'])");
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    newRow?.scrollIntoView({
      behavior: reduceMotion ? "auto" : "smooth",
      block: "center",
    });
    firstField?.focus({ preventScroll: true });
    if (status) status.textContent = `已新增第 ${nextIndex + 1} 行明细。`;
    if (nextIndex + 1 >= maximum) addButton.disabled = true;
  });
});
