// 親Webchatのprotocolを確認するページ。アプリケーション用の接続SDKではない。
const protocol = 'xstorybot-liff';
const parentOrigin = document.documentElement.dataset.parentOrigin || location.origin;
const notice = document.querySelector('#notice');
const events = document.querySelector('#events');
const send = document.querySelector('#send');
const close = document.querySelector('#close');
const operations = [...document.querySelectorAll('[data-action]'), send];
let session = null;
let pending = null;
let sequence = 0;
let stopped = false;
const setBusy = (busy) => operations.forEach(button => { button.disabled = busy; });
const post = (type, values = {}) => parent.postMessage({ protocol, version: 1, type, session, ...values }, parentOrigin);
const fail = (message) => {
  clearTimeout(pending?.timer);
  pending = null;
  stopped = true;
  setBusy(true);
  notice.textContent = `${message} 操作を繰り返さず、閉じて開き直してください。`;
};
const invoke = (method, args = {}) => {
  if (pending || stopped) return;
  setBusy(true);
  const id = String(++sequence);
  pending = { id, method, timer: setTimeout(() => fail('応答を確認できませんでした。'), method === 'connect' ? 10000 : 60000) };
  try { post(method === 'connect' ? 'connect' : 'invoke', { id, method, args }); }
  catch (_error) { fail('親画面へ送信できませんでした。'); }
};
window.addEventListener('message', event => {
  const data = event.data;
  if (stopped || !pending || event.source !== parent || event.origin !== parentOrigin
      || data?.protocol !== protocol || data?.version !== 1 || data.id !== pending.id) return;
  if (pending.method === 'connect') {
    if (data.type !== 'connected' || typeof data.session !== 'string' || !data.session) return;
    clearTimeout(pending.timer);
    pending = null;
    session = data.session;
    close.disabled = false;
    notice.textContent = '';
    invoke('request', { action: 'open' });
    return;
  }
  if (data.type !== 'result' || data.session !== session) return;
  if (data.ok === false) { fail(data.error?.message || '処理に失敗しました。'); return; }
  if (data.ok !== true || (pending.method === 'request' ? !Array.isArray(data.value) : data.value !== null)) {
    fail('応答形式が不正です。'); return;
  }
  clearTimeout(pending.timer);
  const method = pending.method;
  pending = null;
  if (method === 'sendText') { post('close'); stopped = true; }
  else { events.textContent = JSON.stringify(data.value, null, 2); setBusy(false); }
});
for (const button of document.querySelectorAll('[data-action]')) {
  button.addEventListener('click', () => invoke('request', { action: button.dataset.action }));
}
send.addEventListener('click', () => invoke('sendText', { text: document.querySelector('#text').value }));
close.addEventListener('click', () => { clearTimeout(pending?.timer); stopped = true; post('close'); });
if (parent === window || new URL(location.href).searchParams.get('xsb_client') !== 'webchat') {
  fail('登録したWebchatのリンクから開いてください。');
} else {
  invoke('connect');
}
