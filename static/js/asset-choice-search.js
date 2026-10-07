(() => {
  "use strict";
  const form = document.querySelector("form[data-asset-form-assistance], form[data-choice-search]");
  if (!form || typeof HTMLDialogElement === "undefined") return;
  const candidates = form.hasAttribute("data-choice-search")
    ? Array.from(form.querySelectorAll("select[data-choice-search]"))
    : ["category", "department", "responsible_employee", "location"]
      .map(name => form.elements.namedItem(name));
  const targets = candidates.filter(field => field instanceof HTMLSelectElement && !field.multiple);
  if (!targets.length) return;

  const dialog = document.createElement("dialog");
  if (typeof dialog.showModal !== "function") return;
  dialog.className = "asset-choice-search";
  dialog.setAttribute("aria-labelledby", "asset-choice-search-title");
  dialog.innerHTML = '<div class="asset-choice-search-header"><h2 class="h5 mb-0" id="asset-choice-search-title"></h2><button type="button" class="btn-close" aria-label="关闭查找"></button></div><label class="form-label mt-3" for="asset-choice-search-query">编号或名称</label><input class="form-control" id="asset-choice-search-query" type="search" autocomplete="off" placeholder="可输入多个关键词"><p class="small text-secondary mt-2 mb-2" data-choice-count role="status" aria-live="polite"></p><div class="asset-choice-search-results" data-choice-results></div><p class="small text-secondary mt-2 mb-0">上下方向键选择，回车确认；关闭查找可保留原选择。</p>';
  document.body.append(dialog);
  const title = dialog.querySelector("h2");
  const input = dialog.querySelector("input");
  const results = dialog.querySelector("[data-choice-results]");
  const count = dialog.querySelector("[data-choice-count]");
  let current = null;
  let opener = null;

  const available = select => Array.from(select.options).filter(option =>
    option.value && !option.disabled && !option.hidden && !option.closest("optgroup[disabled]"));
  const labelFor = select => Array.from(select.labels || [])
    .map(label => label.childNodes[0]?.textContent.trim()).find(Boolean) || select.name;
  const render = () => {
    if (!current) return;
    const terms = input.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const matches = available(current).filter(option =>
      terms.every(term => option.textContent.toLocaleLowerCase().includes(term)));
    if (!terms.length) matches.sort((left, right) => Number(right.selected) - Number(left.selected));
    count.textContent = matches.length > 100 ? `找到 ${matches.length} 项，先显示前 100 项，请继续输入缩小范围。`
      : matches.length ? `找到 ${matches.length} 项。` : "没有匹配项，请换一个编号或名称。";
    const fragment = document.createDocumentFragment();
    matches.slice(0, 100).forEach(option => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "asset-choice-search-option";
      button.textContent = option.textContent;
      if (option.selected) {
        button.classList.add("is-selected");
        button.setAttribute("aria-current", "true");
        const selected = document.createElement("small");
        selected.textContent = "当前选择";
        button.append(selected);
      }
      button.addEventListener("click", () => {
        // Recheck the live option because department changes can disable people.
        if (!current?.isConnected || current.disabled || !available(current).includes(option)) {
          render();
          return;
        }
        const changed = current.value !== option.value;
        current.value = option.value;
        if (changed) {
          current.dispatchEvent(new Event("input", {bubbles: true}));
          current.dispatchEvent(new Event("change", {bubbles: true}));
        }
        dialog.close();
      });
      fragment.append(button);
    });
    results.replaceChildren(fragment);
    results.scrollTop = 0;
  };

  targets.forEach(select => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-sm btn-outline-secondary mt-2";
    button.textContent = `查找${labelFor(select)}`;
    button.setAttribute("aria-haspopup", "dialog");
    button.dataset.assetChoiceFor = select.name;
    select.after(button);
    const update = () => {
      button.hidden = select.disabled || available(select).length < 8;
      if (dialog.open && current === select) {
        if (select.disabled) dialog.close();
        else render();
      }
    };
    button.addEventListener("click", () => {
      if (select.disabled || document.querySelector("dialog[open]")) return;
      current = select;
      opener = button;
      title.textContent = `查找${labelFor(select)}`;
      input.value = "";
      render();
      dialog.showModal();
      input.focus();
    });
    new MutationObserver(update).observe(select, {
      childList: true, subtree: true, attributes: true,
      attributeFilter: ["disabled", "hidden", "label", "value"], characterData: true,
    });
    select.addEventListener("change", update);
    update();
  });
  input.addEventListener("input", render);
  dialog.querySelector(".btn-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    const target = opener?.isConnected && !opener.hidden ? opener : current;
    target?.focus();
    current = null;
  });
  dialog.addEventListener("keydown", event => {
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "Escape") {
      event.preventDefault();
      dialog.close();
      return;
    }
    const buttons = Array.from(results.querySelectorAll("button"));
    const index = buttons.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp"].includes(event.key) && (document.activeElement === input || index >= 0)) {
      event.preventDefault();
      if (!buttons.length) return;
      if (event.key === "ArrowUp" && index === 0) input.focus();
      else buttons[index < 0 ? (event.key === "ArrowDown" ? 0 : buttons.length - 1)
        : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length].focus();
    } else if (event.key === "Enter" && document.activeElement === input) {
      event.preventDefault();
      if (buttons.length === 1) buttons[0].click();
      else buttons[0]?.focus();
    }
  });
})();
