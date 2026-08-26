document.addEventListener("DOMContentLoaded", () => {
  const addButton = document.getElementById("add-supply-line");
  const rows = document.getElementById("supply-line-rows");
  const template = document.getElementById("empty-supply-line");
  const totalForms = document.getElementById("id_lines-TOTAL_FORMS");
  const maxForms = document.getElementById("id_lines-MAX_NUM_FORMS");
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
    if (nextIndex + 1 >= maximum) addButton.disabled = true;
  });
});
