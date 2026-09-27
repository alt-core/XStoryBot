import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createWebchatClient } from './index.js';
import { createTrialClient } from './trial.js';

const program = JSON.parse(readFileSync(new URL('../tests/fixtures/trial/program.json', import.meta.url), 'utf8'));
const app = { id: 'menu', url: program.liff_apps.menu.url };

for (const mode of ['api', 'trial']) {
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
