(() => {
  "use strict";

  const form = document.querySelector("[data-maintenance-completion]");
  if (!form) return;
  const content = form.elements.namedItem("actual_content");
  const result = form.elements.namedItem("result");
  const problem = form.elements.namedItem("problem_description");
  const button = document.querySelector("[data-maintenance-use-standard]");
  const status = document.querySelector("[data-maintenance-copy-status]");
  const source = document.getElementById("maintenance-standard-content");

  if (content && button && source) {
    const standard = JSON.parse(source.textContent);
    const updateCopyButton = () => {
      const hasInput = content.value.trim().length > 0;
      button.disabled = hasInput;
      button.textContent = hasInput ? "已填写实际内容" : "引用到实际完成内容";
      button.title = hasInput ? "保留已输入内容；清空实际内容后可重新引用。" : "引用标准内容，再按实际执行情况补充结果。";
    };
    button.hidden = false;
    content.addEventListener("input", updateCopyButton);
    button.addEventListener("click", () => {
      // Recheck so a delayed click never overwrites the user's own report.
      if (content.value.trim()) return;
      content.value = standard;
      content.dispatchEvent(new Event("input", { bubbles: true }));
      content.focus();
      if (status) status.textContent = "已引用标准内容，请按现场实际完成情况修改。";
    });
    updateCopyButton();
  }

  if (result && problem) {
    const label = problem.labels?.[0];
    const marker = document.createElement("span");
    marker.className = "required-mark";
    marker.setAttribute("aria-hidden", "true");
    marker.textContent = "*";
    label?.append(marker);
    const updateProblem = () => {
      const required = result.value === "problem_found";
      problem.required = required;
      problem.setAttribute("aria-required", String(required));
      marker.hidden = !required;
      problem.setCustomValidity(!required && problem.value.trim()
        ? "正常结果请保持问题说明为空；如需跟进，请将结果改为发现问题。" : "");
    };
    result.addEventListener("change", updateProblem);
    problem.addEventListener("input", updateProblem);
    window.addEventListener("pageshow", updateProblem);
    updateProblem();
  }
})();
