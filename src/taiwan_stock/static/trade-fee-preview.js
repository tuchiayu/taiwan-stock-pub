// The backend owns Decimal arithmetic, product classification and dated tax rules.
function selectSecurity(value, options) {
  const text = value.trim();
  if (text.includes(':')) return options.find(o => o.value.toUpperCase() === text.toUpperCase());
  const matches = options.filter(o => o.dataset.code === text);
  const active = matches.filter(o => o.dataset.status === 'active');
  return active.length === 1 ? active[0] : matches.length === 1 ? matches[0] : null;
}

function latestQuote(fetcher, receive, failed) {
  let sequence = 0, controller;
  return {
    invalidate() { sequence++; controller?.abort(); },
    async request(payload) {
      const mine = ++sequence;
      controller?.abort();
      controller = new AbortController();
      try {
        const response = await fetcher('/api/v2/trade-fee-quotes', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload), signal: controller.signal
        });
        if (!response.ok) throw new Error('quote unavailable');
        const result = await response.json();
        if (mine === sequence) receive(result);
      } catch (error) {
        if (mine === sequence) failed(error);
      }
    }
  };
}

function enhanceTradeForm(form) {
  const find = name => form.querySelector(`[data-trade-${name}]`);
  const code = find('security-code'), type = find('event-type');
  const account = find('account'), date = find('date');
  const qty = find('quantity'), price = find('unit-price'), amount = find('executed-amount');
  const commission = find('commission'), tax = find('transaction-tax'), other = find('other-fees');
  const dayTrade = find('day-trade-checkbox'), output = find('preview');
  const fees = [commission, tax], reset = form.querySelector('[data-reset-fees]');
  const options = Array.from(form.querySelector('datalist').options);
  const nullable = value => value.trim() === '' ? null : value;
  let timer, previousType = type.value;
  reset.hidden = false;
  function hide(container, hidden) {
    container.hidden = hidden;
    container.querySelectorAll('input').forEach(field => { field.disabled = hidden; });
  }
  function clearPreview(message) {
    output.textContent = message;
    fees.forEach(field => {
      form.querySelector(`[data-fee-status="${field.name}"]`).textContent =
        field.value !== '' ? '已使用手動費用' : '自動試算：尚未取得報價';
    });
    if (amount.readOnly) amount.value = '';
  }
  const quotes = latestQuote(window.fetch.bind(window), result => {
    if (amount.readOnly) amount.value = result.executedAmount ?? '';
    fees.forEach((field, index) => {
      const estimate = index ? result.transactionTax : result.commission;
      form.querySelector(`[data-fee-status="${field.name}"]`).textContent = field.value !== ''
        ? '已使用手動費用' : `自動試算：${estimate ?? '尚無金額'}${estimate === null ? '' : ' 元'}`;
    });
    const label = type.value === 'opening' ? '期初持股總成本' : type.value === 'sell' ? '預估實收' : '預估付款';
    output.textContent = (result.netAmount === null ? '金額尚未完整' : `${label} NT$ ${result.netAmount}`)
      + '；儲存時以系統計算為準。' + [...(result.warnings || []), ...(result.notices || [])].join(' ');
  }, () => clearPreview('暫時無法取得試算；人工費用已保留，儲存時由系統重新計算。'));
  function update() {
    clearTimeout(timer);
    quotes.invalidate();
    const opening = type.value === 'opening', sell = type.value === 'sell';
    if (opening && previousType !== 'opening' && amount.readOnly) amount.value = '';
    previousType = type.value;
    const security = selectSecurity(code.value, options);
    hide(find('price-field'), opening);
    hide(find('fees'), opening);
    hide(find('tax-field'), !sell);
    hide(find('day-trade-field'), !sell || security?.dataset.securityType !== 'stock');
    if (!sell || security?.dataset.securityType !== 'stock') dayTrade.checked = false;
    find('date-label').textContent = opening ? '取得日期' : '交易日期';
    find('amount-label').textContent = opening ? '期初持股總成本（元，未知可留空）' : '成交金額（元）';
    const wasAuto = amount.readOnly;
    amount.readOnly = !opening && price.value.trim() !== '';
    if (wasAuto && !amount.readOnly && !opening) amount.value = '';
    clearPreview('正在取得試算；儲存時以系統計算為準。');
    if (!security || !account.value || !date.value || !/^\d+$/.test(qty.value) || Number(qty.value) <= 0) {
      clearPreview('請提供帳戶、明確商品、日期與股數後試算。');
      return;
    }
    const payload = {
      brokerAccountId: account.value, securityId: security.dataset.securityId,
      eventType: type.value, tradeDate: date.value, quantity: Number(qty.value),
      unitPrice: opening ? null : nullable(price.value),
      executedAmount: amount.readOnly ? null : nullable(amount.value),
      commission: opening ? null : nullable(commission.value),
      transactionTax: sell ? nullable(tax.value) : null,
      otherFees: opening ? null : nullable(other.value), isDayTrade: sell && dayTrade.checked
    };
    timer = setTimeout(() => quotes.request(payload), 180);
  }
  [code, account, date, type, qty, price, amount, commission, tax, other, dayTrade].forEach(field => {
    field.addEventListener('input', update);
    field.addEventListener('change', update);
  });
  reset.addEventListener('click', () => { fees.forEach(field => { field.value = ''; }); update(); });
  update();
}

if (typeof document !== 'undefined') document.querySelectorAll('[data-trade-form]').forEach(enhanceTradeForm);
if (typeof module !== 'undefined') module.exports = {selectSecurity, latestQuote, enhanceTradeForm};
