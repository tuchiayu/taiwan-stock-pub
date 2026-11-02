/* Server state is authoritative across tabs, reloads, CLI, and Web restarts. */
(() => {
  const initial = JSON.parse(document.getElementById('sync-initial').textContent);
  const forms = [...document.querySelectorAll('[data-sync-operation]')];
  const panel = document.getElementById('sync-status');
  const messages = panel.querySelector('[data-sync-messages]');
  const active = (job) => ['queued', 'running'].includes(job.status);
  const labels = {queued: '準備中', running: '更新中', success: '完成', partial: '部分成功', failed: '失敗', interrupted: '中斷'};
  let snapshot = initial.snapshot;
  let selected = initial.selected;
  let calibratedAt = performance.now();
  let serverTime = Date.parse(snapshot.server_now);
  let disconnected = false;
  let submitting = null;
  let posting = false;
  let polling = false;
  let pollTimer = null;
  let pollRequested = false;
  let generation = 0;
  let renderKey = null;
  const clocks = new Map();
  let selectedId = new URL(location.href).searchParams.get('job_id') || document.querySelector('[data-sync-detail]')?.dataset.syncDetail;
  const elapsed = (job) => {
    const end = job.finished_at ? Date.parse(job.finished_at) : serverTime + performance.now() - calibratedAt;
    const seconds = Math.max(0, Math.floor((end - Date.parse(job.started_at || job.submitted_at)) / 1000));
    return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  };
  function render() {
    const jobs = [...snapshot.jobs];
    if (selected && !jobs.some((job) => job.id === selected.id)) jobs.push(selected);
    panel.hidden = !jobs.length && !snapshot.legacy_sources.length && !disconnected;
    const nextKey = JSON.stringify({
      jobs: jobs.map((j) => [j.id, j.status, j.phase, j.processed, j.total, j.failed_units, j.error, j.summary]),
      legacy: snapshot.legacy_sources, disconnected,
    });
    if (nextKey !== renderKey) {
      renderKey = nextKey;
      clocks.clear();
      messages.replaceChildren();
      for (const job of jobs) {
        const p = document.createElement('p');
        const link = document.createElement('a');
        link.href = `/sync-jobs/${encodeURIComponent(job.id)}`;
        const timer = document.createElement('span');
        timer.setAttribute('role', 'timer');
        timer.setAttribute('aria-live', 'off');
        clocks.set(job.id, timer);
        link.append(`${labels[job.status]} `, timer, ` · ${job.phase} · 已處理 ${job.processed}${job.total == null ? '' : `/${job.total}`}${job.failed_units ? ` · 失敗 ${job.failed_units}` : ''}`);
        p.append(link);
        if (job.error) p.append(` · ${job.error}`);
        if (job.summary) p.append(` · ${job.summary}`);
        messages.append(p);
      }
      for (const text of [snapshot.legacy_sources.length ? '舊版同步紀錄仍在執行中，請確認程序已停止後離線恢復。' : '', disconnected ? '暫時無法取得進度，正在重新連線…' : '']) {
        if (text) { const p = document.createElement('p'); p.textContent = text; messages.append(p); }
      }
    }
    for (const job of jobs) clocks.get(job.id).textContent = elapsed(job);
    for (const form of forms) {
      const button = form.querySelector('button[type="submit"]');
      const operation = form.dataset.syncOperation;
      const own = jobs.find((job) => active(job) && job.operation === operation);
      const blocked = snapshot.blocked_operations.includes(operation);
      const busy = submitting === form || Boolean(own);
      const label = button.querySelector('[data-sync-label-text]');
      const hint = form.querySelector('[data-sync-hint]');
      const control = form.querySelector('[data-sync-control]');
      let text = button.dataset.syncLabel;
      let reason = '';
      let tooltip = control.dataset.syncTitle;
      if (disconnected) {
        text = '確認同步狀態…';
        reason = '暫時無法確認狀態，正在重新連線。';
      } else if (submitting === form) {
        text = '送出中…';
      } else if (own) {
        text = own.status === 'queued' ? '準備同步…' : '同步中…';
        reason = '進度與結果見上方同步進度。';
      } else if (blocked) {
        text = '其他同步中…';
        reason = '其他同步正在執行，請查看上方同步進度。';
        tooltip = '其他同步正在執行，完成後即可同步';
      }
      label.textContent = text;
      if (tooltip) control.title = tooltip;
      else control.removeAttribute('title');
      hint.textContent = reason;
      hint.hidden = !reason;
      button.disabled = disconnected || busy || blocked;
      form.setAttribute('aria-busy', String(busy));
    }
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    const busy = submitting || disconnected || snapshot.jobs.some(active) || (selected && active(selected));
    pollTimer = setTimeout(poll, document.hidden ? 30000 : busy ? 2000 : 15000);
  }
  async function poll() {
    clearTimeout(pollTimer);
    if (polling) { pollRequested = true; return; }
    polling = true;
    pollRequested = false;
    const requestGeneration = generation;
    // Only a lookup begun after the POST settled can resolve uncertainty.
    const recoveringSubmission = Boolean(submitting && !posting);
    try {
      const lookup = submitting ? `&request_key=${encodeURIComponent(submitting.querySelector('[name="request_key"]').value)}` : '';
      const response = await fetch(`/api/v2/sync-jobs?active=true${lookup}`, {cache: 'no-store', signal: AbortSignal.timeout(8000)});
      if (!response.ok) throw new Error('status unavailable');
      const next = await response.json();
      if (requestGeneration !== generation) return;
      if (recoveringSubmission) {
        const key = submitting.querySelector('[name="request_key"]').value;
        const accepted = next.requested_job || next.jobs.find((job) => job.request_key === key);
        if (accepted) {
          selectedId = accepted.id;
          const url = new URL(location.href);
          url.searchParams.set('job_id', accepted.id);
          history.replaceState(null, '', url);
        }
        next.jobs = next.jobs.filter(active);
        submitting = null;
      }
      let nextSelected = selected;
      // Terminal results stay visible without fetching the same detail forever.
      // An active snapshot can invalidate a cached result after a restore.
      if (selectedId && (!selected || selected.id !== selectedId || active(selected) || next.jobs.some((job) => job.id === selectedId && active(job)))) {
        const detail = await fetch(`/api/v2/sync-jobs/${encodeURIComponent(selectedId)}`, {cache: 'no-store', signal: AbortSignal.timeout(8000)});
        if (detail.status === 404) {
          selectedId = null;
          nextSelected = null;
          const url = new URL(location.href);
          url.searchParams.delete('job_id');
          url.searchParams.delete('job_done');
          history.replaceState(null, '', url);
        } else {
          if (!detail.ok) throw new Error('status unavailable');
          nextSelected = await detail.json();
        }
      }
      // A response begun before submission must never unlock its button.
      if (requestGeneration !== generation) return;
      // The calendar updates its annotations in place, preserving open dialogs.
      const completed = snapshot.jobs.find((old) => old.operation !== 'market-calendar' && !next.jobs.some((job) => job.id === old.id));
      snapshot = next;
      selected = nextSelected;
      calibratedAt = performance.now();
      serverTime = Date.parse(next.server_now);
      disconnected = false;
      const url = new URL(location.href);
      const completedId = selected && !active(selected) ? selected.id : completed?.id;
      if (completedId && url.searchParams.get('job_done') !== completedId) {
        url.searchParams.set('job_done', completedId);
        if (!selectedId) url.searchParams.set('job_id', completedId);
        location.replace(url);
        return;
      }
    } catch (_) { disconnected = true; }
    finally {
      polling = false;
      render();
      if (pollRequested) poll();
      else schedulePoll();
    }
  }
  forms.forEach((form) => form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (submitting || disconnected || snapshot.blocked_operations.includes(form.dataset.syncOperation)) return;
    submitting = form;
    posting = true;
    generation += 1;
    render();
    schedulePoll();
    const key = form.querySelector('[name="request_key"]').value;
    try {
      const response = await fetch('/api/v2/sync-jobs', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({operation: form.dataset.syncOperation, request_key: key}), signal: AbortSignal.timeout(15000),
      });
      const body = await response.json();
      const id = response.ok ? body.id : body.detail?.active_job_id;
      if (!id) throw new Error(body.detail?.message || '無法確認提交結果');
      const url = new URL(location.href);
      url.searchParams.set('job_id', id);
      url.searchParams.delete('job_done');
      location.assign(url);
    } catch (_) {
      // An uncertain POST can already have started a job. Keep its idempotency
      // key and button locked; recovery polling never resubmits the operation.
      disconnected = true;
      render();
    } finally {
      posting = false;
      schedulePoll();
    }
  }));
  window.addEventListener('pageshow', () => { disconnected = true; render(); poll(); });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) schedulePoll();
    else { disconnected = true; render(); poll(); }
  });
  setInterval(render, 1000);
  render();
  poll();
})();
