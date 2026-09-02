# XStoryBot Webchat client

Webchat API、browser保存、複数tab調停をまとめたframework非依存のheadless ESM clientです。画面描画は含みません。

## 組込み

現時点ではnpmへ公開していないため、repository内のpackageをworkspace／file dependencyとして利用するか、配布物へ同梱します。

```js
import { createWebchatClient } from '@xstorybot/webchat-client';

const client = createWebchatClient({
  apiBaseUrl: 'https://api.example.com',
  bot: 'bot',
});

const unsubscribe = client.subscribe((snapshot) => {
  render(snapshot);
});

await client.initialize();
await client.start();
```

pageを破棄する時は`unsubscribe()`と`client.destroy()`を呼びます。

## 公開API

- `initialize()`: IndexedDBとtab間通知を初期化し、保存済み状態を読み込みます。
- `start()`: stateがなければ新しい会話を開始します。既存stateがあればrequestを送りません。
- `sendText(text)`: 通常textを1 turn送ります。
- `sendPostback(token)`: APIから受け取ったopaque postback tokenを送ります。
- `reset()`: このBotのbrowser保存を消し、新しい会話を開始します。
- `clearHistory()`: 最新stateを残し、表示履歴だけを消します。
- `subscribe(listener)`: immutable snapshotを購読します。
- `getSnapshot()`／`getServerSnapshot()`: client／SSR用snapshotを返します。
- `destroy()`: DB接続とtab間listenerを解放します。

`sendPostback(token, { silent: true })`は、動画完了など利用者が直接押していない自動通知専用です。失敗時にerror表示を残さないため、通常のButton／Quick Replyでは指定しません。

## Snapshotとerror

snapshotには`status`、保存方式、state ID／revision、turn履歴、現在有効なresponse、notice、errorが入ります。完全な型は[index.d.ts](./index.d.ts)を参照してください。

clientはnetwork errorやtimeoutを自動再送しません。手動再試行ではScenarioの外部処理が重複する場合があるため、UI側で利用者へ伝えてください。

## UI例

- [Svelte最小例](./examples/svelte/Webchat.svelte)
- [React hook](./examples/react/useWebchat.js)
- plain DOM参照UI: `static/webchat/`

Svelte例はtext送受信だけを示す最小例です。Quick Reply、Button、media等のrendererは利用するUI systemに合わせて実装してください。
