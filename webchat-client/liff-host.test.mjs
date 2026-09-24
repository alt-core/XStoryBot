import assert from 'node:assert/strict';
import { test } from 'node:test';
import { attachLiffFrame, findLiffApp, resolveLiffLink } from './liff-host.js';

const childOrigin = 'https://pages.example.test';
const app = { id: 'menu', url: `${childOrigin}/menu/` };
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};

// ページ側SDKを使わず、公開protocolのmessageで親の動作を確認する。
function host(callbacks = {}) {
  const original = globalThis.window;
  const window = new EventTarget();
  const replies = [];
  const calls = [];
  const page = { postMessage(data, origin) {
    assert.equal(origin, childOrigin);
    replies.push(structuredClone(data));
  } };
  globalThis.window = window;
  let connection;
  connection = attachLiffFrame({ contentWindow: page }, {
    app,
    request: async action => { calls.push(['request', action]); return callbacks.request ? callbacks.request(action) : [action]; },
    sendText: async text => { calls.push(['text', text]); },
    close: () => { calls.push(['close']); connection.destroy(); },
  });
  const send = (data, origin = childOrigin, source = page) => {
    const event = new Event('message');
    Object.assign(event, { data: { protocol: 'xstorybot-liff', version: 1, ...data }, origin, source });
    window.dispatchEvent(event);
  };
  let sequence = 0;
  return {
    replies, calls, send, connection,
    connect() {
      send({ type: 'connect', id: `connect-${++sequence}` });
      return replies.at(-1).session;
    },
    cleanup() { connection.destroy(); globalThis.window = original; },
  };
}

test('親は接続・action・発話・closeを処理し、ページへセーブを渡さない', async () => {
  const h = host();
  try {
    const session = h.connect();
    assert.equal(typeof session, 'string');
    h.send({ type: 'invoke', id: 'a', session, method: 'request', args: { action: ' open\n' } });
    await flush();
    assert.deepEqual(h.replies.at(-1).value, [' open\n']);
    h.send({ type: 'invoke', id: 'b', session, method: 'sendText', args: { text: ' 持ち物\n' } });
    await flush();
    assert.equal(h.replies.at(-1).value, null);
    assert.deepEqual(h.calls, [['request', ' open\n'], ['text', ' 持ち物\n']]);
    const wire = JSON.stringify(h.replies);
    for (const key of ['state_token', 'stateId', 'baseStateId', 'bot']) assert.equal(wire.includes(`"${key}"`), false);
    h.send({ type: 'close', session });
    assert.deepEqual(h.calls.at(-1), ['close']);
  } finally { h.cleanup(); }
});

test('別origin・別frame・旧session・異なるprotocolを拒否する', async () => {
  const h = host();
  try {
    const session = h.connect();
    const message = { type: 'invoke', id: 'a', session, method: 'request', args: { action: 'x' } };
    h.send(message, 'https://other.example.test');
    h.send(message, childOrigin, {});
    h.send({ ...message, session: 'old' });
    h.send({ ...message, version: 2 });
    h.send({ ...message, protocol: 'other' });
    await flush();
    assert.equal(h.calls.length, 0);
    h.send({ ...message, method: 'setState' });
    await flush();
    assert.equal(h.replies.at(-1).error.code, 'invalid-input');
    assert.equal(h.calls.length, 0);
  } finally { h.cleanup(); }
});

test('同じ接続の重複は未実行のbusyにし、再接続では前の操作の完了を待つ', async () => {
  for (const disconnect of [false, true]) {
    const pending = deferred();
    const h = host({ request: action => action === 'wait' ? pending.promise : [action] });
    try {
      const first = h.connect();
      h.send({ type: 'invoke', id: 'old', session: first, method: 'request', args: { action: 'wait' } });
      h.send({ type: 'invoke', id: 'double', session: first, method: 'request', args: { action: 'next' } });
      assert.equal(h.replies.at(-1).error.code, 'busy');
      assert.deepEqual(h.calls, [['request', 'wait']]);
      if (disconnect) h.send({ type: 'disconnect', session: first });
      h.send({ type: 'connect', id: 'second' });
      await flush();
      assert.equal(h.replies.some(r => r.id === 'second'), false);
      pending.resolve(['old-result']);
      await flush();
      assert.equal(h.replies.some(r => r.id === 'old'), false);
      const connected = h.replies.at(-1);
      assert.equal(connected.type, 'connected');
      assert.equal(connected.id, 'second');
      assert.notEqual(first, connected.session);
      h.send({ type: 'invoke', id: 'stale', session: first, method: 'request', args: { action: 'stale' } });
      h.send({ type: 'invoke', id: 'next', session: connected.session, method: 'request', args: { action: 'next' } });
      await flush();
      assert.deepEqual(h.replies.at(-1).value, ['next']);
      assert.deepEqual(h.calls, [['request', 'wait'], ['request', 'next']]);
    } finally { h.cleanup(); }
  }
});

