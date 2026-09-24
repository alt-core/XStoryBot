import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { findLiffApp } from '../webchat-client/liff-host.js';

import {
  classifyUriTarget,
  createBottomResizeFollower,
  createControlActivator,
  createHorizontalDragController,
  createVideoPlaybackController,
  createVideoCompletionQueue,
} from '../static/webchat/ui_logic.mjs';

test('URIは通常HTTPSを埋込み外部指定を新規tabへ分類する', () => {
  assert.equal(classifyUriTarget('https://example.test/path'), 'embedded');
  assert.equal(classifyUriTarget(
    'https://example.test/path?openExternalBrowser=1#section'), 'external');
  assert.equal(classifyUriTarget(
    'https://example.test/path?openExternalBrowser=0'), 'embedded');
  assert.equal(classifyUriTarget('tel:09001234567'), 'native');
  assert.equal(classifyUriTarget('http://example.test/path'), 'blocked');
  assert.equal(classifyUriTarget(
    'http://127.0.0.1:8765/path', 'http://127.0.0.1:8765'), 'embedded');
  assert.equal(classifyUriTarget(
    'http://example.test/path?openExternalBrowser=1'), 'external');
  assert.equal(classifyUriTarget('javascript:alert(1)'), 'blocked');
});

test('登録LIFFでも外部指定を優先し、通常リンクとlocal HTTPの埋込みを維持する', () => {
  const source = readFileSync(new URL('../static/webchat/app.js', import.meta.url), 'utf8');
  const start = source.indexOf('const linkAttributes =');
  const end = source.indexOf('const richmenuStorageKey =');
  assert.ok(start >= 0 && end > start);
  const apps = [
    { id: 'room', url: 'https://pages.example.test/app/', liff_id: '123-room', match: 'prefix' },
    { id: 'local', url: 'http://127.0.0.1:8766/app/' },
  ];
  const document = { createElement: (tag) => ({ tag, dataset: {}, setAttribute() {}, addEventListener() {} }) };
  const uriControl = new Function('document', 'client', 'findLiffApp', 'classifyUriTarget', 'location',
    `${source.slice(start, end)}\nreturn uriControl;`)(document, { getSnapshot: () => ({ liffApps: apps }) },
    findLiffApp, classifyUriTarget, { origin: 'http://127.0.0.1:8765' });
  for (const [href, mode] of [
    ['https://pages.example.test/app/words/?openExternalBrowser=1#item', 'external'],
    ['https://liff.line.me/123-room/words/?openExternalBrowser=1#item', 'external'],
    ['http://127.0.0.1:8766/app/?openExternalBrowser=1', 'external'],
    ['https://pages.example.test/app/words/', 'embedded'],
    ['https://liff.line.me/123-room/words/', 'embedded'],
    ['http://127.0.0.1:8766/app/', 'embedded'],
    ['http://127.0.0.1:8766/unregistered/', 'blocked'],
  ]) {
    const control = uriControl({ type: 'uri', href, label: '開く' }, 'card-action');
    assert.equal(control.dataset.linkMode, mode, href);
    assert.equal(control.tag, mode === 'external' ? 'a' : 'button', href);
    if (mode === 'external') {
      assert.equal(control.href, href);
      assert.equal(control.target, '_blank');
      assert.equal(control.rel, 'noopener noreferrer');
    }
  }
});

test('controlなし動画は開始しtapごとに一時停止と再生を切り替える', () => {
  let paused = true;
  let playCalls = 0;
  let pauseCalls = 0;
  const controller = createVideoPlaybackController({
    isPaused: () => paused,
    play: () => {
      playCalls += 1;
      paused = false;
      return Promise.resolve();
    },
    pause: () => {
      pauseCalls += 1;
      paused = true;
    },
  });

  controller.start();
  assert.equal(playCalls, 1);
  controller.toggle();
  assert.equal(pauseCalls, 1);
  controller.toggle();
  assert.equal(playCalls, 2);
});

test('同じcontrolの二重発火だけを抑止し新controlは止めない', async () => {
  const calls = [];
  const timers = [];
  const options = {
    setTimer: (callback) => timers.push(callback),
  };
  const first = createControlActivator(async (value) => {
    calls.push(value);
  }, options);
  const next = createControlActivator(async (value) => {
    calls.push(value);
  }, options);

  assert.equal(await first('old'), true);
  assert.equal(await first('duplicate'), false);
  assert.equal(await next('new'), true);
  assert.deepEqual(calls, ['old', 'new']);

  timers.shift()();
  assert.equal(await first('old-again'), true);
  assert.deepEqual(calls, ['old', 'new', 'old-again']);
});

