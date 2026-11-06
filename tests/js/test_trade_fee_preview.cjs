const assert = require('node:assert/strict');
const {test} = require('node:test');
const {groupDigits, selectSecurity, latestQuote, enhanceTradeForm} = require('../../src/taiwan_stock/static/trade-fee-preview.js');
const option = (market, id, status='active') => ({value:`${market}:00679B`,dataset:{code:'00679B',securityId:id,securityType:'etf',status}});
const response = data => ({ok:true,json:async()=>data});
const deferred = () => {let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

test('groupDigits groups Decimal strings without changing precision',()=>{
  assert.equal(groupDigits('684974'),'684,974');
  assert.equal(groupDigits('974'),'974');
  assert.equal(groupDigits('-1234.5'),'-1,234.5');
  assert.equal(groupDigits('1234567.8900'),'1,234,567.8900');
  assert.equal(groupDigits('abc'),'abc');
});

test('preview and fee estimates are grouped; executed amount field stays raw',async()=>{
  const form=fakeForm();
  form.elements.commission.value='';
  global.window={fetch:async()=>response({executedAmount:'684000',commission:'1234',transactionTax:null,netAmount:'684974',warnings:[]})};
  enhanceTradeForm(form);await settled();
  assert.match(form.elements.preview.textContent,/NT\$ 684,974；/);
  assert.equal(form.hints.commission.textContent,'自動試算：1,234 元');
  assert.equal(form.hints.transaction_tax.textContent,'已使用手動費用');
  assert.equal(form.elements['executed-amount'].value,'684,000');
});

test('numeric fields group on load and blur, ungroup on focus, payload and submit stay plain',async()=>{
  const form=fakeForm(),calls=[];
  form.elements.quantity.value='20000';form.elements['unit-price'].value='1234.5';
  global.window={fetch:async(url,options)=>{calls.push(JSON.parse(options.body));return response({executedAmount:'24690000',commission:'7',transactionTax:'3',netAmount:'24689990',warnings:[]});}};
  enhanceTradeForm(form);await settled();
  const qty=form.elements.quantity,price=form.elements['unit-price'],amount=form.elements['executed-amount'];
  assert.equal(qty.value,'20,000');assert.equal(price.value,'1,234.5');
  assert.equal(calls.at(-1).quantity,20000);assert.equal(calls.at(-1).unitPrice,'1234.5');
  assert.equal(amount.readOnly,true);assert.equal(amount.value,'24,690,000');
  qty.emit('focus');assert.equal(qty.value,'20000');
  qty.value='2000';qty.emit('input');qty.emit('blur');await settled();
  assert.equal(qty.value,'2,000');assert.equal(calls.at(-1).quantity,2000);
  amount.emit('focus');assert.equal(amount.value,'24,690,000');
  form.elements['other-fees'].value='1,500';form.elements['other-fees'].emit('input');await settled();
  assert.equal(calls.at(-1).otherFees,'1500');
  form.emit('submit');
  assert.deepEqual([qty.value,price.value,amount.value,form.elements['other-fees'].value],['2000','1234.5','24690000','1500']);
});

test('exact market identity and ambiguous bare codes',()=>{
  const a=option('TWSE','a'),b=option('TPEx','b');
  assert.equal(selectSecurity('00679B',[a,b]),null);
  assert.equal(selectSecurity('tpex:00679B',[a,b]),b);
  b.dataset.status='delisted';
  assert.equal(selectSecurity('00679B',[a,b]),a);
  assert.equal(selectSecurity('unknown',[a,b]),null);
});

test('out-of-order response and failure cannot overwrite newest quote',async()=>{
  const pending=[],seen=[],errors=[];
  const quote=latestQuote(()=>{const p=deferred();pending.push(p);return p.promise;},x=>seen.push(x),e=>errors.push(e));
  const old=quote.request({tradeDate:'2026-12-31'}),current=quote.request({tradeDate:'2027-01-01'});
  pending[1].resolve(response({transactionTax:'20'})); await current;
  pending[0].resolve(response({transactionTax:'0'})); await old;
  assert.deepEqual(seen,[{transactionTax:'20'}]);
  const abandoned=quote.request({});quote.invalidate();pending[2].reject(new Error('offline'));await abandoned;
  assert.equal(errors.length,0);
  const failed=quote.request({});pending[3].reject(new Error('offline'));await failed;
  assert.equal(errors.length,1);
});

class Element {
  constructor(value='',name='') {this.value=value;this.name=name;this.events={};this.dataset={};this.readOnly=false;this.checked=false;this.textContent='';}
  addEventListener(event,handler) {(this.events[event]??=[]).push(handler);}
  emit(event='input') {(this.events[event]||[]).forEach(fn=>fn());}
  querySelectorAll() {return [];}
}
function fakeForm() {
  const names=['security-code','event-type','account','date','quantity','unit-price','executed-amount','commission','transaction-tax','other-fees','day-trade-checkbox','preview','price-field','fees','tax-field','day-trade-field','date-label','amount-label'];
  const elements=Object.fromEntries(names.map(n=>[n,new Element('',n.replaceAll('-','_'))]));
  for (const [name,value] of Object.entries({'security-code':'TWSE:00679B','event-type':'sell',account:'a',date:'2026-12-31',quantity:'1000','unit-price':'20',commission:'7','transaction-tax':'3'})) elements[name].value=value;
  const reset=new Element(),hints={commission:new Element(),transaction_tax:new Element()};
  const events={};
  return {elements,reset,hints,
    addEventListener(event,handler) {(events[event]??=[]).push(handler);},
    emit(event) {(events[event]||[]).forEach(fn=>fn());},
    querySelector(selector) {
    if(selector==='datalist') return {options:[option('TWSE','s')]};
    if(selector==='[data-reset-fees]') return reset;
    const fee=selector.match(/data-fee-status="([^"]+)"/);if(fee)return hints[fee[1]];
    return elements[selector.match(/data-trade-([^\]]+)/)[1]];
  }};
}
const settled=()=>new Promise(resolve=>setTimeout(resolve,240));
test('date changes request backend; reset preserves other fees; offline clears stale automatic amount',async()=>{
  const form=fakeForm(),calls=[];let offline=false;
  global.window={fetch:async(url,options)=>{
    calls.push(JSON.parse(options.body));if(offline)throw new Error('offline');
    return response({executedAmount:'20000',commission:'7',transactionTax:'3',netAmount:'19990',warnings:[]});
  }};
  enhanceTradeForm(form);await settled();
  assert.equal(calls[0].securityId,'s');assert.equal(calls[0].commission,'7');
  assert.equal(form.elements['executed-amount'].value,'20,000');
  form.elements.date.value='2027-01-01';form.elements.date.emit();await settled();
  assert.equal(calls.at(-1).tradeDate,'2027-01-01');
  form.elements['other-fees'].value='9';form.reset.emit('click');await settled();
  assert.equal(calls.at(-1).commission,null);assert.equal(calls.at(-1).transactionTax,null);assert.equal(calls.at(-1).otherFees,'9');
  form.elements.commission.value='11';offline=true;form.elements.commission.emit();await settled();
  assert.equal(form.elements.commission.value,'11');
  assert.equal(form.elements['executed-amount'].value,'');
  assert.match(form.elements.preview.textContent,/暫時無法/);
});

test('preview appends warnings then expiry notices',async()=>{
  const form=fakeForm();
  global.window={fetch:async()=>response({executedAmount:'20000',commission:'7',transactionTax:'3',netAmount:'19990',warnings:['W'],notices:['N']})};
  enhanceTradeForm(form);await settled();
  assert.match(form.elements.preview.textContent,/儲存時以系統計算為準。W N$/);
});
