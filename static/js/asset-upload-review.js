(() => {
  "use strict";
  const input = document.querySelector("[data-upload-files]");
  if (!input) return;
  const form = input.closest("form");
  const review = form.querySelector("[data-upload-file-review]");
  const list = review.querySelector("[data-upload-file-list]");
  const summary = review.querySelector("[data-upload-summary]");
  const warning = review.querySelector("[data-upload-warning]");
  const allowed = new Set(input.dataset.allowedExtensions.split(","));
  const maxFiles = Number(input.dataset.maxFiles);
  const maxTotal = Number(input.dataset.maxTotalBytes);
  const maxFile = Number(input.dataset.maxFileBytes);
  const formatBytes = bytes => bytes < 1024 ? `${bytes} 字节`
    : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
  const update = () => {
    const files = Array.from(input.files || []);
    review.hidden = files.length === 0;
    list.replaceChildren();
    const total = files.reduce((sum, file) => sum + file.size, 0);
    summary.textContent = `已选择 ${files.length} 个文件 · 共 ${formatBytes(total)}`;
    const messages = [];
    if (files.length > maxFiles) messages.push(`文件数超过本次 ${maxFiles} 个的上限。`);
    if (total > maxTotal) messages.push(`合计大小超过 ${formatBytes(maxTotal)}。`);
    const photoRole = ["cover", "photo"].includes(form.elements.role.value);
    if (form.elements.role.value === "cover" && files.length > 1) messages.push("封面每次只能选择一张，请改用资产照片用途或重新选择文件。");
    files.forEach(file => {
      const item = document.createElement("li");
      const extension = file.name.split(".").pop().toLocaleLowerCase();
      const problems = [];
      if (!allowed.has(extension)) problems.push("当前公司未允许此格式");
      if (file.size > maxFile) problems.push("超过单个文件上限");
      if (file.size === 0) problems.push("文件为空");
      if (photoRole && !["jpg", "jpeg", "png", "webp"].includes(extension)) problems.push("照片用途需要图片文件");
      item.textContent = `${file.name} · ${formatBytes(file.size)}` + (problems.length ? ` · ${problems.join("；")}` : "");
      if (problems.length) { item.className = "text-danger"; messages.push(`${file.name} 需要核对。`); }
      list.appendChild(item);
    });
    warning.textContent = messages.join(" ");
    warning.hidden = !messages.length;
  };
  input.addEventListener("change", update);
  form.elements.role.addEventListener("change", update);
  review.querySelector("[data-upload-clear]").addEventListener("click", () => {
    input.value = "";
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.focus();
  });
  update();
})();
