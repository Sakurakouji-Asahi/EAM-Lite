(() => {
  const button = document.querySelector('[data-count-print]');
  if (!button) return;
  button.hidden = false;
  button.addEventListener('click', () => window.print());
})();
