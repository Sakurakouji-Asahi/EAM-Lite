(() => {
  "use strict";

  for (const form of document.querySelectorAll("[data-inventory-scan-entry]")) {
    const input = form.elements.namedItem("token");
    if (!input) continue;
    form.addEventListener("submit", (event) => {
      input.value = input.value.trim();
      if (!input.value) {
        event.preventDefault();
        input.reportValidity();
      }
    });
  }

  for (const form of document.querySelectorAll("[data-inventory-scan-result]")) {
    const otherMismatch = form.elements.namedItem("other_mismatch");
    const note = form.elements.namedItem("note");
    const hint = form.querySelector("[data-inventory-note-required]");
    if (!otherMismatch || !note || !hint) continue;
    const updateRequirement = () => {
      note.required = otherMismatch.checked;
      hint.hidden = !otherMismatch.checked;
    };
    otherMismatch.addEventListener("change", updateRequirement);
    updateRequirement();
  }
})();
