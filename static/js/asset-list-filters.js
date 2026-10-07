(() => {
  "use strict";
  const form = document.querySelector("[data-asset-list-filters]");
  const summary = document.querySelector("[data-asset-active-filters]");
  if (!form || !summary) return;

  // Use the filters behind the displayed results, not unsaved form edits.
  const applied = new URLSearchParams(form.dataset.appliedQuery);
  const chips = summary.querySelector("[data-asset-filter-chips]");
  let count = 0;
  applied.forEach((value, name) => {
    if (!value || name === "view" || (name === "record_status" && value === "active")) return;
    const field = form.elements.namedItem(name);
    const label = field?.labels?.[0]?.textContent.trim();
    if (!label || field.type === "hidden") return;

    const choice = field.tagName === "SELECT"
      ? Array.from(field.options).find((option) => option.value === value)
      : null;
    const display = name === "codes" ? `${value.split(/\r?\n/).filter(Boolean).length} 个编号`
      : field.tagName === "SELECT" ? (choice?.textContent.trim() || "无效值") : value;
    const remaining = new URLSearchParams(applied);
    remaining.delete(name);
    remaining.delete("page");
    remaining.set("page_size", form.dataset.pageSize);
    const link = document.createElement("a");
    link.className = "btn btn-sm btn-outline-secondary text-start text-break mw-100";
    link.href = `?${remaining}`;
    link.setAttribute("aria-label", `移除筛选：${label}：${display}`);
    link.textContent = `${label}：${display} `;
    const close = document.createElement("span");
    close.setAttribute("aria-hidden", "true");
    close.textContent = "×";
    link.append(close);
    chips.append(link);
    count += 1;
  });
  summary.hidden = count === 0;
})();
