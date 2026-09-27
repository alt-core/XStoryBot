import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createTrialRuntime } from './trial-runtime.js';
import { createTrialClient } from './trial.js';

const fixture = name => JSON.parse(readFileSync(new URL(`../tests/fixtures/trial/${name}.json`, import.meta.url), 'utf8'));
const base = fixture('program');
const copy = () => structuredClone(base);
const page = base.liff_apps.menu.url;
const liff = action => ({ type: 'liff', app: 'menu', app_url: page, action });
const text = reply => reply.messages.filter(message => message.type === 'text').map(message => message.text);

for (const script of fixture('story').cases) {
  test(`Pythonと共通の台本: ${script.name}`, () => {
    const runtime = createTrialRuntime(copy());
    let reply, active = [];
    for (const step of script.steps) {
      let input = step.input;
      if (input.type === 'choice') input = { type: 'postback', postback_token: active.at(-1).quick_replies[input.index].token };
      if (input.type === 'liff') input = liff(input.action);
      reply = runtime.execute({ input, ...(reply ? { state_token: reply.state_token } : {}) });
      if (reply.chat_updated) active = reply.messages;
      const state = JSON.parse(reply.state_token);
      const actual = { texts: text(reply), choices: active.flatMap(message => message.quick_replies || []).map(choice => choice.label),
        scene: state.scene, waiting: !!state.wait };
      if (input.type === 'liff') Object.assign(actual, { events: reply.liff_result, chat_updated: reply.chat_updated });
      assert.deepEqual(actual, step.expect);
    }
  });
}

test('古い選択肢を拒否し、LIFFの出力違反では元の保存を変更しない', () => {
  const runtime = createTrialRuntime(copy());
  const start = runtime.execute({ input: { type: 'start' } });
  const selected = start.messages.at(-1).quick_replies[0].token;
  const next = runtime.execute({ input: { type: 'postback', postback_token: selected }, state_token: start.state_token });
  assert.throws(() => runtime.execute({ input: { type: 'postback', postback_token: selected }, state_token: next.state_token }), { code: 'action-not-active' });
  const blocked = copy();
  blocked.liff.action_prefix = '';
  const other = createTrialRuntime(blocked);
  assert.throws(() => other.execute({ input: liff('##line.follow'), state_token: start.state_token }), { code: 'trial-unsupported-command' });
  assert.equal(JSON.parse(start.state_token).wait.choices[0][0], '左');
});

test('メニューの内部入力はguardを通り、ignore_patternとfollowの規則を維持する', () => {
  const program = copy();
  program.richmenu = { id: 'main', revision: 'one' };
  program.menu_actions = [{ type: 'postback', data: '#help' }, { type: 'postback', data: '##line.follow' }];
  const input = area => ({ type: 'menu', menu: 'main', revision: 'one', area });
  let runtime = createTrialRuntime(program);
  const first = runtime.execute({ input: { type: 'start' } });
  assert.deepEqual(text(runtime.execute({ input: input(0), state_token: first.state_token })), ['選択してください']);
  assert.deepEqual(text(runtime.execute({ input: input(1), state_token: first.state_token })), ['入口です']);
  program.ignore_pattern = '^#help$';
  runtime = createTrialRuntime(program);
  const result = runtime.execute({ input: input(0), state_token: first.state_token });
  assert.deepEqual(text(result), ['説明です']);
  assert.ok(JSON.parse(result.state_token).wait);
});

test('guardなしの選択待ちは再提示先がなくても保存・再開できる', () => {
  const program = copy();
  for (const block of Object.values(program.blocks)) for (const op of block.ops) {
    if (op.op === 'wait') Object.assign(op.wait, { guard: false, retry: '#未使用の再提示先' });
  }
  const runtime = createTrialRuntime(program);
  const first = runtime.execute({ input: { type: 'start' } });
  runtime.restore({ stateToken: first.state_token });
  const next = runtime.execute({ input: { type: 'text', text: 'その他の入力' }, state_token: first.state_token });
  assert.equal(JSON.parse(next.state_token).wait, null);
  assert.deepEqual(text(next), ['入力が見つかりません']);
});

