// Design 012: expand holding details and sort by column. The server
// renders the default order (market value, descending) and every value;
// this script only reorders rows and toggles visibility.
const SORT_STORAGE_KEY = 'taiwan-stock:holdings-sort';

document.querySelectorAll('[data-holdings-table]').forEach((table) => {
  const panel = table.closest('[data-holdings-display]');
  const controls = document.getElementById('holdings-controls');
  const sortPicker = controls.querySelector('[data-holdings-sort]');
  const directionPicker = controls.querySelector('[data-holdings-direction]');
  const displayPicker = controls.querySelector('[data-holdings-display-picker]');
  const directionToggle = controls.querySelector('[data-holdings-direction-toggle]');
  controls.querySelector('[data-holdings-apply]').hidden = true;
  // Design 056: one icon button flips the direction; the select stays for no-JS.
  controls.querySelector('[data-holdings-direction-field]').hidden = true;
  directionToggle.hidden = false;
  function showDirection() {
    const ascending = directionPicker.value === 'ascending';
    directionToggle.textContent = ascending ? '↑' : '↓';
    directionToggle.setAttribute(
      'aria-label',
      ascending ? '排序方向：由小到大，點選改為由大到小' : '排序方向：由大到小，點選改為由小到大',
    );
    directionToggle.title = ascending ? '由小到大' : '由大到小';
  }
  function displayValue() {
    return displayPicker.querySelector('input:checked')?.value || 'auto';
  }
  function rememberURL() {
    const url = new URL(location.href);
    url.searchParams.set('sort', sortPicker.value);
    url.searchParams.set('direction', directionPicker.value);
    url.searchParams.set('display', displayValue());
    history.replaceState(null, '', url.pathname + url.search);
    document.querySelectorAll('a[href^="/transactions?new=1"]').forEach((link) => {
      const target = new URL(link.href);
      target.searchParams.set('return_to', url.pathname + url.search);
      link.href = target.pathname + target.search + target.hash;
    });
  }
  const headers = Array.from(table.tHead.rows[0].cells);
  const sortable = headers.filter((th) => th.dataset.sortKey);
  // Server order doubles as the tie-breaker, so equal values keep the
  // default market-value / code / account order.
  Array.from(table.tBodies).forEach((body, index) => {
    body.dataset.defaultOrder = String(index);
  });

  sortable.forEach((th) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'sort-button';
    button.append(th.textContent.trim());
    const indicator = document.createElement('span');
    indicator.className = 'sort-indicator';
    indicator.setAttribute('aria-hidden', 'true');
    button.append(indicator);
    th.replaceChildren(button);
    button.addEventListener('click', () => {
      const current = th.getAttribute('aria-sort');
      let direction;
      if (current === 'descending') direction = 'ascending';
      else if (current === 'ascending') direction = 'descending';
      else direction = th.dataset.sortType === 'text' ? 'ascending' : 'descending';
      sortBy(th, direction);
      saveSort(th.dataset.sortKey, direction);
      rememberURL();
    });
  });

  function cellValue(body, columnIndex, type) {
    const raw = body.rows[0].cells[columnIndex].dataset.sort;
    if (raw === undefined || raw === '') return null;
    if (type === 'text') return raw;
    const number = Number(raw);
    return Number.isFinite(number) ? number : null;
  }

  function sortBy(th, direction) {
    const columnIndex = headers.indexOf(th);
    const type = th.dataset.sortType;
    const sign = direction === 'ascending' ? 1 : -1;
    const bodies = Array.from(table.tBodies);
    bodies.sort((a, b) => {
      const av = cellValue(a, columnIndex, type);
      const bv = cellValue(b, columnIndex, type);
      // Missing values (no price, unknown cost) always sink to the bottom.
      if (av === null || bv === null) {
        if (av !== bv) return av === null ? 1 : -1;
      } else {
        const diff = type === 'text' ? av.localeCompare(bv, 'zh-Hant') : av - bv;
        if (diff !== 0) return diff * sign;
      }
      return Number(a.dataset.defaultOrder) - Number(b.dataset.defaultOrder);
    });
    const cards = Array.from(panel.querySelectorAll('.mobile-records > li'));
    bodies.forEach((body) => {
      table.append(body);
      const card = cards.find((item) => item.dataset.holdingIndex === body.dataset.holdingIndex);
      if (card) card.parentElement.append(card);
    });
    sortPicker.value = th.dataset.sortKey;
    directionPicker.value = direction;
    showDirection();
    // The triangle is drawn by CSS from aria-sort (design 035).
    sortable.forEach((header) => {
      if (header === th) {
        header.setAttribute('aria-sort', direction);
      } else {
        header.removeAttribute('aria-sort');
      }
    });
  }

  function saveSort(key, direction) {
    try {
      localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify({ key, direction }));
    } catch {
      // Storage may be blocked (private mode); sorting still works for this view.
    }
  }

  function loadSort() {
    try {
      return JSON.parse(localStorage.getItem(SORT_STORAGE_KEY) || 'null');
    } catch {
      return null;
    }
  }

  const saved = new URL(location.href).searchParams.has('sort') ? null : loadSort();
  const savedHeader = saved && sortable.find((th) => th.dataset.sortKey === saved.key);
  if (savedHeader && ['ascending', 'descending'].includes(saved.direction)) {
    sortBy(savedHeader, saved.direction);
  } else {
    const initial = sortable.find((th) => th.hasAttribute('aria-sort'));
    if (initial) sortBy(initial, initial.getAttribute('aria-sort'));
  }

  function applyPickers() {
    const th = sortable.find((header) => header.dataset.sortKey === sortPicker.value);
    sortBy(th, directionPicker.value);
    saveSort(sortPicker.value, directionPicker.value);
    rememberURL();
  }
  sortPicker.addEventListener('change', applyPickers);
  directionToggle.addEventListener('click', () => {
    directionPicker.value = directionPicker.value === 'ascending' ? 'descending' : 'ascending';
    applyPickers();
  });
  displayPicker.addEventListener('change', () => {
    panel.dataset.holdingsDisplay = displayValue();
    rememberURL();
  });

  showDirection();
  rememberURL();

  table.addEventListener('click', (event) => {
    // Design 049 §4.2.5: a click on the holding row's blank space opens it
    // too; links and controls keep their own job, and selecting text to
    // copy a number doesn't count as a click.
    let toggle = event.target.closest('.lots-toggle');
    if (!toggle) {
      const row = event.target.closest('tbody.holding > tr:first-child');
      if (!row || event.target.closest('a, button, input, select, label')) return;
      if (String(window.getSelection?.() || '')) return;
      toggle = row.querySelector('.lots-toggle');
    }
    if (!toggle) return;
    const detail = document.getElementById(toggle.getAttribute('aria-controls'));
    if (!detail) return;
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    detail.classList.toggle('is-open', open);
  });
});
