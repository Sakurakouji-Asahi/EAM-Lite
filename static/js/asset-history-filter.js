(() => {
  "use strict";
  const normalize = (value) => String(value).normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, " ").trim();
  document.querySelectorAll("[data-asset-history]").forEach((history) => {
    const controls = history.querySelector("[data-asset-history-controls]");
    const event = history.querySelector("[data-history-event]");
    const keyword = history.querySelector("[data-history-keyword]");
    const count = history.querySelector("[data-history-count]");
    const empty = history.querySelector("[data-history-no-match]");
    const resets = history.querySelectorAll("[data-history-reset]");
    const rows = Array.from(history.querySelectorAll("[data-history-row]"));
    if (!controls || !event || !keyword || !count || !empty || !rows.length) return;
    const texts = new Map(rows.map((row) => [row, normalize(row.textContent)]));
    const types = new Map();
    rows.forEach((row) => {
      const type = row.dataset.historyType;
      const value = types.get(type) || {label: row.dataset.historyLabel, count: 0};
      value.count += 1;
      types.set(type, value);
    });
    types.forEach((value, type) => {
      const option = document.createElement("option");
      option.value = type;
      option.textContent = `${value.label}（${value.count}）`;
      event.append(option);
    });
    const apply = () => {
      const tokens = normalize(keyword.value).split(" ").filter(Boolean);
      let matched = 0;
      rows.forEach((row) => {
        const visible = (!event.value || row.dataset.historyType === event.value)
          && tokens.every((token) => texts.get(row).includes(token));
        row.hidden = !visible;
        if (visible) matched += 1;
      });
      empty.hidden = matched !== 0;
      count.textContent = `当前页匹配 ${matched} / ${rows.length} 条；历史共 ${history.dataset.historyTotal} 条。`;
      resets.forEach((button) => { button.disabled = !event.value && !keyword.value; });
    };
    event.addEventListener("change", apply);
    keyword.addEventListener("input", (inputEvent) => { if (!inputEvent.isComposing) apply(); });
    keyword.addEventListener("compositionend", apply);
    keyword.addEventListener("search", apply);
    resets.forEach((button) => button.addEventListener("click", () => {
      event.value = "";
      keyword.value = "";
      apply();
      keyword.focus({preventScroll: true});
    }));
    apply();
    controls.hidden = false;
  });
})();
