const summary = document.querySelector('[data-error-summary]');
if (summary) {
  const disclosure = summary.closest('details');
  if (disclosure) disclosure.open = true;
  summary.focus();
  summary.querySelectorAll('a[href^="#"]').forEach((link) => {
    link.addEventListener('click', (event) => {
      const field = document.getElementById(link.hash.slice(1));
      if (!field) return;
      event.preventDefault();
      const details = field.closest('details');
      if (details) details.open = true;
      field.focus();
    });
  });
}
// Success remains visible, but a later reload or bookmark is not a new save.
if (document.querySelector('.notice.success')) {
  const url = new URL(location.href);
  if (url.searchParams.has('saved') || url.searchParams.has('account_created')) {
    url.searchParams.delete('saved');
    url.searchParams.delete('account_created');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  }
}
