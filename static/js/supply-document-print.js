(() => {
  const button = document.querySelector('[data-supply-document-print]');
  if (!button) return;
  button.hidden = false;
  button.addEventListener('click', () => window.print());
})();
