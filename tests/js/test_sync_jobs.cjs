/* Exercise the shipped UI controller with a deterministic DOM/HTTP boundary. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/taiwan_stock/static/sync-jobs.js', 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };
const reply = (body) => ({ok: true, json: async () => body});
const clock = '2026-10-12T00:00:30Z';
const job = (status = 'running') => ({id: 'job', operation: 'prices', request_key: 'key', status, phase: 'TWSE 行情', processed: 2, total: 4, failed_units: 0, submitted_at: '2026-10-12T00:00:00Z', started_at: status === 'queued' ? null : '2026-10-12T00:00:00Z', finished_at: ['queued', 'running'].includes(status) ? null : clock});
const snapshot = (jobs = [], blocked = jobs.filter((j) => ['queued', 'running'].includes(j.status)).map((j) => j.operation)) => ({jobs, blocked_operations: blocked, server_now: clock, legacy_sources: []});
class Element {
  setAttribute(name, value) { this.attributes[name] = value; }
  constructor() { this.children = []; this.ownText = ''; this.hidden = false; this.attributes = {}; }
  set textContent(value) { this.ownText = value; }
  get textContent() { return this.ownText + this.children.map((child) => typeof child === 'string' ? child : child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
  replaceChildren() { this.children = []; this.ownText = ''; }
}
function page(initial, handler, href = 'http://localhost/', operation = 'prices', options = {}) {
  const messages = new Element();
  const panel = new Element();
  panel.querySelector = () => messages;
  const button = new Element();
  button.dataset = {syncLabel: operation === 'prices' ? '同步最新股價' : '同步資料'};
  const label = new Element();
  label.textContent = button.dataset.syncLabel;
  const icon = new Element();
  button.append(icon, label);
  button.querySelector = () => label;
  const hint = new Element();
  const control = new Element();
  control.dataset = {syncTitle: '同步說明'};
  control.title = '同步說明';
  control.removeAttribute = (name) => { delete control[name]; };
  const key = {value: 'key'};
  const handlers = {};
  const controls = {'[data-sync-hint]': hint, '[data-sync-control]': control};
  const form = {
    dataset: {syncOperation: operation},
    querySelector: (selector) => selector.startsWith('button') ? button : controls[selector] || key,
    attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener: (name, fn) => { handlers[name] = fn; },
  };
  const timers = {};
  const timeouts = new Map();
  let now = 0;
  let timerId = 0;
  const events = {};
  const calls = [];
  const prompts = [];
  const location = {href, assignments: [], replacements: [], assign(url) { this.assignments.push(String(url)); this.href = String(url); }, replace(url) { this.replacements.push(String(url)); this.href = String(url); }};
  const document = {
    hidden: Boolean(options.hidden),
    getElementById: (id) => id === 'sync-initial' ? {textContent: JSON.stringify(initial)} : panel,
    querySelectorAll: () => [form], querySelector: () => options.detailId ? {dataset: {syncDetail: options.detailId}} : null,
    createElement: () => new Element(),
    addEventListener: (name, fn) => { events[name] = fn; },
  };
  vm.runInNewContext(source, {
    document, location, URL,
    history: {replaceState: (_, __, url) => { location.href = String(url); }},
    window: {addEventListener: (name, fn) => { events[name] = fn; }},
    performance: {now: () => now}, AbortSignal: {timeout: () => undefined},
    confirm: (text) => { prompts.push(text); return options.confirm ?? true; },
    setInterval: (fn, delay) => { timers[delay] = fn; },
    setTimeout: (fn, delay) => { const id = ++timerId; timeouts.set(id, {fn, at: now + delay}); return id; },
    clearTimeout: (id) => { timeouts.delete(id); },
    fetch: (url, options) => { calls.push({url, options}); return handler(url, options); },
  });
  async function advance(ms) {
    const end = now + ms;
    for (;;) {
      const next = [...timeouts.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      now = next[1].at;
      timeouts.delete(next[0]);
      await next[1].fn();
      await flush();
    }
    now = end;
  }
  return {button, label, icon, hint, control, form, messages, location, timers, events, calls, prompts, document, advance,
    delays: () => [...timeouts.values()].map((timer) => timer.at - now),
    submit: () => handlers.submit({preventDefault() {}})};
}
(async () => {
  // An existing CLI job restores its original elapsed time; its button cancels it.
  let offline = false;
  const active = page({snapshot: snapshot([job()]), selected: null}, async () => {
    if (offline) throw new Error('offline');
    return reply(snapshot([job()]));
  });
  assert.equal(active.button.disabled, false);
  assert.equal(active.label.textContent, '取消同步');
  assert.equal(active.form.attributes['aria-busy'], 'true');
  assert.match(active.messages.textContent, /00:30/);
  await flush();
  offline = true;
  await active.advance(2000);
  assert(active.button.disabled);
  assert.equal(active.label.textContent, '確認同步狀態…');
  assert.match(active.messages.textContent, /暫時無法取得進度/);
  offline = false;
  active.events.pageshow();
  assert(active.button.disabled);
  await flush();
  // BFCache restores the cancel control, never the start one, for an active job.
  assert.equal(active.label.textContent, '取消同步');
  assert.equal(active.button.disabled, false);

  // A queued own job can be cancelled too; a different operation holding a
  // shared lock still blocks.
  const queued = page({snapshot: snapshot([job('queued')]), selected: null}, async () => reply(snapshot([job('queued')])));
  assert.equal(queued.label.textContent, '取消同步');
  assert.equal(queued.button.disabled, false);
  const blockingJob = {...job(), operation: 'stock-dividends'};
  const conflictState = snapshot([blockingJob], ['stock-announcements']);
  const conflict = page({snapshot: conflictState, selected: null}, async () => reply(conflictState), undefined, 'stock-announcements');
  assert(conflict.button.disabled);
  assert.equal(conflict.label.textContent, '其他同步中…');
  assert.match(conflict.hint.textContent, /其他同步正在執行/);
  assert.equal(conflict.hint.hidden, false);
  assert.match(conflict.control.title, /其他同步正在執行/);
  assert.equal(conflict.form.attributes['aria-busy'], 'false');
  // Once the other job ends, the label and original tooltip come back.
  conflictState.jobs = [];
  conflictState.blocked_operations = [];
  await flush();
  await conflict.advance(2000);
  assert.equal(conflict.button.disabled, false);
  assert.equal(conflict.label.textContent, '同步資料');
  assert.equal(conflict.control.title, '同步說明');
  const unrelated = page({snapshot: snapshot([job()]), selected: null}, async () => reply(snapshot([job()])), undefined, 'catalog');
  assert.equal(unrelated.button.disabled, false);
  assert.equal(unrelated.hint.hidden, true);
  assert.equal(unrelated.control.title, '同步說明');
  assert.equal(active.control.title, '同步說明'); // Own job keeps the original tooltip.

  // A GET begun before a POST cannot unlock its pending submission.
  const oldGet = deferred();
  const post = deferred();
  let firstGet = true;
  let backendJob = null;
  const ui = page({snapshot: snapshot(), selected: null}, async (url, options) => {
    if (options.method === 'POST') return post.promise;
    if (firstGet) { firstGet = false; return oldGet.promise; }
    if (url.endsWith('/job')) return reply(backendJob);
    return reply(snapshot(backendJob ? [backendJob] : []));
  });
  ui.submit();
  assert(ui.button.disabled);
  assert.equal(ui.label.textContent, '送出中…');
  oldGet.resolve(reply(snapshot()));
  await flush();
  assert(ui.button.disabled);
  // The POST response is lost, but the backend accepted it. Polling reconnects
  // by idempotency key; it must never send another POST.
  backendJob = job();
  post.reject(new Error('response lost'));
  await flush();
  assert.equal(ui.label.textContent, '確認同步狀態…');
  await ui.advance(2000);
  assert.equal(ui.label.textContent, '取消同步');
  assert.equal(new URL(ui.location.href).searchParams.get('job_id'), 'job');
  assert.equal(ui.calls.filter((call) => call.options.method === 'POST').length, 1);
  assert.match(ui.messages.textContent, /00:30/);
  backendJob = job('success');
  await ui.advance(2000);
  assert.equal(ui.location.replacements.length, 1);
  const detailCalls = ui.calls.filter((call) => call.url.endsWith('/job')).length;
  await ui.advance(15000);
  assert.equal(ui.location.replacements.length, 1);
  assert.equal(ui.calls.filter((call) => call.url.endsWith('/job')).length, detailCalls);
  assert.equal(ui.button.disabled, false);
  assert.equal(ui.label.textContent, '同步最新股價');
  assert.equal(ui.button.children[0], ui.icon);
  assert.equal(ui.hint.hidden, true);
  assert.equal(ui.form.attributes['aria-busy'], 'false');
  assert.match(ui.messages.textContent, /完成 00:30/);
  // Restoring an older backup can remove a selected job; a confirmed 404
  // clears that stale URL rather than permanently disabling all forms.
  const restored = page({snapshot: snapshot(), selected: null}, async (url) =>
    url.endsWith('/gone') ? {ok: false, status: 404} : reply(snapshot()),
    'http://localhost/?job_id=gone');
  await flush();
  assert.equal(restored.button.disabled, false);
  assert.equal(new URL(restored.location.href).searchParams.has('job_id'), false);
  for (const status of ['partial', 'failed', 'interrupted']) {
    const terminal = page({snapshot: snapshot(), selected: job(status)}, async () => reply(snapshot()));
    assert.equal(terminal.button.disabled, false);
    assert.equal(terminal.label.textContent, '同步最新股價');
  }
  // Every Web sync operation shares the same active/terminal polling lifecycle.
  for (const operation of ['prices', 'catalog', 'stock-announcements', 'held-etf-distributions', 'investment-trust']) {
    for (const status of ['success', 'partial', 'failed', 'interrupted']) {
      let current = {...job('queued'), operation};
      const running = page({snapshot: snapshot([current]), selected: current}, async (url) =>
        reply(url.endsWith('/job') ? current : snapshot(['queued', 'running'].includes(current.status) ? [current] : [])),
        'http://localhost/?job_id=job', operation);
      await flush();
      assert.deepEqual(running.delays(), [2000]);
      assert.equal(running.label.textContent, '取消同步');
      current = {...job(), operation};
      await running.advance(2000);
      assert.equal(running.label.textContent, '取消同步');
      assert.deepEqual(running.delays(), [2000]);
      current = {...job(status), operation};
      await running.advance(2000);
      assert.equal(running.button.disabled, false);
      assert.equal(running.location.replacements.length, 1);
      assert.match(running.messages.textContent, new RegExp({success: '完成', partial: '部分成功', failed: '失敗', interrupted: '中斷'}[status]));
      assert.deepEqual(running.delays(), [15000]);
      const beforeIdle = running.calls.length;
      const beforeDetails = running.calls.filter((call) => call.url.endsWith('/job')).length;
      await running.advance(60000);
      assert.equal(running.calls.length - beforeIdle, 4);
      assert.equal(running.calls.filter((call) => call.url.endsWith('/job')).length, beforeDetails);
      assert.equal(running.location.replacements.length, 1);
      assert.equal(running.calls.filter((call) => call.options.method === 'POST').length, 0);
    }
  }
  // Completed results rendered by the server also skip detail requests after reload.
  const finished = page({snapshot: snapshot(), selected: job('success')}, async () => reply(snapshot()),
    'http://localhost/?job_id=job&job_done=job');
  await flush();
  await finished.advance(60000);
  assert.equal(finished.calls.length, 5); // Initial snapshot plus four idle polls.
  assert(finished.calls.every((call) => call.url === '/api/v2/sync-jobs?active=true'));
  assert.equal(finished.location.replacements.length, 0);
  assert.match(finished.messages.textContent, /完成/);

  // Idle discovery still notices CLI/other-tab jobs and switches to fast polling.
  let discovered = [];
  const idle = page({snapshot: snapshot(), selected: null}, async () => reply(snapshot(discovered)));
  await flush();
  assert.deepEqual(idle.delays(), [15000]);
  await idle.advance(14999);
  assert.equal(idle.calls.length, 1);
  discovered = [job()];
  await idle.advance(1);
  assert.equal(idle.calls.length, 2);
  assert.equal(idle.label.textContent, '取消同步');
  assert.deepEqual(idle.delays(), [2000]);

  // Hidden tabs slow down; resuming/pageshow refreshes immediately with one timer.
  idle.document.hidden = true;
  idle.events.visibilitychange();
  assert.deepEqual(idle.delays(), [30000]);
  const beforeHidden = idle.calls.length;
  await idle.advance(29999);
  assert.equal(idle.calls.length, beforeHidden);
  await idle.advance(1);
  assert.equal(idle.calls.length, beforeHidden + 1);
  assert.deepEqual(idle.delays(), [30000]);
  idle.document.hidden = false;
  idle.events.visibilitychange();
  assert.equal(idle.calls.length, beforeHidden + 2);
  await flush();
  assert.deepEqual(idle.delays(), [2000]);
  idle.events.pageshow();
  await flush();
  assert.equal(idle.calls.length, beforeHidden + 3);
  assert.deepEqual(idle.delays(), [2000]);

  const hidden = page({snapshot: snapshot(), selected: null}, async () => reply(snapshot()), undefined, 'prices', {hidden: true});
  await flush();
  assert.deepEqual(hidden.delays(), [30000]);

  // Switching visibility during an in-flight request never starts parallel GETs.
  const held = deferred();
  let gets = 0;
  const inflight = page({snapshot: snapshot(), selected: null}, async () => ++gets === 1 ? held.promise : reply(snapshot()));
  inflight.document.hidden = true;
  inflight.events.visibilitychange();
  inflight.document.hidden = false;
  inflight.events.visibilitychange();
  inflight.events.pageshow();
  assert.equal(inflight.calls.length, 1);
  held.resolve(reply(snapshot()));
  await flush();
  assert.equal(inflight.calls.length, 2); // Coalesced immediate refresh after the held GET.
  assert.deepEqual(inflight.delays(), [15000]);

  // Submitting from idle schedules recovery promptly instead of waiting 15 seconds.
  const uncertain = deferred();
  const prompt = page({snapshot: snapshot(), selected: null}, async (_, options) =>
    options.method === 'POST' ? uncertain.promise : reply(snapshot()));
  await flush();
  assert.deepEqual(prompt.delays(), [15000]);
  prompt.submit();
  assert.deepEqual(prompt.delays(), [2000]);
  uncertain.reject(new Error('offline'));
  await flush();
  assert(prompt.button.disabled);

  // A slow POST must stay locked even if a new status GET still finds no job.
  const slowPost = deferred();
  const lateStatus = deferred();
  let slowGets = 0;
  const slow = page({snapshot: snapshot(), selected: null}, async (_, options) => {
    if (options.method === 'POST') return slowPost.promise;
    slowGets += 1;
    return slowGets === 3 ? lateStatus.promise : reply(snapshot());
  });
  await flush();
  slow.submit();
  await slow.advance(2000);
  assert(slow.button.disabled);
  assert.equal(slow.label.textContent, '送出中…');
  slow.submit();
  assert.equal(slow.calls.filter((call) => call.options.method === 'POST').length, 1);
  slow.events.pageshow(); // This GET begins while the POST is still pending.
  slowPost.reject(new Error('response lost'));
  await flush();
  lateStatus.resolve(reply(snapshot()));
  await flush();
  assert(slow.button.disabled); // An old snapshot cannot settle an uncertain POST.
  await slow.advance(2000); // A fresh lookup may confirm no job was accepted.
  assert.equal(slow.button.disabled, false);
  assert(slow.calls.at(-1).url.includes('request_key=key'));
  assert.deepEqual(slow.delays(), [15000]);

  // Design 081: declining the confirmation sends nothing.
  const declined = page({snapshot: snapshot([job()]), selected: null}, async () => reply(snapshot([job()])),
    undefined, 'prices', {confirm: false});
  await flush();
  await declined.submit();
  assert.deepEqual(declined.prompts, ['確定取消同步？已完成的項目會保留，之後可以重試。']);
  assert.equal(declined.calls.filter((call) => call.options.method === 'POST').length, 0);
  assert.equal(declined.label.textContent, '取消同步');

  // Confirming cancels the own job, locks the button until it stops, then the
  // finished job reloads the page like any other result.
  let backend = job();
  const cancelReply = deferred();
  const cancelled = page({snapshot: snapshot([backend]), selected: null}, async (url, options) => {
    if (options.method === 'POST') return cancelReply.promise;
    return reply(snapshot(['queued', 'running'].includes(backend.status) ? [backend] : []));
  });
  await flush();
  const pending = cancelled.submit();
  assert.equal(cancelled.label.textContent, '取消中…');
  assert(cancelled.button.disabled);
  cancelled.submit(); // A second click while the request is pending sends nothing.
  const posts = cancelled.calls.filter((call) => call.options.method === 'POST');
  assert.deepEqual(posts.map((call) => call.url), ['/api/v2/sync-jobs/job/cancel']);
  backend = {...job(), cancel_requested: 1};
  cancelReply.resolve(reply(backend));
  await pending;
  await flush();
  assert.equal(cancelled.label.textContent, '取消中…');
  assert(cancelled.button.disabled);
  assert.match(cancelled.hint.textContent, /等目前的請求結束後停止/);
  await cancelled.advance(2000);
  assert.equal(cancelled.label.textContent, '取消中…');
  backend = {...job('interrupted'), cancel_requested: 1};
  await cancelled.advance(2000);
  assert.equal(cancelled.location.replacements.length, 1);
  assert.equal(new URL(cancelled.location.href).searchParams.get('job_done'), 'job');
  assert.equal(cancelled.button.disabled, false);
  assert.equal(cancelled.label.textContent, '同步最新股價');

  // A failed cancel request never shows the job as cancelled or restartable.
  const lost = page({snapshot: snapshot([job()]), selected: null}, async (_, options) => {
    if (options.method === 'POST') throw new Error('offline');
    return reply(snapshot([job()]));
  });
  await flush();
  await lost.submit();
  assert(lost.button.disabled);
  assert.equal(lost.label.textContent, '確認同步狀態…');
  await flush();
  assert.equal(lost.label.textContent, '取消同步');

  // Another operation's job is never cancelled from this button.
  const otherState = snapshot([{...job(), operation: 'stock-dividends'}], ['stock-announcements']);
  const other = page({snapshot: otherState, selected: null}, async () => reply(otherState), undefined, 'stock-announcements');
  await flush();
  await other.submit();
  assert.equal(other.prompts.length, 0);
  assert.equal(other.calls.filter((call) => call.options.method === 'POST').length, 0);
  assert.equal(other.label.textContent, '其他同步中…');

  // The dedicated detail page discovers its selection even without a URL query.
  const detail = page({snapshot: snapshot(), selected: null}, async (url) =>
    reply(url.endsWith('/job') ? job('success') : snapshot()), 'http://localhost/sync-jobs/job', 'prices', {detailId: 'job'});
  await flush();
  await detail.advance(30000);
  assert.equal(detail.calls.filter((call) => call.url.endsWith('/job')).length, 1);
  assert.equal(detail.location.replacements.length, 1);
  console.log('adaptive polling: all Web operations, terminal detail stop, idle discovery, visibility, single timer passed');
  console.log('sync UI: elapsed restore, BFCache, disconnect, stale GET, uncertain POST, terminal refresh passed');
  console.log('cancel: confirmation, pending lock, cancelling state, failure, other operation passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