test('待機中の接続は最新一件だけを残し、操作が失敗した場合も接続を成立させる', async () => {
  for (const failed of [false, true]) {
    const pending = deferred();
    const h = host({ request: async () => {
      await pending.promise;
      if (failed) throw { code: 'turn-timeout' };
      return ['old-result'];
    } });
    try {
      h.send({ type: 'invoke', id: 'old', session: h.connect(), method: 'request', args: { action: 'wait' } });
      h.send({ type: 'connect', id: 'second' });
      h.send({ type: 'connect', id: 'latest' });
      pending.resolve();
      await flush();
      assert.equal(h.replies.some(r => r.id === 'second' || r.id === 'old'), false);
      assert.equal(h.replies.at(-1).id, 'latest');
      assert.equal(h.replies.at(-1).type, 'connected');
      assert.equal(h.replies.length, 2);
      assert.deepEqual(h.calls, [['request', 'wait']]);
    } finally { h.cleanup(); }
  }
});

test('破棄したframeへ処理の後着結果を送らない', async () => {
  const pending = deferred();
  const h = host({ request: () => pending.promise });
  try {
    const session = h.connect();
    h.send({ type: 'invoke', id: 'pending', session, method: 'request', args: { action: 'wait' } });
    h.send({ type: 'connect', id: 'next-page' });
    h.connection.destroy();
    pending.resolve(['done']);
    await flush();
    assert.equal(h.calls.length, 1);
    assert.equal(h.replies.length, 1);
  } finally { h.cleanup(); }
});

test('親の失敗を公開errorへ変換し、例外の内部情報を渡さない', async () => {
  for (const [code, mapped] of [['request-in-flight', 'busy'], ['state-refreshed', 'state-refreshed'],
    ['turn-timeout', 'outcome-unknown'], ['invalid-state', 'unavailable'], ['other', 'request-failed']]) {
    const h = host({ request: () => { throw Object.assign(new Error('private-detail'), { code, requestId: 'request-1' }); } });
    try {
      h.send({ type: 'invoke', id: 'a', session: h.connect(), method: 'request', args: { action: 'x' } });
      await flush();
      assert.equal(h.replies.at(-1).error.code, mapped);
      assert.equal(h.replies.at(-1).error.requestId, 'request-1');
      assert.equal(JSON.stringify(h.replies).includes('private-detail'), false);
    } finally { h.cleanup(); }
  }
  const h = host({ request: () => ({ invalid: true }) });
  try {
    h.send({ type: 'invoke', id: 'a', session: h.connect(), method: 'request', args: { action: 'x' } });
    await flush();
    assert.equal(h.replies.at(-1).error.code, 'outcome-unknown');
  } finally { h.cleanup(); }
});

test('登録ページはorigin・pathと境界で選び、LIFF URLの追加情報を維持する', () => {
  assert.equal(findLiffApp(`${app.url}?item=1#tab`, [app]), app);
  assert.equal(findLiffApp(`${childOrigin}/another/`, [app]), null);
  assert.equal(findLiffApp('javascript:alert(1)', [app]), null);
  const root = { ...app, url: childOrigin + '/app/?base=1', liff_id: '123-room', match: 'prefix' };
  const nested = { id: 'words', url: childOrigin + '/app/words/', match: 'prefix' };
  assert.equal(resolveLiffLink('https://liff.line.me/123-room/words/?id=2#item', [root]).url,
    childOrigin + '/app/words/?base=1&id=2#item');
  assert.equal(findLiffApp(childOrigin + '/app/words/item', [root, nested]), nested);
  assert.equal(findLiffApp(childOrigin + '/application/', [root]), null);
  assert.equal(findLiffApp('https://liff.line.me/123-room-other/words/', [root]), null);
  assert.equal(findLiffApp('https://liff.line.me/123-room/words/', [{ ...root, match: 'exact' }]), null);
  assert.equal(resolveLiffLink('https://liff.line.me/123-room', [root]).url, root.url);
  assert.equal(findLiffApp('https://other.example.test/app/', [root]), null);
  assert.equal(findLiffApp(childOrigin + '/app/../outside/', [root]), null);
});
