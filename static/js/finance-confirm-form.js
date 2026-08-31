document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("finance-confirm-form");
  const treatment = document.getElementById("id_accounting_treatment");
  if (!form || !treatment) {
    return;
  }

  const depreciationFieldIds = [
    "id_fixed_asset_category",
    "id_capitalization_date",
    "id_depreciation_policy",
    "id_useful_life_months",
    "id_salvage_mode",
    "id_salvage_rate",
    "id_salvage_amount",
    "id_method",
    "id_posting_period",
    "id_start_rule",
    "id_stop_rule",
    "id_specified_start_date",
    "id_actual_continuation_date",
    "id_expected_total_units",
    "id_work_unit",
    "id_annual_posting_month",
  ];
  const zeroOnlyFieldIds = [
    "id_opening_actual_accumulated_depreciation",
    "id_opening_impairment",
  ];
  const previewButton = form.querySelector("button[formaction]");

  const updateTreatmentFields = () => {
    const controlled = treatment.value === "controlled_non_fixed";
    for (const fieldId of depreciationFieldIds) {
      const field = document.getElementById(fieldId);
      if (!field) {
        continue;
      }
      const group = field.closest(".mb-3");
      if (controlled) {
        field.value = "";
      }
      field.disabled = controlled;
      if (group) {
        const hasServerError = Boolean(group.querySelector(".invalid-feedback"));
        group.classList.toggle("d-none", controlled && !hasServerError);
      }
    }
    for (const fieldId of zeroOnlyFieldIds) {
      const field = document.getElementById(fieldId);
      if (!field) {
        continue;
      }
      if (controlled) {
        field.value = "0.00";
      }
      field.readOnly = controlled;
      field.setAttribute("aria-readonly", controlled ? "true" : "false");
    }
    if (previewButton) {
      previewButton.disabled = controlled;
      previewButton.classList.toggle("d-none", controlled);
    }
  };

  treatment.addEventListener("change", updateTreatmentFields);
  updateTreatmentFields();
});
