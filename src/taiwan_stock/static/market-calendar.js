/* Annotations update in place; amounts, focus and payment dialogs stay intact. */
(() => {
  const calendar = document.querySelector('[data-market-calendar]');
  if (!calendar) return;
  const year = Number(calendar.dataset.marketYear);
  const month = calendar.dataset.marketMonth;
  const status = document.querySelector('[data-market-status]');
  const retry = status.querySelector('[data-market-retry]');
  const message = status.querySelector('[data-market-message]');
  const updated = status.querySelector('[data-market-updated]');
  let timer;
  let busy = false;
  let pending = null;
  // Design 051 §4.2.3: the site-wide Taipei `YYYY-MM-DD HH:MM`.
  const time = value => {
    if (!value) return '尚未取得';
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Taipei', hourCycle: 'h23',
      year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).formatToParts(new Date(value)).map(part => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  };
  function apply(data) {
    calendar.querySelectorAll('[data-market-day]').forEach(cell => {
      const day = cell.dataset.marketDay;
      const event = data.events[day];
      const weekday = new Date(`${day}T12:00:00+08:00`).getUTCDay();
      const weekend = weekday === 0 || weekday === 6;
      const closed = event?.status === 'closed' || (weekend && event?.status !== 'open');
      cell.classList.toggle('income-day-closed', closed);
      const label = cell.querySelector('[data-market-label]');
      const full = event?.name || '';
      label.textContent = full.replace('臺灣光復暨金門古寧頭大捷紀念日', '光復紀念日').replace('孔子誕辰紀念日/ 教師節', '教師節').replace('兒童節及民族掃墓節', '兒童／清明');
      label.title = full;
      label.hidden = !full;
      const description = [weekend && event?.status !== 'open' ? '週末' : '', full].filter(Boolean).join('，');
      if (cell.dataset.marketBaseLabel) cell.setAttribute('aria-label', `${cell.dataset.marketBaseLabel}${description ? `，${description}` : ''}`);
      else cell.querySelector('time').setAttribute('aria-label', `${day}${description ? `，${description}` : ''}`);
    });
    message.textContent = `${data.annualMessage} · ${data.noticeMessage}${data.activeJobId ? ' · 背景更新中…' : ''}`;
    updated.textContent = `年度表 ${time(data.annualUpdatedAt)} · 臨時公告 ${time(data.noticeUpdatedAt)}`;
    document.querySelector('[data-market-times]').textContent = updated.textContent;
    retry.hidden = !data.canRetry;
    retry.disabled = Boolean(data.activeJobId);
    const sources = document.querySelector('[data-market-sources]');
    const sourceKey = JSON.stringify(data.events);
    if (sources.dataset.renderKey === sourceKey) return;
    sources.dataset.renderKey = sourceKey;
    const focused = sources.contains(document.activeElement) ? document.activeElement.href : null;
    sources.replaceChildren();
    Object.entries(data.events).filter(([day]) => day.startsWith(month)).sort().forEach(([day, event]) => {
      const li = document.createElement('li');
      const a = document.createElement('a');
      const url = new URL(event.url);
      if (url.origin !== 'https://www.twse.com.tw') return;
      a.href = url.href;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = `${day} ${event.name}${event.needsReview ? '（待確認）' : ''}`;
      li.append(a);
      sources.append(li);
    });
    if (focused) {
      const replacement = [...sources.querySelectorAll('a')].find(a => a.href === focused);
      (replacement || document.querySelector('.income-market-info summary')).focus({preventScroll: true});
    }
  }
  function schedule(delay) {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(check, delay);
  }
  async function check(force = false) {
    if (busy || document.hidden) return;
    busy = true;
    let delay = 60000;
    try {
      const query = new URLSearchParams({year: String(year)});
      if (pending) query.set('requestKey', pending.key);
      const response = await fetch(`/api/v2/market-calendar?${query}`, {cache: 'no-store', signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('無法取得休市資料狀態');
      const data = await response.json();
      if (pending && data.requestedJobId) pending = null;
      apply(data);
      if (data.activeJobId) { delay = 2000; return; }
      if (!data.supported) return;
      if (!data.needsRefresh && !force && !pending) return;
      pending ||= {key: [...crypto.getRandomValues(new Uint32Array(4))].map(n => n.toString(16)).join('-'), force: Boolean(force)};
      const post = await fetch('/api/v2/market-calendar/refresh', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({year, requestKey: pending.key, force: pending.force}),
        signal: AbortSignal.timeout(15000),
      });
      if (!post.ok && post.status !== 409) throw new Error('休市資料更新未能送出');
      pending = null;
      delay = 2000;
    } catch (_) {
      // Keep the same key after an uncertain POST; admission is idempotent.
      message.textContent += ' · 暫時無法更新，稍後重試';
      retry.hidden = false;
      retry.disabled = false;
      delay = 30000;
    } finally {
      busy = false;
      schedule(delay);
    }
  }
  retry.addEventListener('click', () => check(true));
  document.addEventListener('visibilitychange', () => {
    clearTimeout(timer);
    if (!document.hidden) check();
  });
  window.addEventListener('pageshow', () => check());
  check();
})();
