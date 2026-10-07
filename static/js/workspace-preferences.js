(() => {
  "use strict";
  const scope = document.body.dataset.preferenceScope;
  if (!scope) return;
  const key = `eam-table-density:v1:${scope}`;
  let density = "comfortable";
  try { if (localStorage.getItem(key) === "compact") density = "compact"; } catch (_) { /* Browser preferences are optional. */ }
  const buttons = Array.from(document.querySelectorAll("[data-table-density]"));
  const apply = value => {
    density = value === "compact" ? "compact" : "comfortable";
    document.body.classList.toggle("app-density-compact", density === "compact");
    buttons.forEach(button => button.setAttribute("aria-pressed", String(button.dataset.tableDensity === density)));
  };
  apply(density);
  document.querySelectorAll("[data-table-density-controls]").forEach(control => { control.hidden = false; });
  buttons.forEach(button => button.addEventListener("click", event => {
    event.stopPropagation();
    apply(button.dataset.tableDensity);
    try { localStorage.setItem(key, density); } catch (_) { /* Keep the current view usable without storage. */ }
  }));
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) apply(event.key === null ? "comfortable" : event.newValue);
  });
})();
