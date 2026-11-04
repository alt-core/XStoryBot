import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createWebchatClient } from './index.js';
import { createTrialClient } from './trial.js';

const program = JSON.parse(readFileSync(new URL('../tests/fixtures/trial/program.json', import.meta.url), 'utf8'));
const app = { id: 'menu', url: program.liff_apps.menu.url };

for (const mode of ['api', 'trial']) {
  test(`${mode}: 名前付き入口は初回だけ使い、未知入口・履歴削除・resetを区別する`, async () => {
    const data = structuredClone(program);
    data.start_entries = { help: { action: '#help', echo: '説明を読む' } };
    const calls = [];
    const options = { bot: data.bot, storage: 'memory' };
    const client = mode === 'trial'
      ? createTrialClient({ ...options, programUrl: 'https://static.example.test/scenario.json',
        fetch: async () => new Response(JSON.stringify(data)) })
      : createWebchatClient({ ...options, apiBaseUrl: 'https://api.example.test',
        fetch: async (_url, request) => {
          const body = JSON.parse(request.body);
          calls.push(body);
          const named = Object.hasOwn(body.input, 'entry');
          if (named && body.input.entry !== 'help') return new Response(JSON.stringify({
            code: 'invalid-entry', title: 'invalid-entry',
          }), { status: 400, headers: { 'Content-Type': 'application/problem+json' } });
          const id = String(calls.length);
          return new Response(JSON.stringify({ schema_version: 1, request_id: id,
            state: { id, revision: 0 }, state_token: id,
            echo_message: named ? '説明を読む' : null,
            messages: [{ id, type: 'text', text: named ? '説明です' : '入口です' }],
          }), { headers: { 'Content-Type': 'application/json' } });
        } });
    try {
      await assert.rejects(client.start({ entry: 'missing' }), { code: 'invalid-entry' });
      assert.equal(client.getSnapshot().stateId, null);
      assert.deepEqual(client.getSnapshot().turns, []);
      if (mode === 'api') assert.equal(calls.length, 1);
      await client.start({ entry: 'help' });
      assert.equal(client.getSnapshot().turns[0].echoMessage, '説明を読む');
      assert.equal(client.getSnapshot().messages[0].text, '説明です');
      const state = client.getSnapshot().stateId;
      const before = calls.length;
      await client.start({ entry: 'missing' });
      assert.equal(client.getSnapshot().stateId, state);
      await client.clearHistory();
      await client.start({ entry: 'help' });
      assert.equal(client.getSnapshot().stateId, state);
      assert.deepEqual(client.getSnapshot().turns, []);
      assert.equal(calls.length, before);
      await client.reset();
      assert.equal(client.getSnapshot().messages[0].text, '入口です');
      assert.equal(client.getSnapshot().turns[0].echoMessage, null);
      if (mode === 'api') assert.deepEqual(calls.at(-1), { input: { type: 'start' } });
    } finally { client.destroy(); }
  });

  test(`${mode}: memoryは共有領域に触れず、各clientの進行とLIFFを独立させる`, async () => {
    const names = ['indexedDB', 'localStorage', 'BroadcastChannel', 'navigator'];
    const original = new Map(names.map(name => [name, Object.getOwnPropertyDescriptor(globalThis, name)]));
    const accessed = [];
    for (const name of names) Object.defineProperty(globalThis, name, {
      configurable: true, get() { accessed.push(name); throw new Error(`共有領域へアクセス: ${name}`); },
    });
    const clients = [];
    let requestId = 0;
    const make = () => {
      const options = { bot: program.bot, storage: 'memory',
        indexedDB: { open() { accessed.push('options.indexedDB'); throw new Error('保存を開きました'); } } };
      const client = mode === 'trial'
        ? createTrialClient({ ...options, programUrl: 'https://static.example.test/scenario.json',
          fetch: async () => new Response(JSON.stringify(program)) })
        : createWebchatClient({ ...options, apiBaseUrl: 'https://api.example.test',
          fetch: async (_url, request) => {
            const body = JSON.parse(request.body);
            const revision = body.state_token ? Number(body.state_token) + 1 : 0;
            const id = String(++requestId);
            const liff = body.input.type === 'liff';
            return new Response(JSON.stringify({ schema_version: 1, request_id: id,
              state: { id, revision }, state_token: String(revision), liff_apps: [app],
              messages: liff ? [] : [{ id, type: 'text', text: body.input.text || '開始' }],
              chat_updated: !liff, ...(liff ? { liff_result: ['イベント'] } : {}),
            }), { headers: { 'Content-Type': 'application/json' } });
          } });
      clients.push(client);
      return client;
    };
    try {
      const one = make(), two = make();
      await Promise.all([one.start(), two.start()]);
      const before = two.getSnapshot();
      await one.sendText('左');
      const active = one.getSnapshot().activeResponse;
      const id = one.getSnapshot().stateId;
      assert.deepEqual(await one.requestLiff(app, 'open'), mode === 'trial' ? ['{"event":"open"}'] : ['イベント']);
      assert.notEqual(one.getSnapshot().stateId, id);
      assert.deepEqual(one.getSnapshot().activeResponse, active);
      await one.clearHistory();
      assert.equal(one.getSnapshot().messages.length, 0);
      await one.reset();
      assert.deepEqual(two.getSnapshot(), before);
      const fresh = make();
      await fresh.initialize();
      assert.equal(fresh.getSnapshot().stateId, null);
      assert.equal(fresh.getSnapshot().persistence, 'memory');
      assert.deepEqual(accessed, []);
    } finally {
      clients.forEach(client => client.destroy());
      for (const [name, descriptor] of original) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor);
        else delete globalThis[name];
      }
    }
  });
}

test('保存方式の誤指定は開始前に知らせる', () => {
  for (const storage of ['', 'localstorage', false]) {
    assert.throws(() => createWebchatClient({ apiBaseUrl: 'https://api.example.test', bot: 'bot', storage }), TypeError);
    assert.throws(() => createTrialClient({ bot: 'bot', programUrl: 'https://static.example.test/scenario.json', storage }), TypeError);
  }
});
