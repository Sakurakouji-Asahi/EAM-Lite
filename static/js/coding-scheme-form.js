(() => {
  const form = document.querySelector("[data-coding-scheme-form]");
  if (!form) return;

  const resetMode = form.querySelector('[name="reset_mode"]');
  const categoryScope = form.querySelector('[name="category_scope_level"]');

  const syncCategoryScope = () => {
    if (!resetMode || !categoryScope) return;
    const usesCategory = ["category_yearly", "category_monthly"].includes(
      resetMode.value,
    );
    categoryScope.disabled = !usesCategory;
    if (!usesCategory) categoryScope.value = "";
    categoryScope.closest(".mb-3")?.classList.toggle("opacity-50", !usesCategory);
  };

  const fixedValueTypes = new Set(["fixed_text", "custom_text", "separator"]);

  const setFieldState = (field, enabled, clearWhenDisabled = true) => {
    if (!field) return;
    field.disabled = !enabled;
    if (!enabled && clearWhenDisabled) {
      if (field.type === "checkbox") field.checked = false;
      else field.value = "";
    }
    field.closest(".col-md-4")?.classList.toggle("opacity-50", !enabled);
  };

  const syncSegment = (container) => {
    const segmentType = container.querySelector('[name$="-segment_type"]');
    const fixedValue = container.querySelector('[name$="-fixed_value"]');
    const sequenceLength = container.querySelector('[name$="-sequence_length"]');
    const zeroPad = container.querySelector('[name$="-zero_pad"]');
    const sequenceOrder = container.querySelector('[name$="-sequence_order"]');
    const deleteField = container.querySelector('[name$="-DELETE"]');
    const deleted = Boolean(deleteField?.checked);

    setFieldState(sequenceOrder, !deleted, false);
    setFieldState(segmentType, !deleted, false);
    setFieldState(
      fixedValue,
      !deleted && fixedValueTypes.has(segmentType?.value),
      !deleted,
    );
    setFieldState(
      sequenceLength,
      !deleted && segmentType?.value === "sequence",
      !deleted,
    );
    setFieldState(
      zeroPad,
      !deleted && segmentType?.value === "sequence",
      !deleted,
    );
    container.classList.toggle("opacity-50", deleted);
    container
      .querySelectorAll(".invalid-feedback")
      .forEach((item) => item.classList.toggle("d-none", deleted));
  };

  resetMode?.addEventListener("change", syncCategoryScope);
  syncCategoryScope();

  form.querySelectorAll("[data-coding-segment-form]").forEach((container) => {
    container
      .querySelector('[name$="-segment_type"]')
      ?.addEventListener("change", () => syncSegment(container));
    container
      .querySelector('[name$="-DELETE"]')
      ?.addEventListener("change", () => syncSegment(container));
    syncSegment(container);
  });
})();
