(() => {
  "use strict";
  document.querySelectorAll("[data-scroll-table]").forEach((container, index) => {
    const table = container.querySelector("table");
    if (!table) return;
    const hint = document.createElement("p");
    hint.id = `table-scroll-hint-${index + 1}`;
    hint.className = "table-scroll-hint small text-secondary mb-2";
    hint.hidden = true;
    hint.textContent = "左右滚动可查看更多列。";
    const keyboardHelp = document.createElement("span");
    keyboardHelp.className = "visually-hidden";
    keyboardHelp.textContent = "使用键盘时，可聚焦表格后按左右方向键。";
    hint.append(keyboardHelp);
    container.before(hint);
    const initialTabindex = container.getAttribute("tabindex");
    const initialDescription = container.getAttribute("aria-describedby");
    const update = () => {
      const overflow = container.clientWidth > 0 && container.scrollWidth > container.clientWidth + 1;
      hint.hidden = !overflow;
      if (overflow) {
        if (initialTabindex === null) container.setAttribute("tabindex", "0");
        container.setAttribute("aria-describedby", [initialDescription, hint.id].filter(Boolean).join(" "));
      } else {
        if (initialTabindex === null) container.removeAttribute("tabindex");
        if (initialDescription === null) container.removeAttribute("aria-describedby");
        else container.setAttribute("aria-describedby", initialDescription);
      }
    };
    if (window.ResizeObserver) {
      const observer = new ResizeObserver(update);
      observer.observe(container);
      observer.observe(table);
    } else {
      window.addEventListener("resize", update);
    }
    update();
  });
})();
