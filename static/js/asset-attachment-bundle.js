(() => {
  const form = document.querySelector('[data-attachment-bundle]');
  if (!form) return;
  const files = Array.from(form.querySelectorAll('[data-bundle-file]'));
  const download = form.querySelector('[data-bundle-download]');
  const status = form.querySelector('[data-bundle-status]');
  const visible = form.querySelector('[data-bundle-visible]');
  const clear = form.querySelector('[data-bundle-clear]');
  const maxFiles = Number(form.dataset.maxFiles), maxBytes = Number(form.dataset.maxBytes);
  const isVisible = field => !field.closest('[data-attachment-row]').hidden;
  const update = () => {
    const selected = files.filter(field => field.checked);
    const bytes = selected.reduce((total, field) => total + Number(field.dataset.fileBytes), 0);
    const hiddenCount = selected.filter(field => !isVisible(field)).length;
    const overLimit = selected.length > maxFiles || bytes > maxBytes;
    download.disabled = !selected.length || overLimit;
    clear.disabled = !selected.length;
    visible.disabled = !files.some(isVisible);
    download.textContent = selected.length ? `下载已选（${selected.length}）` : '下载已选附件';
    const size = bytes < 1024 ? `${bytes} 字节` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    status.textContent = `已选 ${selected.length} 个文件 · ${size}` +
      (hiddenCount ? `，其中 ${hiddenCount} 个不在当前显示结果中。` : '。') +
      (overLimit ? ' 超过单包限制，请减少选择或分批下载。' : '');
    status.classList.toggle('text-danger', overLimit);
  };
  visible.hidden = clear.hidden = false;
  visible.addEventListener('click', () => {files.filter(isVisible).forEach(field => {field.checked = true;}); update();});
  clear.addEventListener('click', () => {files.forEach(field => {field.checked = false;}); update();});
  form.addEventListener('change', update);
  form.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.matches('[data-attachment-search]')) event.preventDefault();
  });
  form.addEventListener('submit', event => {
    event.preventDefault();
    update();
    if (download.disabled) return;
    // A download keeps this page open; it must not enter the shared save-form busy state.
    const url = new URL(form.action);
    url.search = new URLSearchParams(new FormData(form)).toString();
    window.location.assign(url.href);
  });
  new MutationObserver(update).observe(form.querySelector('.list-group'), {subtree:true, attributes:true, attributeFilter:['hidden']});
  window.addEventListener('pageshow', update);
  update();
})();
