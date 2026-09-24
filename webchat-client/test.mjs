import assert from 'node:assert/strict';

import { createWebchatClient } from './index.js';

// 応答本文は一度だけ受け取り、有効なIDから表示中の選択肢を保存する。
{
  let activeIds = ['last'];
  const messages = [
    { id: 'first', type: 'text', text: '転送前' },
    { id: 'last', type: 'text', text: '転送後' },
  ];
  const activeClient = createWebchatClient({
    apiBaseUrl: 'https://api.example.test', bot: 'active-ids', indexedDB: null,
    fetch: async () => new Response(JSON.stringify({
      schema_version: 1, request_id: 'request',
      state: { id: 'state', revision: 0 }, state_token: 'token',
      messages, active_message_ids: activeIds,
    }), { headers: { 'Content-Type': 'application/json' } }),
  });
  await activeClient.start();
  assert.deepEqual(activeClient.getSnapshot().messages, messages);
  assert.deepEqual(activeClient.getSnapshot().activeResponse, [messages[1]]);
  await activeClient.clearHistory();
  assert.deepEqual(activeClient.getSnapshot().activeResponse, [messages[1]]);
  activeIds = [];
  await activeClient.sendText('解除');
  assert.deepEqual(activeClient.getSnapshot().activeResponse, []);
  activeIds = [42];
  await assert.rejects(activeClient.sendText('不正'), { code: 'invalid-response' });
  activeClient.destroy();
}

// LIFFの状態だけを採用した場合、会話履歴と表示中の選択肢を維持する。
{
  let revision = -1;
  const bodies = [];
  let malformed = false;
  const app = { id: 'menu', url: 'https://pages.example.test/menu' };
  const sessionClient = createWebchatClient({
    apiBaseUrl: 'https://api.example.test', bot: 'liff-session',
    fetch: async (_url, options) => {
      const body = JSON.parse(options.body);
      bodies.push(body);
      const liff = body.input.type === 'liff';
      revision++;
      return new Response(JSON.stringify({
        schema_version: 1, request_id: `request-${revision}`,
        state: { id: `state-${revision}`, revision }, state_token: `token-${revision}`,
        messages: liff ? [] : [{ id: `message-${revision}`, type: 'text', text: '選択してください' }],
        echo_message: body.input.type === 'text' ? body.input.text : null,
        chat_updated: !liff, liff_apps: [app],
        ...(liff ? { liff_result: malformed ? {} : ['{"event":"advance"}'] } : {}),
      }), { headers: { 'Content-Type': 'application/json' } });
    },
  });
  await sessionClient.start();
  const before = sessionClient.getSnapshot();
  assert.deepEqual(await sessionClient.requestLiff(app, 'open'), ['{"event":"advance"}']);
  let current = sessionClient.getSnapshot();
  assert.equal(current.stateId, 'state-1');
  assert.deepEqual(current.turns, before.turns);
  assert.deepEqual(current.activeResponse, before.activeResponse);
  assert.deepEqual(current.liffApps, [app]);
  assert.equal(bodies[1].state_token, 'token-0');
  await sessionClient.sendText('持ち物');
  assert.equal(bodies[2].state_token, 'token-1');
  assert.equal(sessionClient.getSnapshot().turns.length, 2);
  malformed = true;
  await assert.rejects(sessionClient.requestLiff(app, 'open'), { code: 'invalid-response' });
  assert.equal(sessionClient.getSnapshot().stateId, 'state-2');
  assert.equal(sessionClient.getSnapshot().turns.length, 2);
  sessionClient.destroy();
}


let revision = -1;
const requests = [];
const fakeFetch = async (_url, options) => {
  const body = JSON.parse(options.body);
  requests.push(body);
  if (body.input.postback_token === 'silent-failure') {
    return new Response(JSON.stringify({
      code: 'action-not-active',
      title: 'action-not-active',
      status: 409,
    }), {
      status: 409,
      headers: { 'Content-Type': 'application/problem+json' },
    });
  }
  revision += 1;
  const text = body.input.type === 'text' ? body.input.text : null;
  const payload = {
    schema_version: 1,
    request_id: `request-${revision}`,
    state: { id: `state-${revision}`, revision },
    state_token: `token-${revision}`,
    echo_message: text,
    messages: [{
      id: `request-${revision}:0`,
      role: 'assistant',
      sender: null,
      type: 'text',
      text: `response-${revision}`,
    }],
  };
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
};

const client = createWebchatClient({
  apiBaseUrl: 'https://api.example.test',
  bot: 'bot',
  fetch: fakeFetch,
  indexedDB: null,
});

const firstServerSnapshot = client.getServerSnapshot();
assert.equal(firstServerSnapshot, client.getServerSnapshot());

await client.initialize();
assert.equal(client.getSnapshot().persistence, 'memory');
await client.start();
assert.equal(client.getSnapshot().stateId, 'state-0');
assert.equal(requests[0].input.type, 'start');
assert.throws(() => client.getSnapshot().turns.push({}));
assert.throws(() => {
  client.getSnapshot().turns[0].messages[0].text = '改ざん';
});

