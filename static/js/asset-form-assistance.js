(() => {
  "use strict";
  const form = document.querySelector("form[data-asset-form-assistance]");
  if (!form) return;
  const overview = form.querySelector("[data-asset-input-overview]");
  const count = overview?.querySelector("[data-asset-required-count]");
  const next = overview?.querySelector("[data-asset-next-empty]");
  const message = overview?.querySelector("[data-asset-required-message]");
  if (!overview || !count || !next || !message) return;

  const requiredFields = () => Array.from(form.querySelectorAll("input[required],select[required],textarea[required]"))
    .filter(field => field.name && !field.disabled && !field.readOnly && field.type !== "hidden");
  const isBlank = field => field.type === "checkbox" || field.type === "radio"
    ? !field.checked : !field.value.trim();
  const reveal = target => {
    let parent = target.parentElement;
    while (parent && parent !== form) {
      if (parent instanceof HTMLDetailsElement) parent.open = true;
      parent = parent.parentElement;
    }
    if (!target.matches("input,select,textarea,button,summary,a[href]")) target.tabIndex = -1;
    target.focus({preventScroll: true});
    target.scrollIntoView({block: "center", behavior: "auto"});
  };
  const update = () => {
    const fields = requiredFields();
    const empty = fields.filter(isBlank);
    const loading = form.querySelector('[data-custom-fields-container][aria-busy="true"]');
    count.textContent = `必填资料已填 ${fields.length - empty.length} / ${fields.length}`;
    next.disabled = !empty.length;
    next.textContent = empty.length ? `定位未填项（${empty.length}）` : "必填输入已齐";
    message.textContent = loading ? "分类扩展资料正在加载，请稍候。"
      : empty.length ? "先填写带星号的资料；保存时系统会继续核对资料。"
      : "带星号的资料均已填写，可继续补充选填资料；保存时系统会核对内容。";
  };
  next.addEventListener("click", () => {
    const empty = requiredFields().filter(isBlank);
    if (!empty.length) return;
    const current = empty.indexOf(document.activeElement);
    reveal(empty[(current + 1) % empty.length]);
  });
  const followAnchor = event => {
    const link = event.target.closest?.('a[href^="#"]');
    if (!link) return;
    const target = document.getElementById(link.getAttribute("href").slice(1));
    if (!target || !form.contains(target)) return;
    event.preventDefault();
    reveal(target);
  };
  overview.addEventListener("click", followAnchor);
  document.getElementById("asset-form-errors")?.addEventListener("click", followAnchor);
  form.addEventListener("input", update);
  form.addEventListener("change", update);
  form.addEventListener("reset", () => setTimeout(update, 0));
  const custom = form.querySelector("[data-custom-fields-container]");
  if (custom) new MutationObserver(update).observe(custom, {
    childList: true, subtree: true, attributes: true,
    attributeFilter: ["aria-busy", "required", "disabled"],
  });
  overview.hidden = false;
  update();
})();
