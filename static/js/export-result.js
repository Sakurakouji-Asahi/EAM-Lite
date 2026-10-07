(() => {
  "use strict";
  const section = document.querySelector("[data-export-receipt-section]");
  if (!section) return;
  const button = section.querySelector("[data-export-copy-receipt]");
  const text = section.querySelector("#export-receipt-text");
  const details = section.querySelector("[data-export-receipt-details]");
  const status = section.querySelector("[data-export-copy-status]");
  if (!button || !text || !details || !status) return;
  const canCopy = window.isSecureContext && typeof navigator.clipboard?.writeText === "function";
  button.textContent = canCopy ? "复制核对说明" : "选中核对说明";
  button.hidden = false;
  button.addEventListener("click", async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    status.hidden = false;
    try {
      if (canCopy) {
        try {
          await navigator.clipboard.writeText(text.value);
          status.textContent = "核对说明已复制。";
          return;
        } catch (_) { /* Keep the authorized text available for manual copying. */ }
      }
      details.open = true;
      text.focus();
      text.select();
      text.setSelectionRange(0, text.value.length);
      status.textContent = "核对说明已选中，请按 Ctrl+C（Mac 为 ⌘C）或长按复制。";
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  });
})();
