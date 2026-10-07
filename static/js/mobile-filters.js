(() => {
  "use strict";
  const mobile = window.matchMedia("(max-width: 767.98px)");
  document.querySelectorAll("form[data-mobile-filters]").forEach((form, index) => {
    const hasErrors = form.dataset.presetErrors === "true";
    const snapshot = () => JSON.stringify(Array.from(new FormData(form).entries()));
    const applied = snapshot();
    const submitButton = form.querySelector('button[type="submit"], input[type="submit"]');
    const queryAction = submitButton?.textContent.trim() || submitButton?.value || "查询";
    const summaries = [];
    const ignored = new Set(["view", "page", "page_size", "summary_page"]);
    Array.from(form.elements).forEach(field => {
      if (!field.name || field.disabled || ignored.has(field.name)
          || ["hidden", "submit", "button", "file"].includes(field.type)) return;
      if (["radio", "checkbox"].includes(field.type) && !field.checked) return;
      const label = field.labels?.[0]?.textContent.trim().replace(/\s+/g, " ") || field.name;
      if (field instanceof HTMLSelectElement) {
        const selected = Array.from(field.selectedOptions)
          .filter(option => option.value && option.value !== "all")
          .map(option => option.textContent.trim());
        if (selected.length) summaries.push(`${label}：${selected.join("、")}`);
      } else if (field.value.trim()) {
        summaries.push(["radio", "checkbox"].includes(field.type) ? label : `${label}：${field.value.trim()}`);
      }
    });

    const body = document.createElement("div");
    body.id = `mobile-filter-fields-${index + 1}`;
    body.className = "mobile-filter-fields";
    body.dataset.mobileFilterBody = "";
    while (form.firstChild) body.append(form.firstChild);
    const controls = document.createElement("div");
    controls.className = "mobile-filter-controls";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "btn btn-outline-primary btn-sm";
    toggle.dataset.mobileFilterToggle = "";
    toggle.setAttribute("aria-controls", body.id);
    const summary = document.createElement("p");
    summary.className = "small text-secondary mt-2 mb-0 mobile-filter-summary";
    summary.dataset.mobileFilterSummary = "";
    summary.textContent = hasErrors ? "查询条件有误，请根据页面提示修正。"
      : summaries.length ? `已应用：${summaries.join("；")}` : "正在使用默认查询范围。";
    const pending = document.createElement("p");
    pending.className = "small text-warning-emphasis mt-2 mb-0";
    pending.dataset.mobileFilterPending = "";
    pending.setAttribute("role", "status");
    pending.setAttribute("aria-live", "polite");
    pending.textContent = `条件已修改，点击“${queryAction}”后应用。`;
    pending.hidden = true;
    controls.append(toggle, summary, pending);
    form.append(controls, body);

    let mobileExpanded = hasErrors;
    const render = () => {
      const expanded = !mobile.matches || mobileExpanded;
      body.hidden = !expanded;
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.textContent = expanded ? "收起筛选" : `展开筛选${summaries.length ? `（${summaries.length} 项条件）` : ""}`;
      form.classList.toggle("mobile-filter-collapsed", !expanded);
    };
    toggle.addEventListener("click", () => {
      mobileExpanded = !mobileExpanded;
      render();
    });
    const updatePending = () => { pending.hidden = snapshot() === applied; };
    form.addEventListener("input", updatePending);
    form.addEventListener("change", updatePending);
    form.addEventListener("reset", () => setTimeout(updatePending, 0));
    window.addEventListener("pageshow", updatePending);
    mobile.addEventListener("change", () => {
      // Keep an active field visible when a user rotates the screen while editing.
      if (mobile.matches && body.contains(document.activeElement)) mobileExpanded = true;
      render();
    });
    render();
  });
})();
