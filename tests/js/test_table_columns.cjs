const assert = require('node:assert/strict');
const {test} = require('node:test');
const columns = require('../../src/taiwan_stock/static/table-columns.js');

class Element {
  constructor(text = '', width = 100) {
    this.textContent = text;
    this.width = width;
    this.dataset = {};
    this.style = {};
    this.attrs = {};
    this.events = {};
    this.children = [];
    this.colSpan = 1;
    const names = new Set();
    this.classList = {add: n => names.add(n), remove: n => names.delete(n), contains: n => names.has(n)};
  }
  setAttribute(name, value) { this.attrs[name] = value; }
  append(child) { this.children.push(child); }
  addEventListener(type, fn) { (this.events[type] ??= []).push(fn); }
  removeEventListener(type, fn) { this.events[type] = (this.events[type] || []).filter(f => f !== fn); }
  emit(type, init = {}) {
    const event = {button: 0, clientX: 0, pointerId: 1, ...init};
    event.preventDefault = () => { event.prevented = true; };
    event.stopPropagation = () => { event.stopped = true; };
    (this.events[type] || []).slice().forEach(fn => fn(event));
    return event;
  }
  getBoundingClientRect() { return {width: this.style.width ? parseFloat(this.style.width) : this.width}; }
}

function makeTable(labels = ['代碼', '名稱', '市場'], {className = 'demo-table', widths = [100, 200, 300], hidden = false} = {}) {
  const cells = labels.map((label, i) => new Element(label, widths[i]));
  const table = new Element();
  table.className = className;
  table.tHead = {rows: [{cells}]};
  table.getBoundingClientRect = () => ({width: hidden ? 0 : cells.reduce((s, c) => s + c.getBoundingClientRect().width, 0)});
  return {table, cells, show() { hidden = false; }};
}

function memory(initial = {}) {
  const data = {...initial};
  return {data, getItem: k => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = v; }, removeItem: k => { delete data[k]; }};
}

const doc = {createElement: () => new Element(), body: new Element()};
const handleOf = th => th.children.find(c => c.className === 'column-resizer');
const enhance = (table, storage = memory(), extra = {}) => columns.enhance(table, {storage, doc, path: '/securities', ...extra});

test('storage and column keys separate stock and ETF tables', () => {
  const {table, cells} = makeTable();
  table.className = 'securities-table has-payment-month';
  table.dataset.securityType = 'etf';
  assert.equal(columns.storageKey(table, '/securities'), 'taiwan-stock:columns:/securities:securities-table:etf');
  table.dataset.columnsKey = 'custom';
  assert.equal(columns.storageKey(table, '/x'), 'taiwan-stock:columns:/x:custom');
  cells[1].textContent = ' 2026-10 發放事件 ';
  assert.equal(columns.columnKey(cells[1]), '2026-10發放事件');
  cells[1].dataset.colKey = 'pay_date';
  assert.equal(columns.columnKey(cells[1]), 'pay_date');
});

test('dragging changes only that column, keeps the total in sync and remembers it', () => {
  const {table, cells} = makeTable();
  const storage = memory();
  enhance(table, storage);
  const handle = handleOf(cells[0]);
  const down = handle.emit('pointerdown', {clientX: 500});
  assert.ok(down.stopped && down.prevented);
  handle.emit('pointermove', {clientX: 560});
  handle.emit('pointerup');
  assert.deepEqual(cells.map(c => c.style.width), ['160px', '200px', '300px']);
  assert.equal(table.style.width, '660px');
  assert.equal(table.style.tableLayout, 'fixed');
  assert.equal(handle.attrs['aria-valuenow'], '160');
  assert.deepEqual(JSON.parse(storage.data['taiwan-stock:columns:/securities:demo-table']), {代碼: 160});
  // A later move without a pointerdown does nothing: the listeners are gone.
  handle.emit('pointermove', {clientX: 900});
  assert.equal(cells[0].style.width, '160px');
});

test('columns never shrink below the minimum width', () => {
  const {table, cells} = makeTable();
  enhance(table);
  const handle = handleOf(cells[1]);
  handle.emit('pointerdown', {clientX: 500});
  handle.emit('pointermove', {clientX: -1000});
  handle.emit('pointerup');
  assert.equal(cells[1].style.width, `${columns.MIN_WIDTH}px`);
});

