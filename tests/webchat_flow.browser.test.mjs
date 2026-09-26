// 開発serverの人工Botで、実DOM・HTTP・保存・iframe通信を通して画面を検証する。
if (typeof document === 'undefined') throw new Error('画面のテストは開発serverの/devtest/flowで実行してください');
const resultElement = document.querySelector('#flow-test-result');
const stage = document.querySelector('#flow-test-stage');
const results = [];
const widths = [];
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
const rect = element => element.getBoundingClientRect();
const inside = (inner, outer) => inner.left >= outer.left - 2 && inner.right <= outer.right + 2
  && inner.top >= outer.top - 2 && inner.bottom <= outer.bottom + 2;
const text = document => document.querySelector('#messages').textContent;

async function removeTestSave(key) {
  const database = await new Promise((resolve, reject) => {
    const request = indexedDB.open('xstorybot-webchat-v1');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  try {
    if (!database.objectStoreNames.contains('conversations')) return;
    await new Promise((resolve, reject) => {
      const tx = database.transaction(['conversations', 'turns'], 'readwrite');
      tx.oncomplete = resolve;
      tx.onabort = () => reject(tx.error);
      tx.objectStore('conversations').delete(key);
      const turns = tx.objectStore('turns');
      turns.index('conversationKey').getAllKeys(key).onsuccess = event => {
        for (const id of event.target.result) turns.delete(id);
      };
    });
  } finally { database.close(); }
  localStorage.removeItem(`xstorybot-webchat-richmenu:${key}`);
}

async function runViewport(width, height) {
  const bot = `ui-test-${width}-${crypto.randomUUID()}`;
  const key = `${location.origin}|${bot}`;
  const frame = document.createElement('iframe');
  frame.title = `画面テスト ${width}×${height}`;
  frame.style.cssText = `display:block;border:0;width:${width}px;height:${height}px;`;
  frame.src = `/chat/${bot}`;
  stage.append(frame);
  frame.scrollIntoView({ block: 'start' });
  stage.scrollLeft = Math.max(0, (width - stage.clientWidth) / 2);
  try {
    await waitFor(() => frame.contentDocument?.querySelector('#messages')?.textContent.includes('こんにちは'), '初回会話が表示されません');
    let doc = frame.contentDocument;
    const send = async value => {
      const input = doc.querySelector('#draft');
      input.value = value;
      input.dispatchEvent(new frame.contentWindow.Event('input', { bubbles: true }));
      doc.querySelector('#send').click();
      await waitFor(() => !doc.querySelector('#reset').disabled && text(doc).includes(value), '発話への応答がありません');
    };
    const checkLayout = () => {
      const chat = rect(doc.querySelector('.chat'));
      const rem = parseFloat(frame.contentWindow.getComputedStyle(doc.documentElement).fontSize);
      assert(chat.width <= Math.min(width, 30 * rem) + 1, 'チャットがスマートフォン幅を超えました');
      assert(Math.abs(chat.left - (width - chat.width) / 2) <= 1, 'チャットが中央にありません');
      assert(chat.height <= height + 1 && chat.height > chat.width, '縦長画面の範囲を維持できません');
      assert(doc.documentElement.scrollWidth <= width + 1 && doc.documentElement.scrollHeight <= height + 1,
        'ページ全体にはみ出しがあります');
      assert(inside(rect(doc.querySelector('#composer')), chat), '入力欄が画面外にあります');
      assert(rect(doc.querySelector('#scroller')).height > 80, '会話欄の高さが足りません');
      return chat;
    };
    await send('あ'.repeat(68));
    const bubble = [...doc.querySelectorAll('.bubble.text')].find(e => e.textContent.startsWith('入力を受け取りました'));
    const style = frame.contentWindow.getComputedStyle(bubble);
    const contentWidth = rect(bubble).width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    assert(contentWidth <= 17 * parseFloat(style.fontSize) + 2, '本文が全角17文字程度の幅を超えました');
    if (width >= 390) widths.push(contentWidth);
    checkLayout();
    await send('image');
    await send('richmenu');
    const menu = doc.querySelector('#richmenu');
    await waitFor(() => !menu.hidden && menu.querySelector('img')?.complete, 'リッチメニューを表示できません');
    const chat = checkLayout();
    assert(inside(rect(menu), chat), 'リッチメニューがチャット領域からはみ出しました');
    for (const hotspot of menu.querySelectorAll('.hotspot')) {
      assert(inside(rect(hotspot), rect(menu)), '領域がメニュー画像からはみ出しました');
    }
    doc.querySelector('#richmenu-toggle').click();
    assert(menu.hidden, 'メニューを閉じられません');
    doc.querySelector('#richmenu-toggle').click();
    assert(!menu.hidden, 'メニューを開けません');

    const openLiff = async () => {
      // 会話内のリンクを使わず、リッチメニューの領域を選ぶ。
      doc.querySelector('#richmenu [aria-label="LIFFを開く"]').click();
      const child = await waitFor(() => doc.querySelector('.link-viewer-frame'), 'LIFFを開けません');
      await waitFor(() => child.contentDocument?.querySelector('#events')?.textContent.includes('count')
        && !child.contentDocument.querySelector('#send').disabled, 'LIFFの初期化が完了しません');
      assert(new URL(child.src).searchParams.get('xsb_client') === 'webchat', '起動指定がありません');
      const viewer = doc.querySelector('#media-viewer');
      assert(viewer.open, 'viewerが開いていません');
      assert(Math.abs(rect(viewer).left - chat.left) <= 2 && Math.abs(rect(viewer).width - chat.width) <= 2,
        'LIFF viewerがチャットと同じ幅・位置にありません');
      assert(Math.abs(rect(child).width - rect(viewer).width) <= 2,
        `LIFFの左右に余分な余白があります: ${JSON.stringify({ viewer: rect(viewer).width,
          page: rect(child).width, classes: viewer.className,
          children: [...viewer.children].map(element => element.tagName),
          padding: frame.contentWindow.getComputedStyle(viewer).padding })}`);
      assert(inside(rect(child), rect(viewer)), 'LIFFページがviewerからはみ出しました');
      assert(child.contentDocument.documentElement.scrollWidth <= child.clientWidth + 1,
        '確認用LIFFページに横はみ出しがあります');
      const close = rect(doc.querySelector('#media-viewer-close'));
      assert(inside(close, rect(viewer)) && close.width >= 44 && close.height >= 44, '閉じるボタンが操作しにくい配置です');
      assert(close.bottom <= rect(child).top + 1, '閉じるボタンがLIFFページに重なっています');
      return child.contentDocument;
    };
    // 閉じた直後にLIFFへ切り替えても、前のviewerの後片付けが干渉しない。
    const picture = await waitFor(() => doc.querySelector('.bubble.media img'), '会話の画像がありません');
    picture.scrollIntoView({ block: 'center' });
    await waitFor(() => picture.complete && picture.naturalWidth > 0, '会話の画像を読み込めません');
    assert(inside(rect(picture), rect(doc.querySelector('.chat'))), '会話の画像が画面からはみ出しました');
    picture.closest('button').click();
    const zoom = doc.querySelector('#media-viewer');
    await waitFor(() => zoom.open && zoom.querySelector('img')?.complete, '画像の拡大表示を開けません');
    assert(Math.abs(rect(zoom).width - rect(doc.querySelector('.chat')).width) <= 2,
      '画像の拡大表示がチャットの幅と一致しません');
    doc.querySelector('#media-viewer-close').click();
    let child = await openLiff();
    const count = () => JSON.parse(JSON.parse(child.querySelector('#events').textContent)[0]).count;
    assert(count() === 0, '初期状態が独立していません');
    child.querySelector('[data-action="bump"]').click();
    await waitFor(() => count() === 1 && !child.querySelector('#send').disabled, 'LIFFの状態を更新できません');
    child.querySelector('[data-action="sync"]').click();
    await waitFor(() => text(doc).includes('LIFFから同期しました: 1') && !child.querySelector('#send').disabled,
      '会話への同期が表示されません');
    child.querySelector('#send').click();
    await waitFor(() => !doc.querySelector('#media-viewer').open && text(doc).includes('入力を受け取りました: 持ち物'),
      '発話して会話へ戻れません');
    assert(doc.activeElement === doc.querySelector('#richmenu [aria-label="LIFFを開く"]'), '閉じた後に操作元へfocusが戻りません');
    checkLayout();
    child = await openLiff();
    assert(count() === 1, '再表示でLIFFの状態が失われました');
    doc.querySelector('#media-viewer-close').click();
    await waitFor(() => !doc.querySelector('.link-viewer-frame'), 'iframeが破棄されません');

    // 既定の「開く」と異なる状態にして、保存が失われた場合も検出する。
    doc.querySelector('#richmenu-toggle').click();
    assert(doc.querySelector('#richmenu').hidden, '再読込前にメニューを閉じられません');
    frame.contentWindow.location.reload();
    await waitFor(() => frame.contentDocument !== doc && frame.contentDocument?.querySelector('#messages')?.textContent.includes('持ち物'),
      '再読込で履歴を復元できません');
    doc = frame.contentDocument;
    await waitFor(() => !doc.querySelector('#richmenu-toggle').hidden
      && doc.querySelector('#richmenu .hotspot'), '再読込でメニューを復元できません');
    assert(doc.querySelector('#richmenu').hidden
      && doc.querySelector('#richmenu-toggle').getAttribute('aria-expanded') === 'false',
      '再読込でメニューの閉じた状態が失われました');
    doc.querySelector('#richmenu-toggle').click();
    assert(!doc.querySelector('#richmenu').hidden, '復元したメニューを開けません');
    checkLayout();
    results.push(`${width}×${height}: 本文${contentWidth.toFixed(1)}px、メニュー→LIFF→同期・発話→再表示・保存、はみ出しなし`);
    resultElement.textContent = `確認中\n${results.join('\n')}`;
  } finally {
    frame.remove();
    await removeTestSave(key);
  }
}

try {
  for (const [width, height] of [[390, 844], [320, 568], [1440, 900], [1280, 720]]) {
    await runViewport(width, height);
  }
  assert(Math.max(...widths) - Math.min(...widths) <= 2, 'スマートフォンとPCで本文の幅が揃っていません');
  resultElement.textContent = `成功: ${results.length}画面サイズ\n${results.join('\n')}`;
  resultElement.dataset.status = 'passed';
} catch (error) {
  resultElement.textContent = `失敗: ${error.message || error}\n${results.join('\n')}`;
  resultElement.dataset.status = 'failed';
}
