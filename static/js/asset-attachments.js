(() => {
  "use strict";
  const workspace = document.querySelector("[data-asset-attachments]");
  if (!workspace) return;
  const search = workspace.querySelector("[data-attachment-search]");
  if (!search) return;
  const role = workspace.querySelector("[data-attachment-role]");
  const rows = Array.from(workspace.querySelectorAll("[data-attachment-row]"));
  const count = workspace.querySelector("[data-attachment-count]");
  const empty = workspace.querySelector("[data-attachment-empty]");
  const dialog = workspace.querySelector("[data-attachment-dialog]");
  const buttons = Array.from(workspace.querySelectorAll("[data-attachment-preview]"));
  const image = dialog.querySelector("[data-attachment-image]");
  const status = dialog.querySelector("[data-attachment-preview-status]");
  const previous = dialog.querySelector("[data-attachment-prev]");
  const next = dialog.querySelector("[data-attachment-next]");
  let visibleImages = [], activeIndex = 0, controller, reader, generation = 0;
  const cancel = () => {
    generation += 1;
    controller?.abort();
    if (reader?.readyState === FileReader.LOADING) reader.abort();
    image.hidden = true;
    image.removeAttribute("src");
  };
  const filter = () => {
    const q = search.value.trim().toLocaleLowerCase();
    let shown = 0;
    rows.forEach(row => {
      row.hidden = Boolean(role.value && row.dataset.role !== role.value)
        || !(row.dataset.search || "").toLocaleLowerCase().includes(q);
      if (!row.hidden) shown += 1;
    });
    count.textContent = `显示 ${shown} / ${rows.length} 个附件`;
    empty.hidden = shown > 0;
  };
  const show = async index => {
    cancel();
    const version = generation;
    activeIndex = index;
    const button = visibleImages[index];
    dialog.querySelector("#asset-image-title").textContent = button.dataset.filename;
    dialog.querySelector("[data-attachment-image-count]").textContent = `${index + 1} / ${visibleImages.length}`;
    dialog.querySelector("[data-attachment-original]").href = button.dataset.attachmentPreview;
    const navigationFocus = document.activeElement;
    previous.disabled = index === 0;
    next.disabled = index === visibleImages.length - 1;
    if (dialog.open && navigationFocus === next && next.disabled) previous.focus();
    if (dialog.open && navigationFocus === previous && previous.disabled) next.focus();
    image.alt = button.dataset.filename;
    status.textContent = "正在读取图片…";
    status.hidden = false;
    controller = new AbortController();
    if (!dialog.open) dialog.showModal();
    try {
      const response = await fetch(button.dataset.attachmentPreview, { signal: controller.signal, credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("unavailable");
      const blob = await response.blob();
      if (!["image/jpeg", "image/png", "image/webp"].includes(blob.type)) throw new Error("unsupported");
      if (version !== generation) return;
      const data = await new Promise((resolve, reject) => {
        reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("unreadable"));
        reader.onabort = () => reject(new DOMException("Cancelled", "AbortError"));
        reader.readAsDataURL(blob);
      });
      if (version !== generation || !dialog.open) return;
      image.onload = () => { if (version === generation) { image.hidden = false; status.hidden = true; } };
      image.onerror = () => { if (version === generation) { status.textContent = "图片预览失败，可尝试下载原图。"; image.hidden = true; } };
      image.src = data;
    } catch (error) {
      if (error.name !== "AbortError" && version === generation) status.textContent = "暂时无法预览。请刷新后重试，或使用下载原图。";
    }
  };
  search.addEventListener("input", filter);
  role.addEventListener("change", filter);
  workspace.querySelector("[data-attachment-reset]").addEventListener("click", () => {
    search.value = ""; role.value = ""; filter(); search.focus();
  });
  if (typeof dialog.showModal === "function") buttons.forEach(button => {
    button.hidden = false;
    button.addEventListener("click", () => {
      visibleImages = buttons.filter(candidate => !candidate.closest("[data-attachment-row]").hidden);
      show(visibleImages.indexOf(button));
    });
  });
  previous.addEventListener("click", () => show(activeIndex - 1));
  next.addEventListener("click", () => show(activeIndex + 1));
  dialog.addEventListener("keydown", event => {
    if (event.key === "ArrowLeft" && activeIndex > 0) { event.preventDefault(); show(activeIndex - 1); }
    if (event.key === "ArrowRight" && activeIndex < visibleImages.length - 1) { event.preventDefault(); show(activeIndex + 1); }
  });
  dialog.querySelector("[data-attachment-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", cancel);
  window.addEventListener("pagehide", cancel);
  workspace.querySelector("[data-attachment-controls]").hidden = false;
  filter();
})();