test('saved widths apply by column key and ignore columns that no longer exist', () => {
  const {table, cells} = makeTable();
  const key = 'taiwan-stock:columns:/securities:demo-table';
  enhance(table, memory({[key]: JSON.stringify({名稱: 260, 舊欄位: 999})}));
  assert.deepEqual(cells.map(c => c.style.width), ['100px', '260px', '300px']);
  assert.equal(table.style.width, '660px');
});

test('double-click or Home restores defaults and clears the saved widths', () => {
  for (const restore of [h => h.emit('dblclick'), h => h.emit('keydown', {key: 'Home'})]) {
    const {table, cells} = makeTable();
    const key = 'taiwan-stock:columns:/securities:demo-table';
    const storage = memory({[key]: JSON.stringify({名稱: 260})});
    enhance(table, storage);
    restore(handleOf(cells[0]));
    assert.deepEqual(cells.map(c => c.style.width), ['', '', '']);
    assert.equal(table.style.width, '');
    assert.equal(table.style.tableLayout, '');
    assert.equal(table.dataset.columnsFrozen, undefined);
    assert.equal(storage.data[key], undefined);
  }
});

test('arrow keys resize in small or large steps and save immediately', () => {
  const {table, cells} = makeTable();
  const storage = memory();
  enhance(table, storage);
  const handle = handleOf(cells[1]);
  handle.emit('keydown', {key: 'ArrowRight'});
  assert.equal(cells[1].style.width, '216px');
  handle.emit('keydown', {key: 'ArrowLeft', shiftKey: true});
  assert.equal(cells[1].style.width, '152px');
  assert.equal(JSON.parse(storage.data['taiwan-stock:columns:/securities:demo-table']).名稱, 152);
  const ignored = handle.emit('keydown', {key: 'Enter'});
  assert.equal(ignored.prevented, undefined);
});

test('blocked storage still resizes, it only forgets', () => {
  const {table, cells} = makeTable();
  const broken = {getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); }};
  enhance(table, broken);
  const handle = handleOf(cells[0]);
  handle.emit('pointerdown', {clientX: 0});
  handle.emit('pointermove', {clientX: 30});
  handle.emit('pointerup');
  assert.equal(cells[0].style.width, '130px');
  handle.emit('dblclick');
  assert.equal(cells[0].style.width, '');
});

test('handles are labelled, skip the last column and never sort on click', () => {
  const {table, cells} = makeTable();
  enhance(table);
  assert.equal(handleOf(cells[2]), undefined);
  const handle = handleOf(cells[0]);
  assert.equal(handle.attrs.role, 'separator');
  assert.equal(handle.attrs['aria-label'], '調整「代碼」欄寬');
  assert.equal(handle.attrs['aria-valuemin'], String(columns.MIN_WIDTH));
  assert.equal(handle.tabIndex, 0);
  const click = handle.emit('click');
  assert.ok(click.stopped && click.prevented);
  assert.equal(handle.emit('pointerdown', {button: 2}).prevented, undefined);
});

test('tables without one plain header row, or opted out, are left alone', () => {
  const opted = makeTable();
  opted.table.dataset.resizable = 'false';
  assert.equal(enhance(opted.table), null);
  const spanning = makeTable();
  spanning.cells[1].colSpan = 2;
  assert.equal(enhance(spanning.table), null);
  const headless = makeTable();
  headless.table.tHead = null;
  assert.equal(enhance(headless.table), null);
  const twoRows = makeTable();
  twoRows.table.tHead.rows.push({cells: []});
  assert.equal(enhance(twoRows.table), null);
  for (const t of [opted, spanning]) assert.ok(t.cells.every(c => !c.children.length));
});

test('a hidden table applies saved widths once it becomes visible', () => {
  const key = 'taiwan-stock:columns:/securities:demo-table';
  const hidden = makeTable(undefined, {hidden: true});
  let callback = null;
  enhance(hidden.table, memory({[key]: JSON.stringify({名稱: 250})}), {observe: (_, fn) => { callback = fn; }});
  assert.equal(hidden.cells[1].style.width, undefined);
  assert.equal(callback(), false);
  hidden.show();
  assert.equal(callback(), true);
  assert.equal(hidden.cells[1].style.width, '250px');
});