await client.sendText('こんにちは');
assert.equal(requests[1].state_token, 'token-0');
assert.equal(client.getSnapshot().turns.at(-1).echoMessage, 'こんにちは');
assert.equal(client.getSnapshot().messages.at(-1).text, 'response-1');

const activeBeforeClear = client.getSnapshot().activeResponse;
const callsBeforeClear = requests.length;
await client.clearHistory();
assert.equal(client.getSnapshot().turns.length, 0);
assert.equal(client.getSnapshot().messages.length, 0);
assert.equal(client.getSnapshot().stateId, 'state-1');
assert.deepEqual(client.getSnapshot().activeResponse, activeBeforeClear);
assert.throws(() => {
  client.getSnapshot().activeResponse[0].text = '改ざん';
});
await client.clearHistory();
assert.deepEqual(client.getSnapshot().activeResponse, activeBeforeClear);
assert.equal(requests.length, callsBeforeClear);

await client.reset();
assert.equal(client.getSnapshot().stateRevision, 2);
assert.equal(requests.at(-1).input.type, 'start');

const beforeSilentFailure = client.getSnapshot().stateId;
await assert.rejects(
  client.sendPostback('silent-failure', { silent: true }),
  (error) => error.code === 'action-not-active',
);
assert.equal(client.getSnapshot().status, 'ready');
assert.equal(client.getSnapshot().error, null);
assert.equal(client.getSnapshot().stateId, beforeSilentFailure);

client.destroy();
await client.initialize();
assert.equal(client.getSnapshot().stateRevision, 2);
client.destroy();

let invalidCalls = 0;
const invalidClient = createWebchatClient({
  apiBaseUrl: 'https://api.example.test',
  bot: 'invalid',
  indexedDB: null,
  fetch: async () => {
    invalidCalls += 1;
    return new Response('{"schema_version":1}', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  },
});
await assert.rejects(
  invalidClient.start(),
  (error) => error.code === 'invalid-response',
);
assert.equal(invalidCalls, 1);
assert.equal(invalidClient.getSnapshot().stateId, null);
invalidClient.destroy();

const choiceRequests = [];
const choiceClient = createWebchatClient({
  apiBaseUrl: 'https://api.example.test',
  bot: 'choice',
  fetch: async (_url, options) => {
    choiceRequests.push(JSON.parse(options.body));
    const current = choiceRequests.length;
    return new Response(JSON.stringify({
      schema_version: 1,
      request_id: `choice-request-${current}`,
      state: { id: `choice-state-${current}`, revision: current },
      state_token: `choice-state-token-${current}`,
      echo_message: null,
      messages: [{
        id: `choice-message-${current}`, type: 'button', text: '選択',
        actions: [{ type: 'postback', label: '次へ', token: 'choice-token' }],
      }],
    }), { headers: { 'Content-Type': 'application/json' } });
  },
});
await choiceClient.start();
await choiceClient.clearHistory();
await choiceClient.sendPostback(
  choiceClient.getSnapshot().activeResponse[0].actions[0].token);
assert.deepEqual(choiceRequests[1], {
  state_token: 'choice-state-token-1',
  input: { type: 'postback', postback_token: 'choice-token' },
});
assert.equal(choiceClient.getSnapshot().turns.length, 1);
choiceClient.destroy();

console.log('webchat-client tests: OK');

// 旧メニューのタップではセーブと履歴を維持し、表示だけを更新する。
{
  const richmenu = {id: 'main', revision: 'new', chat_bar_text: 'メニュー', selected: false,
    image_url: 'https://media.example.test/a.png', width: 800, height: 400,
    areas: [{x: 0, y: 0, width: 800, height: 400,
      action: {type: 'menu', label: '開く', echo_text: null}}]};
  const sent = [];
  const menuClient = createWebchatClient({apiBaseUrl: 'https://api.example.test', bot: 'menu-case',
    fetch: async (_url, options) => {
      const body = JSON.parse(options.body); sent.push(body);
      return new Response(JSON.stringify({schema_version: 1, request_id: 'request',
        state: {id: 'state', revision: 0}, state_token: 'token', echo_message: null,
        messages: [], richmenu, ...(body.input.type === 'menu' ? {chat_updated: false, menu_updated: true} : {})}),
      {headers: {'Content-Type': 'application/json'}});
    }});
  await menuClient.start();
  const before = menuClient.getSnapshot();
  await menuClient.sendMenu('main', 0, 'old');
  assert.deepEqual(sent[1].input, {type: 'menu', menu: 'main', area: 0, revision: 'old'});
  assert.deepEqual(menuClient.getSnapshot().turns, before.turns);
  assert.equal(menuClient.getSnapshot().stateId, before.stateId);
  assert.equal(menuClient.getSnapshot().richmenu.revision, 'new');
  assert.match(menuClient.getSnapshot().notice, /メニューが更新/);
  await menuClient.clearHistory();
  assert.equal(menuClient.getSnapshot().richmenu.id, 'main');
  menuClient.destroy();
}