test('Quick Replyはmouse dragで横scrollしdrag後のclickを抑止する', () => {
  let scrollLeft = 40;
  const timers = [];
  const controller = createHorizontalDragController({
    getScrollLeft: () => scrollLeft,
    setScrollLeft: (value) => {
      scrollLeft = value;
    },
    setTimer: (callback) => timers.push(callback),
  });

  assert.equal(controller.pointerDown({
    pointerId: 1,
    clientX: 100,
    button: 0,
    pointerType: 'touch',
    scrollable: true,
  }), false);
  assert.equal(controller.pointerDown({
    pointerId: 1,
    clientX: 100,
    button: 0,
    pointerType: 'mouse',
    scrollable: true,
  }), true);
  assert.deepEqual(controller.pointerMove({
    pointerId: 1,
    clientX: 96,
  }), { handled: true, dragging: false });
  assert.equal(scrollLeft, 40);
  assert.deepEqual(controller.pointerMove({
    pointerId: 1,
    clientX: 70,
  }), { handled: true, dragging: true });
  assert.equal(scrollLeft, 70);
  assert.equal(controller.pointerUp(1), true);
  assert.equal(controller.consumeClick(), true);
  assert.equal(controller.consumeClick(), false);
  timers.shift()();

  assert.equal(controller.pointerDown({
    pointerId: 2,
    clientX: 100,
    button: 0,
    pointerType: 'mouse',
    scrollable: true,
  }), true);
  assert.equal(controller.pointerUp(2), false);
  assert.equal(controller.consumeClick(), false);
});

test('履歴の遅延resizeは変更前に最下部なら追従し上にいたら維持する', () => {
  let scrollTop = 600;
  let scrollHeight = 1000;
  let following = false;
  let calls = 0;
  const follower = createBottomResizeFollower({
    getScrollTop: () => scrollTop,
    getClientHeight: () => 400,
    getScrollHeight: () => scrollHeight,
    isFollowing: () => following,
    scrollToBottom: () => {
      calls += 1;
      scrollTop = scrollHeight - 400;
    },
  });

  scrollHeight = 1300;
  assert.equal(follower.sync(), true);
  assert.equal(calls, 1);
  assert.equal(scrollTop, 900);

  scrollTop = 400;
  scrollHeight = 1600;
  assert.equal(follower.sync(), false);
  assert.equal(calls, 1);
  assert.equal(scrollTop, 400);

  following = true;
  scrollHeight = 1500;
  assert.equal(follower.sync(), true);
  assert.equal(calls, 2);
  assert.equal(scrollTop, 1100);
});

test('動画完了actionは送信中を待ちmessageごとに一回だけ送る', async () => {
  let status = 'sending';
  const calls = [];
  const queue = createVideoCompletionQueue({
    getStatus: () => status,
    send: async (action) => calls.push(action.token),
  });

  assert.equal(queue.enqueue('video-1', { token: 'one' }), true);
  assert.equal(queue.enqueue('video-1', { token: 'one' }), false);
  assert.deepEqual(calls, []);

  status = 'ready';
  assert.equal(await queue.flush(), true);
  assert.deepEqual(calls, ['one']);
  assert.equal(queue.enqueue('video-1', { token: 'one' }), false);
});

test('client request開始との競合だけはpendingへ戻して再試行する', async () => {
  let attempts = 0;
  const queue = createVideoCompletionQueue({
    getStatus: () => 'ready',
    send: async () => {
      attempts += 1;
      if (attempts === 1) throw { code: 'request-in-flight' };
    },
  });

  queue.enqueue('video-1', { token: 'one' });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(attempts, 1);
  assert.equal(await queue.flush(), true);
  assert.equal(attempts, 2);
});

test('リッチメニューの開閉は同じ名前の利用者操作を維持する', async () => {
  const {richmenuOpenState} = await import('../static/webchat/ui_logic.mjs');
  assert.equal(richmenuOpenState(null, {id: 'main', selected: true}), true);
  assert.equal(richmenuOpenState({id: 'main', open: false}, {id: 'main', selected: true}), false);
  assert.equal(richmenuOpenState({id: 'old', open: false}, {id: 'main', selected: true}), true);
});
