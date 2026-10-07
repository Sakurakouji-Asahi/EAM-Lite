(() => {
  "use strict";
  const button = document.querySelector("[data-print-information-card]");
  if (!button || typeof window.print !== "function") return;
  button.hidden = false;
  button.addEventListener("click", () => window.print());
})();
