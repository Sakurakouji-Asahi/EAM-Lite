(() => {
  "use strict";
  const owner = document.body.dataset.preferenceScope;
  if (!owner) return;
  document.querySelectorAll("form[data-query-presets]").forEach(form => {
    const controls = form.querySelector("[data-query-preset-controls]");
    const list = form.querySelector("[data-query-preset-list]");
    const nameInput = form.querySelector("[data-query-preset-name]");
    const save = form.querySelector("[data-query-preset-save]");
    const status = form.querySelector("[data-query-preset-status]");
    if (!controls || !list || !nameInput || !save || !status) return;
    const fields = new Set(Array.from(form.elements).filter(field => field.name && !field.disabled).map(field => field.name));
    const sanitize = query => {
      const cleaned = new URLSearchParams();
      new URLSearchParams(query).forEach((value, name) => {
        if (fields.has(name) && name !== "page" && value !== "") cleaned.append(name, value);
      });
      return cleaned.toString();
    };
    const applied = sanitize(new URLSearchParams(new FormData(form)).toString());
    const key = `eam-query-presets:v1:${owner}:${form.dataset.presetCompany}:${window.location.pathname}`;
    let presets = [];
    try {
      const stored = JSON.parse(localStorage.getItem(key) || "[]");
      if (Array.isArray(stored)) presets = stored.filter(item => item && typeof item.name === "string" && item.name.trim()
        && item.name.length <= 40 && typeof item.query === "string" && item.query.length <= 10000).slice(0, 8);
    } catch (_) { /* Use page-local preferences when browser storage is unavailable. */ }
    const persist = () => {
      try { localStorage.setItem(key, JSON.stringify(presets)); return true; }
      catch (_) { return false; }
    };
    const render = () => {
      const fragment = document.createDocumentFragment();
      presets.forEach((preset, index) => {
        const item = document.createElement("li");
        const link = document.createElement("a");
        const query = sanitize(preset.query);
        link.href = window.location.pathname + (query ? `?${query}` : "");
        link.className = "btn btn-sm btn-outline-secondary rounded-end-0";
        link.textContent = preset.name;
        link.title = `应用查询：${preset.name}`;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-sm btn-outline-secondary rounded-start-0";
        remove.textContent = "×";
        remove.setAttribute("aria-label", `移除常用查询 ${preset.name}`);
        remove.addEventListener("click", () => {
          presets.splice(index, 1);
          const saved = persist();
          render();
          status.textContent = saved ? `已移除“${preset.name}”。` : "已在当前页面移除；浏览器未能保存更改。";
          nameInput.focus();
        });
        item.append(link, remove);
        fragment.append(item);
      });
      if (!presets.length) {
        const empty = document.createElement("li");
        empty.className = "small text-secondary";
        empty.textContent = "还没有常用查询。";
        fragment.append(empty);
      }
      list.replaceChildren(fragment);
    };
    save.addEventListener("click", () => {
      if (form.dataset.presetErrors === "true") return;
      const name = nameInput.value.trim();
      if (!name || name.length > 40) { status.textContent = "请填写 1–40 字的查询名称。"; nameInput.focus(); return; }
      const existing = presets.findIndex(item => item.name === name);
      if (existing < 0 && presets.length >= 8) { status.textContent = "最多记住 8 个查询，请先移除不再使用的查询。"; return; }
      if (existing >= 0) presets.splice(existing, 1);
      presets.unshift({ name, query: applied });
      const saved = persist();
      render();
      nameInput.value = "";
      status.textContent = saved ? `已记住“${name}”的当前查询条件。` : "已加入当前页面；浏览器未允许保存，重新打开后需要重新添加。";
    });
    nameInput.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); save.click(); } });
    if (form.dataset.presetErrors === "true") status.textContent = "请先修正查询条件，再记住当前查询。";
    render();
    controls.hidden = false;
  });
})();
