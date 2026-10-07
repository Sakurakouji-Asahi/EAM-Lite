(() => {
  "use strict";
  const form = document.querySelector("form[data-finance-form-navigation]");
  if (!form) return;
  const reveal = target => {
    let ancestor = target.parentElement;
    while (ancestor && ancestor !== form) {
      if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
      ancestor = ancestor.parentElement;
    }
    target.focus({preventScroll: true});
    requestAnimationFrame(() => {
      const box = target.getBoundingClientRect();
      const header = document.querySelector(".app-topbar")?.getBoundingClientRect().height || 0;
      const offset = Math.max(header + 16, (innerHeight - box.height) / 2);
      scrollTo({top: scrollY + box.top - offset, behavior: "instant"});
    });
  };
  const errors = form.querySelector("[data-finance-form-errors]");
  if (errors) {
    errors.addEventListener("click", event => {
      const link = event.target.closest("[data-finance-error-field]");
      const target = link && document.getElementById(link.dataset.financeErrorField);
      if (!target || !form.contains(target)) return;
      event.preventDefault();
      reveal(target);
    });
    reveal(errors);
  }
  const navigator = form.querySelector("[data-finance-field-navigator]");
  const fields = Array.from(form.querySelectorAll("[data-finance-config-field]")).map(group => ({
    target: document.getElementById(group.querySelector("label[for]")?.htmlFor),
    label: group.dataset.financeFieldLabel,
    text: `${group.dataset.financeFieldLabel} ${group.querySelector(".form-text")?.textContent || ""}`.toLocaleLowerCase(),
  })).filter(item => item.target && !item.target.matches(":disabled") && item.target.getClientRects().length);
  if (!navigator || fields.length < 8) return;
  const query = navigator.querySelector("[data-finance-field-query]");
  const count = navigator.querySelector("[data-finance-field-count]");
  const results = navigator.querySelector("[data-finance-field-results]");
  const render = () => {
    const terms = query.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const matches = fields.filter(item => terms.every(term => item.text.includes(term)));
    count.textContent = matches.length ? `找到 ${matches.length} 项，选择后直接定位。` : "没有匹配的填写项，请减少搜索文字。";
    const fragment = document.createDocumentFragment();
    for (const item of matches) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-sm btn-outline-secondary";
      button.textContent = item.label;
      button.addEventListener("click", () => { navigator.open = false; reveal(item.target); });
      fragment.append(button);
    }
    results.replaceChildren(fragment);
    results.scrollTop = 0;
  };
  query.addEventListener("input", render);
  navigator.addEventListener("toggle", () => { if (navigator.open) query.focus({preventScroll: true}); });
  navigator.addEventListener("keydown", event => {
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    const buttons = Array.from(results.querySelectorAll("button"));
    const index = buttons.indexOf(document.activeElement);
    if (event.target === query && event.key === "Enter") {
      event.preventDefault();
      if (buttons.length === 1) buttons[0].click();
      else buttons[0]?.focus();
    } else if (["ArrowDown", "ArrowUp"].includes(event.key) && (event.target === query || index >= 0)) {
      event.preventDefault();
      if (!buttons.length) return;
      if (event.key === "ArrowUp" && index === 0) query.focus();
      else buttons[index < 0 ? (event.key === "ArrowDown" ? 0 : buttons.length - 1)
        : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
    } else if (event.key === "Escape") {
      event.preventDefault();
      navigator.open = false;
      navigator.querySelector("summary").focus();
    }
  });
  form.addEventListener("reset", () => requestAnimationFrame(render));
  navigator.querySelector("[data-finance-field-title]").textContent = `查找填写项（${fields.length} 项）`;
  render();
  navigator.hidden = false;
})();
