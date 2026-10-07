(() => {
  const button = document.querySelector('[data-maintenance-record-print]');
  if (!button || typeof window.print !== 'function') return;
  button.hidden = false;
  button.addEventListener('click',() => window.print());
})();
