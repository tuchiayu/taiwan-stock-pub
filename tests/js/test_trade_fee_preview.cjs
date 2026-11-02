const assert = require('node:assert/strict');
const {test} = require('node:test');
const {selectSecurity, latestQuote, enhanceTradeForm} = require('../../src/taiwan_stock/static/trade-fee-preview.js');
const option = (market, id, status='active') => ({value:`${market}:00679B`,dataset:{code:'00679B',securityId:id,securityType:'etf',status}});
const response = data => ({ok:true,json:async()=>data});
const deferred = () => {let resolve,reject; const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};

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
  return {elements,reset,hints,querySelector(selector) {
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
  assert.equal(form.elements['executed-amount'].value,'20000');
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
