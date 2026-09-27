// 体験版の書出し例を静的配信し、実DOM・IndexedDB・iframe通信を検証する。
const output = document.querySelector('#result');
const stage = document.querySelector('#stage');
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const waitFor = async (read, message) => {
  const deadline = performance.now() + 8000;
  while (performance.now() < deadline) {
    const result = read();
    if (result) return result;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error(message);
};
const key = 'trial:trial-demo';
async function cleanSave(key) {
  const database = await new Promise((resolve, reject) => {
    const request = indexedDB.open('xstorybot-webchat-v1');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  try {
    if (!database.objectStoreNames.contains('conversations')) return;
    await new Promise((resolve, reject) => {
      const tx = database.transaction(['conversations', 'turns'], 'readwrite');
      tx.oncomplete = resolve; tx.onabort = () => reject(tx.error);
      tx.objectStore('conversations').delete(key);
      const turns = tx.objectStore('turns');
      turns.index('conversationKey').getAllKeys(key).onsuccess = event => {
        for (const id of event.target.result) turns.delete(id);
      };
    });
  } finally { database.close(); }
  localStorage.removeItem(`xstorybot-webchat-richmenu:${key}`);
}

async function viewport(width, height) {
  await cleanSave(key);
  const frame = document.createElement('iframe');
  frame.src = './index.html'; frame.title = `体験版 ${width}×${height}`;
  frame.style.cssText = `display:block;width:${width}px;height:${height}px;border:0`;
  stage.append(frame);
  try {
    let doc = await waitFor(() => frame.contentDocument?.querySelector('#messages')?.textContent.includes('どちらの道') && frame.contentDocument, '会話が開始しません');
    const texts = () => doc.querySelector('#messages').textContent;
    const choice = await waitFor(() => [...doc.querySelectorAll('button')].find(button => button.textContent === '左の道'), '選択肢がありません');
    choice.click();
    await waitFor(() => texts().includes('広場に着きました'), '選択後に進みません');
    const chat = doc.querySelector('.chat').getBoundingClientRect();
    assert(chat.width <= Math.min(width, 480) + 1 && chat.height > chat.width, '縦長の会話領域ではありません');
    assert(doc.documentElement.scrollWidth <= width + 1, '横にはみ出しています');
    const picture = doc.querySelector('.bubble.media img');
    await waitFor(() => picture.complete && picture.naturalWidth > 0, '同梱画像を読み込めません');
    const menu = doc.querySelector('#richmenu');
    assert(!menu.hidden && menu.getBoundingClientRect().bottom <= chat.bottom + 1, 'メニューが画面内にありません');
    const openPage = async () => {
      doc.querySelector('#richmenu [aria-label="ページを開く"]').click();
      const child = await waitFor(() => doc.querySelector('.link-viewer-frame'), 'LIFFページを開けません');
      await waitFor(() => child.contentDocument?.querySelector('#events')?.textContent.includes('place'), 'LIFF初期化に失敗しました');
      const viewer = doc.querySelector('#media-viewer').getBoundingClientRect();
      assert(Math.abs(viewer.width - chat.width) < 2 && Math.abs(viewer.left - chat.left) < 2, 'LIFFと会話の幅が違います');
      assert(Math.abs(child.getBoundingClientRect().width - viewer.width) < 2, 'iframeに余分な横余白があります');
      return child.contentDocument;
    };
    let child = await openPage();
    assert(child.querySelector('#events').textContent.includes('広場'), '会話の場面とLIFFの情報が一致しません');
    child.querySelector('[data-action="bump"]').click();
    await waitFor(() => child.querySelector('#events').textContent.includes('展望台'), 'LIFFのイベントが進みません');
    child.querySelector('#send').click();
    await waitFor(() => !doc.querySelector('#media-viewer').open && texts().includes('持ち物を確認しました'), '発話して会話へ戻れません');
    child = await openPage();
    assert(child.querySelector('#events').textContent.includes('展望台'), 'LIFFを閉じると進行が失われます');
    doc.querySelector('#media-viewer-close').click();
    doc.querySelector('#richmenu-toggle').click();
    assert(menu.hidden, 'メニューを閉じられません');
    frame.contentWindow.location.reload();
    await waitFor(() => frame.contentDocument !== doc && frame.contentDocument?.querySelector('#messages')?.textContent.includes('持ち物を確認しました'), '保存した会話を復元できません');
    doc = frame.contentDocument;
    await waitFor(() => !doc.querySelector('#richmenu-toggle').hidden && doc.querySelector('#richmenu .hotspot'), 'メニューを復元できません');
    assert(doc.querySelector('#richmenu').hidden, '閉じたメニューが保存されていません');
    doc.querySelector('#richmenu-toggle').click();
    child = await openPage();
    assert(child.querySelector('#events').textContent.includes('展望台'), '再読込でLIFFの進行が失われました');
    doc.querySelector('#media-viewer-close').click();
  } finally { frame.remove(); await cleanSave(key); }
}

async function storage(program, createTrialClient, programUrl) {
  const bot = `trial-test-${crypto.randomUUID()}`;
  const data = structuredClone(program);
  data.bot = bot;
  for (const app of Object.values(data.liff_apps)) app.bot = bot;
  const clients = [];
  const make = epoch => {
    const client = createTrialClient({ bot, programUrl,
      fetch: async () => new Response(JSON.stringify({ ...data, epoch })) });
    clients.push(client); return client;
  };
  try {
    const one = make('1'), two = make('1');
    await one.start(); await two.initialize();
    assert(one.getSnapshot().persistence === 'indexeddb', '実IndexedDBを使っていません');
    const results = await Promise.allSettled([one.sendText('左の道'), two.sendText('右の道')]);
    assert(results.filter(result => result.status === 'fulfilled').length === 1, '並行入力の両方が保存されました');
    assert(results.find(result => result.status === 'rejected')?.reason.code === 'state-refreshed', '並行更新を診断できません');
    const next = make('2');
    try { await next.initialize(); throw new Error('epoch変更を見逃しました'); }
    catch (error) { assert(error.code === 'incompatible-state', 'epoch検査が保存失敗に化けました'); }
    assert(next.getSnapshot().persistence === 'indexeddb', 'epoch不一致でmemoryに逃げました');
    await next.reset();
    assert(next.getSnapshot().status === 'ready', 'epoch不一致からresetできません');
    await waitFor(() => one.getSnapshot().error?.code === 'incompatible-state', '旧版のタブが更新を受け入れました');
    try { await one.sendText('左の道'); throw new Error('旧版が新しい保存を上書きしました'); }
    catch (error) { assert(error.code === 'incompatible-state', '旧版の操作を止められません'); }
    const same = make('2');
    await same.initialize();
    await same.clearHistory();
    assert(same.getSnapshot().messages.length === 0 && same.getSnapshot().activeResponse.some(message => message.quick_replies?.length), '履歴削除で選択肢が消えました');
  } finally { clients.forEach(client => client.destroy()); await cleanSave(`trial:${bot}`); }
}

// 同じBotの永続保存とmemoryページを並べ、相互に影響しないことを確認する。
async function exportOptions() {
  await cleanSave(key);
  const frames = [];
  const opened = frame => waitFor(() => frame.contentDocument?.querySelector('#messages')?.textContent.includes('どちらの道') && frame.contentDocument, '初期画面を表示できません');
  const open = async (path, width, scheme = 'normal') => {
    const frame = document.createElement('iframe');
    frame.src = path;
    frame.style.cssText = `width:${width}px;height:844px;border:0;color-scheme:${scheme}`;
    frames.push(frame); stage.append(frame);
    return [frame, await opened(frame)];
  };
  const choose = async doc => {
    [...doc.querySelectorAll('button')].find(button => button.textContent === '左の道').click();
    await waitFor(() => doc.querySelector('#messages').textContent.includes('広場に着きました'), '会話が進みません');
  };
  try {
    const [savedFrame, saved] = await open('./index.html', 390);
    await choose(saved);
    saved.querySelector('#richmenu-toggle').click();
    const menuKey = `xstorybot-webchat-richmenu:${key}`;
    const savedMenu = localStorage.getItem(menuKey);
    const [oneFrame, one] = await open('./memory/index.html', 320, 'light');
    const [, two] = await open('./memory/index.html', 1440, 'dark');
    for (const doc of [one, two]) {
      assert(doc.documentElement.dataset.webchatStorage === 'memory', 'memory指定がありません');
      const title = doc.querySelector('#chat-title');
      assert(doc.title !== 'Webchat' && doc.title === title.textContent, '題名が一致しません');
      assert(title.getBoundingClientRect().right <= doc.querySelector('#reset').getBoundingClientRect().left, '題名が操作ボタンに重なります');
      assert(!doc.querySelector('#richmenu').hidden, '他ページのメニュー保存を読みました');
      const styles = doc.defaultView.getComputedStyle(doc.documentElement);
      const dark = doc.defaultView.matchMedia('(prefers-color-scheme: dark)').matches;
      assert(dark === (doc === two), '明暗の検証条件を適用できません');
      assert(styles.getPropertyValue('--accent').trim() === (dark ? '#8cd0c7' : '#28716a'), 'テーマが配色に反映されません');
      const background = doc.defaultView.getComputedStyle(doc.querySelector('.chat-header')).backgroundImage;
      assert(background.includes('/theme/header.svg'), 'テーマの画像参照がありません');
      const image = new Image(); image.src = background.slice(5, -2);
      await image.decode();
    }
    await choose(one);
    one.querySelector('#richmenu [aria-label="ページを開く"]').click();
    const child = await waitFor(() => one.querySelector('.link-viewer-frame')?.contentDocument?.querySelector('#events')?.textContent.includes('広場') && one.querySelector('.link-viewer-frame').contentDocument, 'memoryのLIFFが親の進行を取得できません');
    child.querySelector('[data-action="bump"]').click();
    await waitFor(() => child.querySelector('#events').textContent.includes('展望台'), 'memoryのLIFFが進みません');
    child.querySelector('#send').click();
    await waitFor(() => one.querySelector('#messages').textContent.includes('持ち物を確認しました'), 'memoryのLIFFから発話できません');
    assert(!two.querySelector('#messages').textContent.includes('広場に着きました'), '別のmemoryページが進みました');
    one.querySelector('#richmenu-toggle').click();
    assert(one.querySelector('#richmenu').hidden, 'memory内でメニューを閉じられません');
    one.querySelector('#richmenu-toggle').click();
    assert(localStorage.getItem(menuKey) === savedMenu, 'memoryがメニューの永続保存を書き換えました');
    one.querySelector('#richmenu-toggle').click();
    oneFrame.contentWindow.location.reload();
    await waitFor(() => oneFrame.contentDocument !== one, 'memoryを再読み込みできません');
    const fresh = await opened(oneFrame);
    await waitFor(() => fresh.querySelector('#richmenu .hotspot'), 'memoryのメニューを読み込めません');
    assert(!fresh.querySelector('#richmenu').hidden, '再読み込み後もメニューを保持しています');
    savedFrame.contentWindow.location.reload();
    const restored = await waitFor(() => savedFrame.contentDocument !== saved && savedFrame.contentDocument?.querySelector('#messages')?.textContent.includes('広場に着きました') && savedFrame.contentDocument, '永続保存が失われました');
    assert(!restored.querySelector('#messages').textContent.includes('持ち物を確認しました'), 'memoryの進行が永続保存へ混入しました');
    await waitFor(() => restored.querySelector('#richmenu .hotspot'), '保存したメニューを読み込めません');
    assert(restored.querySelector('#richmenu').hidden, '永続保存のメニュー開閉が失われました');
  } finally { frames.forEach(frame => frame.remove()); await cleanSave(key); }
}

try {
  assert(location.hostname === '127.0.0.1', 'このテストは127.0.0.1の専用静的サーバーで実行してください');
  const html = new DOMParser().parseFromString(await (await fetch('./index.html')).text(), 'text/html');
  assert(html.documentElement.dataset.webchatTrial === '1' && html.documentElement.dataset.webchatBot === 'trial-demo', '書出し例専用のテストです');
  const scriptUrl = new URL(html.querySelector('script[type="module"]').getAttribute('src'), location.href);
  const programUrl = new URL('./scenario.json', scriptUrl);
  const program = await (await fetch(programUrl)).json();
  const { createTrialClient } = await import(new URL('./trial.js', scriptUrl));
  const results = [];
  for (const size of [[390, 844], [320, 568], [1440, 900], [1280, 720]]) {
    await viewport(...size);
    results.push(`${size.join('×')}: 選択・画像・メニュー・LIFF・再開に成功`);
    output.textContent = results.join('\n');
  }
  await storage(program, createTrialClient, programUrl);
  output.textContent += '\n実IndexedDB・並行入力・epoch更新・reset・履歴削除に成功';
  if (new URLSearchParams(location.search).has('options')) {
    await exportOptions();
    output.textContent += '\nmemoryのページ分離・LIFF・再読み込み・題名・テーマ・素材読込に成功';
  }
  output.dataset.status = 'passed';
} catch (error) {
  output.textContent += `\n失敗: ${error.message || error}`;
  output.dataset.status = 'failed';
}
