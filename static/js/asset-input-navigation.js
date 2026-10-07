(() => {
  "use strict";
  const textTypes = new Set(["text", "search", "email", "url", "tel", "number"]);
  document.querySelectorAll("form[data-asset-form-assistance]").forEach((form) => {
    const isTextInput = (field) => field instanceof HTMLInputElement && textTypes.has(field.type);
    const configure = () => {
      Array.from(form.elements).forEach((field) => {
        if (isTextInput(field) && field.name && !field.readOnly && !field.hasAttribute("enterkeyhint")) {
          field.setAttribute("enterkeyhint", "next");
        }
      });
    };
    const canFocus = (field) => {
      const input = field instanceof HTMLInputElement;
      return (input || field instanceof HTMLSelectElement || field instanceof HTMLTextAreaElement)
        && field.name && !field.matches(":disabled") && !field.readOnly
        && !["hidden", "submit", "button", "reset", "file"].includes(field.type)
        && field.getClientRects().length > 0;
    };
    form.addEventListener("keydown", (event) => {
      const field = event.target;
      if (event.key !== "Enter" || !isTextInput(field) || field.form !== form || !field.name
          || event.isComposing || event.keyCode === 229 || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
      // Keep the next/done key in the input flow; submit buttons retain native behavior.
      event.preventDefault();
      if (event.repeat) return;
      const fields = Array.from(form.elements);
      const target = fields.slice(fields.indexOf(field) + 1).find(canFocus)
        || form.querySelector('button[type="submit"]:not(:disabled)');
      if (!target) return;
      target.focus({preventScroll: true});
      requestAnimationFrame(() => target.scrollIntoView({block: "center", behavior: "instant"}));
    });
    configure();
    const custom = form.querySelector("[data-custom-fields-container]");
    if (custom) new MutationObserver(configure).observe(custom, {childList: true, subtree: true});
    const help = form.querySelector("[data-asset-input-navigation-help]");
    if (help) help.hidden = false;
  });
})();
