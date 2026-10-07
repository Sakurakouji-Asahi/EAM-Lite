"use strict";
document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-import-field-guide]").forEach((guide) => {
    const search = guide.querySelector("[data-import-field-search]");
    const requiredOnly = guide.querySelector("[data-import-required-only]");
    const count = guide.querySelector("[data-import-field-count]");
    const rows = Array.from(guide.querySelectorAll("[data-import-field]")).map((row) => ({
      element: row, text: row.textContent.toLocaleLowerCase(), required: row.dataset.importRequired === "true",
    }));
    const empty = guide.querySelector("[data-import-field-empty]");
    const filter = () => {
      const query = search.value.trim().toLocaleLowerCase();
      let visible = 0;
      rows.forEach((row) => {
        row.element.hidden = !row.text.includes(query) || requiredOnly.checked && !row.required;
        if (!row.element.hidden) visible += 1;
      });
      count.textContent = `显示 ${visible} / ${rows.length} 列`;
      empty.hidden = visible > 0;
    };
    search.addEventListener("input", filter);
    requiredOnly.addEventListener("change", filter);
    filter();
    guide.querySelector("[data-import-guide-search]").hidden = false;
  });

  document.querySelectorAll("form[data-import-upload]").forEach((form) => {
    const input = form.querySelector('input[type="file"][name="file"]');
    const zone = form.querySelector("[data-import-file-zone]");
    const review = form.querySelector("[data-import-file-review]");
    const name = form.querySelector("[data-import-file-name]");
    const size = form.querySelector("[data-import-file-size]");
    const message = form.querySelector("[data-import-file-message]");
    if (!input || !zone || !review || !name || !size || !message) return;
    const formatBytes = bytes => bytes < 1024 ? `${bytes} 字节`
      : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
    const problems = file => !/\.xlsx$/i.test(file.name) ? "请选择无宏的 .xlsx 标准模板文件。"
      : file.size === 0 ? "所选文件为空，请重新选择已填写的标准模板。" : "";
    const tell = text => { message.textContent = text; message.hidden = !text; };
    const update = () => {
      const file = input.files?.[0];
      review.hidden = !file;
      name.textContent = file?.name || "";
      size.textContent = file ? `文件大小：${formatBytes(file.size)}` : "";
      tell(file ? problems(file) : "");
    };
    input.addEventListener("change", update);
    form.querySelector("[data-import-file-clear]").addEventListener("click", () => {
      input.value = "";
      input.dispatchEvent(new Event("change", {bubbles: true}));
      input.focus();
    });
    form.addEventListener("reset", () => requestAnimationFrame(update));
    window.addEventListener("pageshow", update);
    update();

    // Enhance only browsers that can assign a dropped FileList to a file input.
    try {
      const probe = document.createElement("input");
      probe.type = "file";
      probe.files = new DataTransfer().files;
    } catch (_) { return; }
    form.querySelector("[data-import-drop-hint]").hidden = false;
    const hasFiles = event => Array.from(event.dataTransfer?.types || []).includes("Files");
    for (const type of ["dragenter", "dragover"]) zone.addEventListener(type, event => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
      zone.classList.add("is-dragging");
    });
    zone.addEventListener("dragleave", event => {
      if (!zone.contains(event.relatedTarget)) zone.classList.remove("is-dragging");
    });
    zone.addEventListener("drop", event => {
      zone.classList.remove("is-dragging");
      if (!hasFiles(event)) return;
      event.preventDefault();
      const files = Array.from(event.dataTransfer.files);
      const problem = files.length !== 1 ? "每批只接受一个文件，请只拖入一个 .xlsx 文件。" : problems(files[0]);
      if (problem) { tell(problem + (input.files.length ? " 原选择已保留。" : "")); return; }
      try {
        const transfer = new DataTransfer();
        transfer.items.add(files[0]);
        input.files = transfer.files;
      } catch (_) { tell("此浏览器无法拖入文件，请使用上方文件选择框；原选择已保留。"); return; }
      input.dispatchEvent(new Event("change", {bubbles: true}));
    });
  });
});