function smallProgram(ops) {
  const program = copy();
  program.startup = 'demo/';
  program.scenes = { 'demo/': { tab: 'demo', entry: '#entry', rules: [
    { test: { kind: 'exact', value: '##line.follow' }, block: 'start' },
    { test: { kind: 'exact', value: '#again' }, block: 'start' },
  ] } };
  program.blocks = { start: { ops, next: null } };
  return program;
}
const emit = value => ({ op: 'emit', text: value, messages: [{ type: 'text', role: 'assistant', sender: null, text: value }] });

test('命令数と応答数の上限で停止し、独自の途中保存をしない', () => {
  for (const ops of [[{ op: 'jump', action: '#again' }], Array.from({ length: 101 }, () => emit('本文'))]) {
    const runtime = createTrialRuntime(smallProgram(ops));
    assert.throws(() => runtime.execute({ input: { type: 'start' } }), { code: 'trial-execution-limit' });
  }
});

test('正規表現の独自オプションを扱い、Pythonの文字範囲は再実装しない', () => {
  const cases = [
    ['yes', { ignore_case: true }, 'YES', true],
    ['YES', { lower: true }, 'YES', false],
    ['yes', { lower: true }, 'YES', true],
    ['yes', { normalize: true, lower: true, exact: true }, 'ＹＥＳ', true],
    ['カタカナ', { normalize: true, exact: true }, 'ｶﾀｶﾅ', true],
    ['a.b', {}, 'a\nb', true],
    ['a|ab', { exact: true }, 'ab', false],
    ['a|ab', { exact: true }, 'a', true],
    ['a\\/b', { exact: true }, 'a/b', true],
    ['\\d', { exact: true }, '１', false],
    ['[\\w]+', { exact: true }, '日本', false],
  ];
  for (const [value, flags, input, expected] of cases) {
    const program = smallProgram([]);
    program.scenes['demo/'].rules.push({ test: { kind: 'regex', value, ...flags }, block: 'match' });
    program.blocks.match = { ops: [emit('一致')], next: null };
    const runtime = createTrialRuntime(program);
    let reply = runtime.execute({ input: { type: 'start' } });
    for (let repeat = 0; repeat < 2; repeat++) {
      reply = runtime.execute({ input: { type: 'text', text: input }, state_token: reply.state_token });
      assert.equal(reply.messages.length > 0, expected, `${value}: ${input}`);
    }
  }
  const invalid = smallProgram([]);
  invalid.scenes['demo/'].rules.push({ test: { kind: 'regex', value: '(?P<name>a)' }, block: 'start', source: { sheet: '台本', line: 8 } });
  assert.throws(() => createTrialRuntime(invalid), error => error.code === 'invalid-scenario' && error.message.includes('台本!8行目'));
});

test('静的JSON一件の取得だけで会話・LIFF・履歴削除を処理できる', async () => {
  let requests = 0;
  const client = createTrialClient({ bot: base.bot, programUrl: 'https://static.example.test/game/assets/revision/scenario.json',
    fetch: async url => {
      assert.equal(url, 'https://static.example.test/game/assets/revision/scenario.json');
      requests++;
      return new Response(JSON.stringify(base));
    } });
  try {
    await client.start();
    const active = client.getSnapshot().activeResponse;
    const stateId = client.getSnapshot().stateId;
    assert.deepEqual(await client.requestLiff({ id: 'menu', url: page }, 'open'), ['{"event":"open"}']);
    assert.deepEqual(client.getSnapshot().activeResponse, active);
    assert.notEqual(client.getSnapshot().stateId, stateId);
    await client.clearHistory();
    assert.equal(client.getSnapshot().messages.length, 0);
    assert.deepEqual(client.getSnapshot().activeResponse, active);
    await client.sendPostback(active.at(-1).quick_replies[1].token);
    assert.deepEqual(client.getSnapshot().messages.map(message => message.text), ['右の道', '到着しました']);
    await client.reset();
    assert.equal(requests, 1);
  } finally { client.destroy(); }
});

