import assert from 'node:assert/strict';

import { createWebchatClient } from './index.js';


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

await client.clearHistory();
assert.equal(client.getSnapshot().turns.length, 0);
assert.equal(client.getSnapshot().stateId, 'state-1');

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

console.log('webchat-client tests: OK');
