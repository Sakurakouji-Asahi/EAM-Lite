(() => {
  "use strict";
  const button = document.querySelector("[data-print-checklist]");
  if (button) {
    button.hidden = false;
    button.addEventListener("click", () => window.print());
  }
})();
