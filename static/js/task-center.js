(() => {
  "use strict";
  const workspace = document.querySelector("[data-task-workspace]");
  if (!workspace) return;
  const cards = Array.from(workspace.querySelectorAll("[data-task-card]"));
  const search = workspace.querySelector("[data-task-search]");
  const activeOnly = workspace.querySelector("[data-task-active-only]");
  const count = workspace.querySelector("[data-task-visible-count]");
  const empty = workspace.querySelector("[data-task-empty]");
  const key = `eam-task-view:v1:${workspace.dataset.taskScope}`;
  try { activeOnly.checked = localStorage.getItem(key) === "active"; } catch (_) { /* Optional browser preference. */ }
  const apply = () => {
    const q = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    cards.forEach(card => {
      const matches = (!activeOnly.checked || Number(card.dataset.taskCount) > 0)
        && (card.dataset.taskSearchText || "").toLocaleLowerCase().includes(q);
      card.hidden = !matches;
      if (matches) visible += 1;
    });
    count.textContent = `显示 ${visible} / ${cards.length} 类`;
    empty.hidden = visible > 0;
    empty.querySelector("[data-task-empty-text]").textContent = q
      ? "没有匹配的待办分类，请调整关键词或查看全部。"
      : "当前可见的分类没有待办，可以查看全部入口或使用下方常用操作。";
  };
  search.addEventListener("input", apply);
  activeOnly.addEventListener("change", () => {
    apply();
    try { localStorage.setItem(key, activeOnly.checked ? "active" : "all"); } catch (_) { /* Keep controls usable without storage. */ }
  });
  workspace.querySelector("[data-task-reset]").addEventListener("click", () => {
    search.value = "";
    activeOnly.checked = false;
    try { localStorage.setItem(key, "all"); } catch (_) { /* Optional browser preference. */ }
    apply();
    search.focus();
  });
  workspace.querySelector("[data-task-controls]").hidden = false;
  apply();
})();
