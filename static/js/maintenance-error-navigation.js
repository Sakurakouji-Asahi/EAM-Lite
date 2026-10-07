(() => {
  const summary = document.querySelector('[data-maintenance-error-summary]');
  if (!summary) return;

  const show = (element, center = false) => {
    const box = element.getBoundingClientRect();
    const headerHeight = document.querySelector('.app-topbar')?.getBoundingClientRect().height || 0;
    const offset = center ? Math.max(headerHeight + 16, (innerHeight - box.height) / 2) : headerHeight + 16;
    window.scrollTo({ top: window.scrollY + box.top - offset, behavior: 'instant' });
  };

  summary.addEventListener('click', event => {
    const link = event.target.closest('[data-maintenance-error-field]');
    if (!link || !summary.contains(link)) return;
    const field = document.getElementById(link.dataset.maintenanceErrorField);
    if (!field || !field.closest('form[data-maintenance-error-navigation]')) return;
    event.preventDefault();
    field.focus({ preventScroll: true });
    requestAnimationFrame(() => show(field, true));
  });

  summary.focus({ preventScroll: true });
  requestAnimationFrame(() => show(summary));
})();
