(() => {
  "use strict";
  document.querySelectorAll("form[data-asset-custom-fields]").forEach(form => {
    const category = form.elements.namedItem("category");
    const container = form.querySelector("[data-custom-fields-container]");
    if (!category || !container) return;
    const fragments = new Map();
    const values = new Map();
    let active;
    let revision = 0;
    let disabledButtons = [];
    const remember = () => {
      if (!container.dataset.category) return;
      fragments.set(container.dataset.category, container.innerHTML);
      container.querySelectorAll("input[name],select[name],textarea[name]").forEach(field => {
        values.set(field.name, field.value);
      });
    };
    const restore = () => {
      container.querySelectorAll("input[name],select[name],textarea[name]").forEach(field => {
        if (values.has(field.name)) field.value = values.get(field.name);
      });
    };
    const setBusy = busy => {
      if (busy) {
        disabledButtons = Array.from(form.querySelectorAll('button[type="submit"]')).filter(button => !button.disabled);
        disabledButtons.forEach(button => { button.disabled = true; });
        container.setAttribute("aria-busy", "true");
      } else {
        disabledButtons.forEach(button => { button.disabled = false; });
        disabledButtons = [];
        container.removeAttribute("aria-busy");
      }
    };
    const showMessage = text => {
      const message = document.createElement("p");
      message.className = "text-secondary mb-0";
      message.textContent = text;
      container.replaceChildren(message);
    };
    const update = async () => {
      remember();
      const current = ++revision;
      if (active) active.abort();
      setBusy(false);
      const selected = category.value;
      container.dataset.category = "";
      if (!selected) {
        showMessage("选择实物分类后显示该分类的扩展字段。");
        return;
      }
      if (fragments.has(selected)) {
        container.innerHTML = fragments.get(selected);
        container.dataset.category = selected;
        restore();
        return;
      }
      showMessage("正在加载分类扩展资料…");
      setBusy(true);
      active = new AbortController();
      try {
        const url = new URL(form.dataset.assetCustomFields, window.location.href);
        url.searchParams.set("category", selected);
        const response = await fetch(url, {signal: active.signal, credentials: "same-origin"});
        if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) throw new Error("加载失败");
        const result = await response.json();
        if (current !== revision) return;
        if (result.category !== selected || typeof result.html !== "string") throw new Error("分类不匹配");
        container.innerHTML = result.html;
        container.dataset.category = selected;
        restore();
        remember();
      } catch (error) {
        if (error.name === "AbortError" || current !== revision) return;
        showMessage("扩展资料暂未加载。请重试；直接保存时系统也会校验并显示需要补填的字段。");
        const retry = document.createElement("button");
        retry.type = "button";
        retry.className = "btn btn-outline-secondary mt-2";
        retry.textContent = "重新加载扩展资料";
        retry.addEventListener("click", update);
        container.appendChild(retry);
      } finally {
        if (current === revision) setBusy(false);
      }
    };
    remember();
    category.addEventListener("change", update);
    form.addEventListener("submit", event => {
      if (container.getAttribute("aria-busy") === "true") event.preventDefault();
    });
    if (category.value !== container.dataset.category) update();
  });
})();
