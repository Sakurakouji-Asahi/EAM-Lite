(() => {
  "use strict";
  const form = document.querySelector("form[data-custody-transfer-review]");
  const summary = document.querySelector("[data-custody-transfer-summary]");
  if (!form || !summary) return;
  const department = form.elements.namedItem("target_department");
  const employee = form.elements.namedItem("target_employee");
  if (!(department instanceof HTMLSelectElement) || !(employee instanceof HTMLSelectElement)) return;
  const departmentLabel = summary.querySelector("[data-receiving-department]");
  const employeeLabel = summary.querySelector("[data-receiving-employee]");
  const message = summary.querySelector("[data-receiving-message]");
  const update = () => {
    const departmentOption = department.selectedOptions[0];
    const employeeOption = employee.selectedOptions[0];
    const validDepartment = Boolean(department.value && departmentOption && !departmentOption.disabled && !departmentOption.hidden);
    const validEmployee = Boolean(employee.value && employeeOption && !employeeOption.disabled && !employeeOption.hidden
      && employeeOption.dataset.department === department.value);
    departmentLabel.textContent = validDepartment ? departmentOption.textContent.trim() : "请选择目标责任部门";
    employeeLabel.textContent = !validDepartment ? "选择部门后核对接收员工"
      : validEmployee ? employeeOption.textContent.trim() : employee.value ? "接收员工与部门不符，请重新选择" : "部门保管（不指定员工）";
    message.textContent = !validDepartment ? "先选接收部门，再选择属于该部门的员工；不指定员工时登记为部门保管。"
      : employee.value && !validEmployee ? "接收员工需要重新核对，提交时仍按原部门归属规则校验。"
      : validEmployee ? "已选择接收员工，请核对身份、数量和转交原因后再确认。"
      : "本次按部门保管登记；若由具体员工接收，请在下方选择该员工。";
  };
  for (const field of [department, employee]) {
    field.addEventListener("input", update);
    field.addEventListener("change", update);
  }
  summary.hidden = false;
  update();
})();