test('randomUUIDがない環境でも開始・選択・LIFF・resetを処理できる', async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  const getRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
  // HTTP配信時と同様に、getRandomValuesだけを提供する。
  Object.defineProperty(globalThis, 'crypto', { configurable: true, value: { getRandomValues } });
  const client = createTrialClient({ bot: base.bot, programUrl: 'http://example.test/scenario.json',
    fetch: async () => new Response(JSON.stringify(base)) });
  try {
    await client.start();
    const started = client.getSnapshot();
    const choice = started.activeResponse.at(-1).quick_replies[0].token;
    await client.sendPostback(choice);
    const advanced = client.getSnapshot();
    assert.deepEqual(advanced.messages.slice(-2).map(message => message.text), ['左の道', '到着しました']);
    assert.deepEqual(await client.requestLiff({ id: 'menu', url: page }, 'open'), ['{"event":"open"}']);
    const afterLiff = client.getSnapshot();
    await client.reset();
    const restarted = client.getSnapshot();
    assert.equal(restarted.status, 'ready');
    assert.equal(new Set([started.stateId, advanced.stateId, afterLiff.stateId, restarted.stateId]).size, 4);
    const oldAction = JSON.parse(choice);
    const newAction = JSON.parse(restarted.activeResponse.at(-1).quick_replies[0].token);
    assert.notEqual(oldAction.conversation, newAction.conversation);
    assert.notEqual(oldAction.generation, newAction.generation);
    await assert.rejects(client.sendPostback(choice), { code: 'action-not-active' });
    assert.equal(client.getSnapshot().stateId, restarted.stateId);
  } finally {
    client.destroy();
    Object.defineProperty(globalThis, 'crypto', original);
  }
});

test('乱数APIがない場合は保存失敗と混同せずブラウザの非対応を知らせる', async () => {
  const original = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
  try {
    for (const crypto of [undefined, {}]) {
      Object.defineProperty(globalThis, 'crypto', { configurable: true, value: crypto });
      const client = createTrialClient({ bot: base.bot, programUrl: 'http://example.test/scenario.json',
        fetch: async () => new Response(JSON.stringify(base)) });
      try {
        await assert.rejects(client.start(), { code: 'unsupported-browser' });
        assert.equal(client.getSnapshot().status, 'error');
        assert.equal(client.getSnapshot().error.code, 'unsupported-browser');
        assert.equal(client.getSnapshot().stateId, null);
        assert.equal(client.getSnapshot().turns.length, 0);
      } finally { client.destroy(); }
    }
  } finally { Object.defineProperty(globalThis, 'crypto', original); }
});

test('保存形式・epoch・台本参照の不一致を、初期化時に診断する', () => {
  const runtime = createTrialRuntime(copy());
  const first = runtime.execute({ input: { type: 'start' } });
  for (const change of [{ epoch: 'next' }, { scene: 'missing' }, { wait: { label: 'missing' } }]) {
    const token = JSON.stringify({ ...JSON.parse(first.state_token), ...change });
    assert.throws(() => runtime.restore({ stateToken: token }), WebchatError => ['invalid-state', 'incompatible-state'].includes(WebchatError.code));
  }
  const broken = smallProgram([{ op: 'unknown' }]);
  assert.throws(() => createTrialRuntime(broken), { code: 'invalid-scenario' });
});

test('LIFFの入口不一致と途中の無効ラベルを区別し、会話用エラー表示はLIFFで実行しない', () => {
  const program = copy();
  const start = createTrialRuntime(program).execute({ input: { type: 'start' } });
  const state = { ...JSON.parse(start.state_token), wait: null };
  const token = JSON.stringify(state);
  const run = (runtime, input) => runtime.execute({ input, state_token: token });
  assert.deepEqual(run(createTrialRuntime(program), liff('missing')).liff_result, []);
  program.liff.ignore_unhandled_action = false;
  assert.deepEqual(run(createTrialRuntime(program), liff('missing')).liff_result, ['移動先が見つかりません']);
  assert.deepEqual(text(run(createTrialRuntime(program), { type: 'text', text: '未登録入力' })), ['入力が見つかりません']);

  program.liff.ignore_unhandled_action = true;
  program.scenes[program.startup].rules.push({ test: { kind: 'exact', value: '##liff.broken' }, block: 'broken' });
  program.blocks.broken = { ops: [{ op: 'jump', action: '#存在しないラベル' }], next: null };
  assert.deepEqual(run(createTrialRuntime(program), liff('broken')).liff_result, ['移動先が見つかりません']);

  const errorRule = program.scenes[program.startup].rules.find(rule => rule.test.value === '##error_invalid_label');
  program.blocks[errorRule.block] = { ops: [{ op: 'emit', text: null, messages: [{ type: 'button', text: '選択' }] }], next: null };
  const runtime = createTrialRuntime(program);
  assert.throws(() => run(runtime, liff('broken')), { code: 'trial-unsupported-command' });
  assert.deepEqual(run(runtime, liff('open')).liff_result, ['{"event":"open"}']);
});
