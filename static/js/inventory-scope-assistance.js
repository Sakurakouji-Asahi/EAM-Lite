(() => {
  "use strict";
  const form = document.querySelector('form[data-inventory-scope-assistance]');
  if (!form) return;
  const scope = form.elements.namedItem('scope_type');
  const type = form.elements.namedItem('inventory_type');
  const hint = form.querySelector('[data-inventory-scope-hint]');
  const assets = form.querySelector('[data-scope-assets]');
  if (!scope || !hint || !assets) return;
  const fieldNames = {department:'scope_department', category:'scope_category', location:'scope_location'};
  const groups = Object.entries(fieldNames).map(([type,name]) => {
    const field = form.elements.namedItem(name);
    const group = form.querySelector(`[data-form-field="${name}"]`);
    return {type,field,group};
  }).filter(item => item.field && item.group);
  const descriptions = {
    company:'全公司范围：按当前权限选择符合盘点条件的正式资产，发布前仍可核对应盘预览。',
    department:'部门范围：请选择一个部门，发布前核对该范围的应盘预览。',
    category:'分类范围：请选择一个实物分类，发布前核对该范围的应盘预览。',
    location:'位置范围：请选择一个位置，发布前核对该范围的应盘预览。',
    selected_assets:'勾选范围：至少选择一项资产；查找不会取消未显示的勾选。',
  };
  const update = () => {
    const requiredScope = {department:'department',full:'company'}[type?.value];
    if (requiredScope) scope.value = requiredScope;
    Array.from(scope.options).forEach(option => {option.disabled = Boolean(requiredScope && option.value !== requiredScope);});
    const value = scope.value;
    groups.forEach(({type,group}) => {group.hidden = value !== type;});
    assets.hidden = value !== 'selected_assets';
    hint.textContent = (descriptions[value] || '请选择盘点范围。') + ' 切换范围会保留本页输入，仅当前范围参与保存。';
    hint.hidden = false;
  };
  scope.addEventListener('change',update);
  type?.addEventListener('change',update);
  form.addEventListener('formdata',event => {
    // Leave page inputs intact, including the unsaved-input guard's initial snapshot.
    groups.forEach(({type,field}) => {if (scope.value !== type) event.formData.delete(field.name);});
    if (scope.value !== 'selected_assets') {
      event.formData.delete('selected_asset_ids');
      event.formData.delete('selected_asset_ids_ui');
    }
  });
  window.addEventListener('pageshow',update);
  update();
})();
