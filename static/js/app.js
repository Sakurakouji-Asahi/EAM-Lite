(() => {
  "use strict";

  const resetSubmittingState = (form) => {
    if (!(form instanceof HTMLFormElement)) return;
    delete form.dataset.eamSubmitting;
    form.removeAttribute("aria-busy");
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.dataset.eamSubmitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.eamSubmitting = "true";
    form.setAttribute("aria-busy", "true");
  });

  for (const eventName of ["htmx:afterRequest", "htmx:responseError", "htmx:sendError"]) {
    document.addEventListener(eventName, (event) => {
      resetSubmittingState(event.target?.closest?.("form"));
    });
  }

  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-eam-submitting]").forEach(resetSubmittingState);
  });
})();
