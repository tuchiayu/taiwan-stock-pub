/* Design 044: refresh stale ETF size/holder data in the background; never reload the page. */
(() => {
  const status = document.querySelector('[data-etf-scale-status]');
  if (!status) return;
  const message = status.querySelector('[data-etf-scale-message]');
  const retry = status.querySelector('[data-etf-scale-retry]');
  let timer;
  let busy = false;
  let pending = null;
  let sawJob = false;
  function apply(data) {
    const parts = [data.message];
    if (data.activeJobId) parts.push('背景更新中…');
    else if (sawJob) parts.push('資料已更新，重新整理頁面以套用排序');
    message.textContent = parts.join(' · ');
    retry.hidden = !data.canRetry;
    retry.disabled = Boolean(data.activeJobId);
  }
  function schedule(delay) {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(check, delay);
  }
  async function check(force = false) {
    if (busy || document.hidden) return;
    busy = true;
    let delay = 0;
    try {
      const query = new URLSearchParams();
      if (pending) query.set('requestKey', pending.key);
      const response = await fetch(`/api/v2/etf-scale?${query}`, {cache: 'no-store', signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('無法取得規模資料狀態');
      const data = await response.json();
      if (pending && data.requestedJobId) pending = null;
      if (data.activeJobId) sawJob = true;
      apply(data);
      if (data.activeJobId) { delay = 2000; return; }
      if (!data.needsRefresh && !force && !pending) return;
      pending ||= {key: [...crypto.getRandomValues(new Uint32Array(4))].map(n => n.toString(16)).join('-'), force: Boolean(force)};
      const post = await fetch('/api/v2/etf-scale/refresh', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({requestKey: pending.key, force: pending.force}),
        signal: AbortSignal.timeout(15000),
      });
      if (!post.ok && post.status !== 409) throw new Error('規模資料更新未能送出');
      const body = await post.json();
      pending = null;
      // 200 means nothing was due after all; only a started job is polled.
      if (body.jobId || body.activeJobId) { sawJob = true; delay = 2000; }
    } catch (_) {
      // Keep the same key after an uncertain POST; admission is idempotent.
      message.textContent += ' · 暫時無法更新，稍後重試';
      retry.hidden = false;
      retry.disabled = false;
      delay = 30000;
    } finally {
      busy = false;
      if (delay) schedule(delay);
    }
  }
  retry.addEventListener('click', () => check(true));
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden) check();
  });
  check();
})();
