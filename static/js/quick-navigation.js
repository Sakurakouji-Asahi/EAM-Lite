(() => {
  "use strict";
  const dialog = document.querySelector("[data-quick-navigation]");
  if (!dialog || typeof dialog.showModal !== "function") return;
  const navigation = document.querySelector("nav.app-nav");
  const input = dialog.querySelector("[data-quick-navigation-query]");
  const results = dialog.querySelector("[data-quick-navigation-results]");
  const empty = dialog.querySelector("[data-quick-navigation-empty]");
  const count = dialog.querySelector("[data-quick-navigation-count]");
  const heading = dialog.querySelector("[data-quick-navigation-heading]");
  const pinnedOnlyButton = dialog.querySelector('[data-quick-navigation-pinned-only]');
  const pinnedCount = dialog.querySelector('[data-quick-navigation-pinned-count]');
  const pinFeedback = dialog.querySelector('[data-quick-navigation-pin-feedback]');
  const emptyMessage = dialog.querySelector('[data-quick-navigation-empty-message]');
  if (!navigation || !input || !results || !pinnedOnlyButton || !pinnedCount || !pinFeedback || !emptyMessage) return;

  const aliases = {
    asset_ledger: "设备 台账 清单 财产",
    supply_stock: "有货 零库存 库存余额",
    supply_documents: "出库 收货 退库 单据",
    asset_inventory: "清点 应盘 盘点表",
    supply_inventory: "清点 应盘 盘点表",
    maintenance_tasks: "保养 到期 逾期",
    report_center: "统计 导出 Excel",
    imports: "批量 上传 Excel 模板",
    backup: "数据保护 下载备份",
  };
  const entries = [];
  const seen = new Set();
  navigation.querySelectorAll("a.app-nav-link[href]").forEach(link => {
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin) return;
    const href = url.pathname + url.search;
    if (seen.has(href)) return;
    seen.add(href);
    const label = link.textContent.trim();
    const section = link.closest("details")?.querySelector("summary")?.textContent.trim() || "首页";
    entries.push({ href, label, section, terms: `${label} ${section} ${aliases[link.dataset.navItem] || ""}`.toLocaleLowerCase() });
  });
  const key = `eam-quick-navigation:v1:${dialog.dataset.preferenceScope}`;
  const pinnedKey = `eam-quick-navigation-pinned:v1:${dialog.dataset.preferenceScope}:${dialog.dataset.companyScope}`;
  const pinnedLimit = 8;
  let pinned = [], pinnedOnly = false, pinnedStorageAvailable = true;
  const readPinned = () => {
    if (!pinnedStorageAvailable) return;
    try {
      const stored = JSON.parse(localStorage.getItem(pinnedKey) || '[]');
      if (Array.isArray(stored)) pinned = [...new Set(stored.filter(href => seen.has(href)))].slice(0, pinnedLimit);
    } catch (_) { pinnedStorageAvailable = false; }
  };
  readPinned();
  let recent = [];
  try {
    const stored = JSON.parse(localStorage.getItem(key) || "[]");
    if (Array.isArray(stored)) recent = stored.filter(href => seen.has(href)).slice(0, 6);
  } catch (_) { /* Navigation stays available when browser storage is disabled. */ }
  const remember = href => {
    recent = [href, ...recent.filter(value => value !== href)].slice(0, 6);
    try { localStorage.setItem(key, JSON.stringify(recent)); } catch (_) { /* Optional preference. */ }
  };
  const current = window.location.pathname + window.location.search;
  if (seen.has(current)) remember(current);

  const render = (focusPin = null) => {
    const terms = input.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    let matches = entries.filter(entry => (!pinnedOnly || pinned.includes(entry.href)) && terms.every(term => entry.terms.includes(term)));
    if (!terms.length) {
      matches = [...matches].sort((a, b) => {
        const rank = entry => pinned.includes(entry.href) ? pinned.indexOf(entry.href)
          : pinned.length + (recent.includes(entry.href) ? recent.indexOf(entry.href) : recent.length);
        return rank(a) - rank(b);
      });
    }
    const fragment = document.createDocumentFragment();
    matches.forEach(entry => {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = entry.href;
      const label = document.createElement("strong");
      label.textContent = entry.label;
      const section = document.createElement("small");
      section.textContent = pinned.includes(entry.href) ? `已固定 · ${entry.section}`
        : !terms.length && recent.includes(entry.href) ? `最近使用 · ${entry.section}` : entry.section;
      link.append(label, section);
      link.addEventListener("click", () => { remember(entry.href); dialog.close(); });
      const pin = document.createElement('button');
      pin.type = 'button';
      pin.className = 'app-command-pin';
      pin.dataset.navigationPin = entry.href;
      const isPinned = pinned.includes(entry.href);
      pin.textContent = isPinned ? '取消固定' : '固定';
      pin.setAttribute('aria-label', `${isPinned ? '取消固定' : '固定'}${entry.label}`);
      pin.setAttribute('aria-pressed', String(isPinned));
      pin.addEventListener('click', () => {
        if (!isPinned && pinned.length >= pinnedLimit) {
          pinFeedback.textContent = `最多固定 ${pinnedLimit} 个功能，请先取消一个再添加。`;
          pinFeedback.hidden = false;
          return;
        }
        pinned = isPinned ? pinned.filter(href => href !== entry.href) : [...pinned, entry.href];
        let saved = true;
        try { localStorage.setItem(pinnedKey, JSON.stringify(pinned)); } catch (_) { saved = false; }
        pinnedStorageAvailable = saved;
        render(entry.href);
        pinFeedback.textContent = `已${isPinned ? '取消固定' : '固定'}“${entry.label}”。` +
          (saved ? '' : '浏览器无法保存偏好，本次页面仍可使用，刷新后可能丢失。');
        pinFeedback.hidden = false;
      });
      item.append(link, pin);
      fragment.append(item);
    });
    results.replaceChildren(fragment);
    empty.hidden = matches.length > 0;
    emptyMessage.textContent = pinnedOnly
      ? (terms.length ? '固定的功能中没有匹配项，可切回全部功能继续查找。' : '还没有固定功能，切回全部功能后可点击“固定”。')
      : '没有匹配的功能，请换个关键词。';
    count.textContent = `${matches.length} 个功能`;
    heading.textContent = pinnedOnly ? '固定的功能' : terms.length ? "匹配的功能" : "固定、最近使用与功能菜单";
    pinnedOnlyButton.setAttribute('aria-pressed', String(pinnedOnly));
    pinnedOnlyButton.textContent = pinnedOnly ? '查看全部功能' : '只看固定';
    pinnedCount.textContent = `已固定 ${pinned.length}/${pinnedLimit} · 本浏览器`;
    results.scrollTop = 0;
    if (focusPin) {
      const target = Array.from(results.querySelectorAll('[data-navigation-pin]')).find(button => button.dataset.navigationPin === focusPin);
      if (target) { target.focus({preventScroll:true}); target.scrollIntoView({block:'nearest',behavior:'instant'}); }
      else input.focus();
    }
  };
  let opener = null;
  const open = () => {
    if (dialog.open || document.querySelector("dialog[open]")) return;
    opener = document.activeElement;
    input.value = "";
    readPinned();
    pinnedOnly = false;
    pinFeedback.hidden = true;
    render();
    dialog.showModal();
    input.focus();
  };
  document.querySelectorAll("[data-quick-navigation-open]").forEach(button => {
    button.hidden = false;
    button.addEventListener("click", open);
  });
  dialog.querySelector("[data-quick-navigation-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => { if (opener?.isConnected) opener.focus(); });
  dialog.addEventListener("click", event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
  input.addEventListener("input", () => render());
  pinnedOnlyButton.addEventListener('click', () => { pinnedOnly = !pinnedOnly; render(); });
  dialog.addEventListener("keydown", event => {
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey) return;
    const links = Array.from(results.querySelectorAll("a"));
    const activePin = document.activeElement.matches('[data-navigation-pin]');
    const rowLink = document.activeElement.closest('li')?.querySelector('a');
    const index = links.indexOf(rowLink || document.activeElement);
    if (event.key === 'Escape') { event.preventDefault(); dialog.close(); return; }
    if (event.key === 'ArrowRight' && document.activeElement === rowLink) {
      event.preventDefault(); rowLink.nextElementSibling.focus(); return;
    }
    if (event.key === 'ArrowLeft' && activePin) { event.preventDefault(); rowLink.focus(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (document.activeElement !== input && index < 0) return;
      event.preventDefault();
      if (!links.length) return;
      if (event.key === "ArrowUp" && index <= 0) input.focus();
      else {
        const target = links[(index + (event.key === "ArrowDown" ? 1 : -1) + links.length) % links.length];
        (activePin ? target.nextElementSibling : target).focus();
      }
    } else if (event.key === "Enter" && document.activeElement === input && links.length) {
      event.preventDefault();
      links[0].click();
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key.toLocaleLowerCase() !== "k" || !(event.ctrlKey || event.metaKey) || event.altKey || event.isComposing) return;
    if (document.querySelector(".modal.show, .offcanvas.show")) return;
    event.preventDefault();
    if (dialog.open) dialog.close(); else open();
  });
})();
