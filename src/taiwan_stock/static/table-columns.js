// Design 045: drag a header's right edge to resize only that column. Widths
// the viewer changed are remembered per browser; untouched columns keep the
// CSS defaults, and a double-click (or Home) restores the table's defaults.
const MIN_WIDTH = 48;
const STEP = 16;
const BIG_STEP = 64;

function clampWidth(px) {
  return Math.max(MIN_WIDTH, Math.round(px));
}

function headerCells(table) {
  const head = table.tHead;
  // Two-row headers or spanning cells have no one-to-one column to resize.
  if (!head || head.rows.length !== 1) return [];
  const cells = Array.from(head.rows[0].cells);
  return cells.some(th => th.colSpan > 1) ? [] : cells;
}

function columnKey(th) {
  return th.dataset.colKey || th.textContent.replace(/\s+/g, '');
}

function storageKey(table, path) {
  const name = table.className.trim().split(/\s+/)[0] || 'table';
  // Stock and ETF results share a template but not their columns.
  const key = table.dataset.columnsKey || [name, table.dataset.securityType].filter(Boolean).join(':');
  return `taiwan-stock:columns:${path}:${key}`;
}

function readStore(storage, key) {
  try {
    const value = JSON.parse(storage.getItem(key) || '{}');
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch (_) {
    return {};
  }
}

function writeStore(storage, key, value) {
  try {
    if (Object.keys(value).length) storage.setItem(key, JSON.stringify(value));
    else storage.removeItem(key);
  } catch (_) {
    // Private windows or blocked storage still resize; they just forget.
  }
}

function syncTotal(table, cells) {
  const total = cells.reduce((sum, th) => sum + parseFloat(th.style.width), 0);
  table.style.width = `${total}px`;
}

// Pixel widths for every column, so changing one never resizes the others.
function freeze(table, cells = headerCells(table)) {
  if (table.dataset.columnsFrozen === 'true') return;
  const widths = cells.map(th => th.getBoundingClientRect().width);
  cells.forEach((th, index) => {
    th.style.width = `${clampWidth(widths[index])}px`;
    th.style.minWidth = '0';
  });
  table.style.tableLayout = 'fixed';
  table.style.minWidth = '0';
  syncTotal(table, cells);
  table.dataset.columnsFrozen = 'true';
}

function setWidth(table, th, px, cells = headerCells(table)) {
  freeze(table, cells);
  const width = clampWidth(px);
  th.style.width = `${width}px`;
  syncTotal(table, cells);
  return width;
}

function applySaved(table, saved, cells = headerCells(table)) {
  const matches = cells.filter(th => Number.isFinite(saved[columnKey(th)]));
  if (!matches.length) return false;
  freeze(table, cells);
  matches.forEach(th => { th.style.width = `${clampWidth(saved[columnKey(th)])}px`; });
  syncTotal(table, cells);
  return true;
}

function reset(table, cells = headerCells(table)) {
  cells.forEach(th => {
    th.style.width = '';
    th.style.minWidth = '';
  });
  table.style.width = '';
  table.style.minWidth = '';
  table.style.tableLayout = '';
  delete table.dataset.columnsFrozen;
}

function enhance(table, {storage, doc, path, observe}) {
  if (table.dataset.resizable === 'false') return null;
  const cells = headerCells(table);
  if (cells.length < 2) return null;
  const key = storageKey(table, path);
  const saved = readStore(storage, key);
  const handles = new Map();
  const label = th => th.textContent.trim();
  function update(th, width) {
    handles.get(th).setAttribute('aria-valuenow', String(Math.round(width)));
  }
  function remember(th, width) {
    saved[columnKey(th)] = width;
    writeStore(storage, key, saved);
  }
  function restore() {
    reset(table, cells);
    Object.keys(saved).forEach(name => { delete saved[name]; });
    writeStore(storage, key, saved);
    handles.forEach((handle, th) => update(th, th.getBoundingClientRect().width));
  }
  // The last column's right edge is the table's edge; its left neighbour
  // already resizes the boundary a viewer can see.
  cells.slice(0, -1).forEach(th => {
    const handle = doc.createElement('span');
    handle.className = 'column-resizer';
    handle.tabIndex = 0;
    handle.setAttribute('role', 'separator');
    handle.setAttribute('aria-orientation', 'vertical');
    handle.setAttribute('aria-label', `調整「${label(th)}」欄寬`);
    handle.setAttribute('aria-valuemin', String(MIN_WIDTH));
    th.classList.add('is-resizable');
    th.append(handle);
    handles.set(th, handle);
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      freeze(table, cells);
      const startX = event.clientX;
      const startWidth = parseFloat(th.style.width);
      let width = startWidth;
      handle.setPointerCapture?.(event.pointerId);
      handle.classList.add('is-dragging');
      doc.body?.classList.add('is-resizing-columns');
      const move = moveEvent => {
        width = setWidth(table, th, startWidth + moveEvent.clientX - startX, cells);
        update(th, width);
      };
      const end = () => {
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', end);
        handle.removeEventListener('pointercancel', end);
        handle.classList.remove('is-dragging');
        doc.body?.classList.remove('is-resizing-columns');
        if (width !== startWidth) remember(th, width);
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', end);
      handle.addEventListener('pointercancel', end);
    });
    // Never let the handle sort the column it sits in.
    handle.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
    });
    handle.addEventListener('dblclick', event => {
      event.preventDefault();
      event.stopPropagation();
      restore();
    });
    handle.addEventListener('keydown', event => {
      if (event.key === 'Home') {
        event.preventDefault();
        restore();
        return;
      }
      const sign = {ArrowLeft: -1, ArrowRight: 1}[event.key];
      if (!sign) return;
      event.preventDefault();
      freeze(table, cells);
      const step = event.shiftKey ? BIG_STEP : STEP;
      const width = setWidth(table, th, parseFloat(th.style.width) + sign * step, cells);
      update(th, width);
      remember(th, width);
    });
  });
  const visible = () => table.getBoundingClientRect().width > 0;
  const start = () => {
    applySaved(table, saved, cells);
    handles.forEach((handle, th) => update(th, th.getBoundingClientRect().width));
  };
  // A collapsed or mobile-hidden table has no widths to freeze yet.
  if (visible()) start();
  else observe?.(table, () => visible() && (start(), true));
  return {restore, handles};
}

function observeOnce(table, callback) {
  if (typeof ResizeObserver === 'undefined') return;
  const observer = new ResizeObserver(() => { if (callback()) observer.disconnect(); });
  observer.observe(table);
}

if (typeof document !== 'undefined') {
  let storage = null;
  try { storage = window.localStorage; } catch (_) { storage = null; }
  const safe = storage || {getItem: () => null, setItem() {}, removeItem() {}};
  document.querySelectorAll('table').forEach(table => {
    enhance(table, {storage: safe, doc: document, path: location.pathname, observe: observeOnce});
  });
}
if (typeof module !== 'undefined') {
  module.exports = {MIN_WIDTH, clampWidth, columnKey, storageKey, freeze, setWidth, applySaved, reset, enhance};
}
