(() => {
  "use strict";
  const initialize = () => {
    document.querySelectorAll("select[data-department-field]").forEach((employees) => {
      if (employees.dataset.assignmentLinked) return;
      const department = employees.form?.elements.namedItem(employees.dataset.departmentField);
      if (!department) return;
      const update = () => {
        for (const option of employees.options) {
          const excluded = Boolean(option.value && department.value && option.dataset.department !== department.value);
          option.hidden = option.disabled = excluded;
        }
        if (employees.selectedOptions[0]?.disabled) employees.value = "";
      };
      department.addEventListener("change", update);
      employees.dataset.assignmentLinked = "true";
      update();
    });
    document.querySelectorAll("select[name='scope_type']").forEach((scope) => {
      if (scope.dataset.scopeLinked) return;
      const form = scope.form;
      const type = form.elements.namedItem("inventory_type");
      if (!type) return;
      const update = () => {
        const requiredScope = { department: "department", full: "company" }[type.value];
        if (requiredScope) scope.value = requiredScope;
        for (const option of scope.options) option.disabled = Boolean(requiredScope && option.value !== requiredScope);
        for (const [value, name] of Object.entries({department: "scope_department", category: "scope_category", location: "scope_location"})) {
          const input = form.elements.namedItem(name);
          if (input) {
            input.disabled = scope.value !== value;
            input.closest("[data-form-field]").hidden = input.disabled;
          }
        }
        const selected = form.querySelector("[data-scope-assets]");
        if (selected) {
          selected.hidden = scope.value !== "selected_assets";
          selected.querySelectorAll("input").forEach((input) => { input.disabled = selected.hidden; });
        }
      };
      scope.addEventListener("change", update);
      type.addEventListener("change", update);
      scope.dataset.scopeLinked = "true";
      update();
    });
  };
  initialize();
  document.addEventListener("htmx:load", initialize);
})();
