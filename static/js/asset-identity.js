(() => {
  "use strict";
  document.querySelectorAll("form[data-asset-identity-preview]").forEach((form) => {
    const fields = ["category", "department", "component_of", "management_attribute", "coding_year", "acquisition_date"];
    const output = form.querySelector("[data-identity-preview-code]");
    const message = form.querySelector("[data-identity-preview-message]");
    let timer;
    let active;
    let revision = 0;
    const update = async () => {
      const current = ++revision;
      if (active) active.abort();
      const params = new URLSearchParams();
      fields.forEach((name) => {
        const input = form.elements.namedItem(name);
        if (input && input.value) params.set(name, input.value);
      });
      if (!params.has("category") || (!params.has("management_attribute") && !params.has("component_of"))) {
        output.textContent = "选择管理属性和实物分类后显示。";
        message.textContent = "预览不占号；管理属性与财务认定独立。";
        return;
      }
      active = new AbortController();
      try {
        const response = await fetch(`${form.dataset.assetIdentityPreview}?${params}`, {signal: active.signal, credentials: "same-origin"});
        if (!response.headers.get("content-type")?.includes("application/json")) throw new Error("请检查登录状态后重试。");
        const value = await response.json();
        if (current !== revision) return;
        output.textContent = response.ok ? value.code : "暂时无法预览";
        message.textContent = value.message;
      } catch (error) {
        if (error.name !== "AbortError" && current === revision) {
          output.textContent = "暂时无法预览";
          message.textContent = "请检查网络或登录状态；保存时系统仍会校验并分配编号。";
        }
      }
    };
    fields.forEach((name) => {
      const input = form.elements.namedItem(name);
      if (input) input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(update, 250); });
    });
    update();
  });
})();
