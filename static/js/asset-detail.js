(() => {
  "use strict";

  const copyLegacy = (text) => {
    const previousFocus = document.activeElement;
    const selection = window.getSelection();
    const ranges = selection ? Array.from({length: selection.rangeCount}, (_, index) => selection.getRangeAt(index).cloneRange()) : [];
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.className = "visually-hidden";
    textarea.setAttribute("aria-hidden", "true");
    textarea.tabIndex = -1;
    document.body.append(textarea);
    try {
      textarea.focus({preventScroll: true});
      textarea.select();
      textarea.setSelectionRange(0, text.length);
      return document.execCommand("copy");
    } catch (_) {
      return false;
    } finally {
      textarea.remove();
      previousFocus?.focus({preventScroll: true});
      if (selection) {
        selection.removeAllRanges();
        ranges.forEach((range) => selection.addRange(range));
      }
    }
  };

  const selectForManualCopy = (target) => {
    target.focus({preventScroll: true});
    const selection = window.getSelection();
    if (!selection) return false;
    const range = document.createRange();
    range.selectNodeContents(target);
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  };

  document.querySelectorAll("button[data-copy-target]").forEach((button) => {
    const target = document.getElementById(button.dataset.copyTarget);
    const status = button.closest("[data-copy-group]")?.querySelector("[data-copy-status]");
    if (!target || !status) return;
    let copying = false;
    button.hidden = false;
    button.addEventListener("click", async () => {
      if (copying) return;
      copying = true;
      button.setAttribute("aria-busy", "true");
      status.textContent = "正在复制…";
      const text = target.textContent;
      let copied = false;
      try {
        if (window.isSecureContext && navigator.clipboard?.writeText) {
          try {
            await navigator.clipboard.writeText(text);
            copied = true;
          } catch (_) {
            // A denied clipboard request can still allow user-triggered legacy copying.
          }
        }
        if (!copied) copied = copyLegacy(text);
        if (copied) {
          status.textContent = `${button.dataset.copyLabel}已复制。`;
        } else {
          const selected = selectForManualCopy(target);
          status.textContent = selected
            ? "未能自动复制，编号已选中；请按 Ctrl+C（Mac 为 ⌘C）或长按复制。"
            : "未能自动复制，请选中编号后手动复制。";
        }
      } finally {
        copying = false;
        button.removeAttribute("aria-busy");
      }
    });
  });
})();
