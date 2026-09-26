document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("finance-confirm-form");
  const treatment = document.getElementById("id_accounting_treatment");
  if (!form || !treatment) return;
  const depreciationFields = ["fixed_asset_category", "capitalization_date", "depreciation_policy",
    "useful_life_months", "salvage_mode", "salvage_rate", "salvage_amount", "method", "posting_period",
    "start_rule", "stop_rule", "specified_start_date", "actual_continuation_date",
    "expected_total_units", "work_unit", "annual_posting_month"];
  const zeroFields = ["opening_actual_accumulated_depreciation", "opening_impairment"];
  const previousZeros = new Map();
  let wasControlled = false;
  const field = name => document.getElementById("id_" + name);
  const value = name => field(name)?.value || "";
  const visibility = (name, hidden) => {
    const input = field(name);
    if (!input) return;
    input.disabled = hidden;
    const group = input.closest("[data-finance-field]") || input.closest(".mb-3");
    if (group) group.classList.toggle("d-none", hidden && !group.querySelector(".invalid-feedback"));
  };
  const refresh = () => {
    const controlled = treatment.value === "controlled_non_fixed";
    document.querySelectorAll("[data-fixed-asset-category-warning]").forEach(notice => notice.classList.toggle("d-none", controlled));
    depreciationFields.forEach(name => visibility(name, controlled));
    zeroFields.forEach(name => {
      const input = field(name);
      if (!input) return;
      if (controlled && !wasControlled) previousZeros.set(name, input.value);
      if (controlled) input.value = "0.00";
      else if (wasControlled && previousZeros.has(name)) input.value = previousZeros.get(name);
      input.readOnly = controlled;
      input.setAttribute("aria-readonly", String(controlled));
      const group = input.closest("[data-finance-field]") || input.closest(".mb-3");
      if (group) group.classList.toggle("d-none", controlled && !group.querySelector(".invalid-feedback"));
    });
    if (!controlled) {
      visibility("salvage_amount", value("salvage_mode") === "rate");
      visibility("salvage_rate", value("salvage_mode") === "amount");
      visibility("specified_start_date", ["current_month", "next_month"].includes(value("start_rule")));
      visibility("annual_posting_month", value("posting_period") === "monthly");
      const noUsage = Boolean(value("method") && value("method") !== "units_of_production");
      visibility("expected_total_units", noUsage);
      visibility("work_unit", noUsage);
    }
    const preview = form.querySelector("button[formaction]");
    if (preview) { preview.disabled = controlled; preview.classList.toggle("d-none", controlled); }
    form.querySelectorAll("[data-finance-section]").forEach(section => {
      const groups = Array.from(section.querySelectorAll("[data-finance-field]"));
      section.hidden = groups.length > 0 && groups.every(group => group.classList.contains("d-none"));
    });
    wasControlled = controlled;
  };
  ["accounting_treatment", "salvage_mode", "start_rule", "posting_period", "method"].forEach(name => field(name)?.addEventListener("change", refresh));
  refresh();
});
