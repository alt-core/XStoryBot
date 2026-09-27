# XStoryBot Webchat client

Webchat API、browser保存、複数tab調停をまとめたframework非依存のheadless ESM clientです。画面描画は含みません。

## 組込み

repository内のpackageをworkspace／file dependencyとして利用するか、Webchatの配布物へ同梱します。

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

## 保存方式

`createWebchatClient`と`createTrialClient`は、`storage: 'indexeddb' | 'memory'`を受け付けます。既定は`indexeddb`で、ブラウザ保存が利用できない場合は従来どおりメモリーへ切り替えます。

`storage: 'memory'`では、そのclientの進行・履歴をページ内だけに保持し、IndexedDB・localStorage・タブ間通知・共有lockを使いません。同じBotのclientを別タブで作っても独立し、`reset()`や`clearHistory()`は他のclientや過去の永続保存を変更しません。再読み込みや新規タブでは新しく開始し、タブの切替やブラウザの「戻る・進む」によるページ復元では進行を維持します。LIFFも親clientと同じ進行を使います。

`indexedDB`引数はテスト等のfactory差替え用です。メモリー保存の指定には`storage`を使ってください。参照UIは`<html data-webchat-storage="memory">`でも指定でき、[書き出しCLI](../docs/webchat-export.md)の`--storage memory`はこの設定を生成します。参照UIのメニュー開閉もmemory時は永続保存しません。

## 公開API

- `initialize()`: 選択した保存方式を初期化します。indexeddbではtab間通知と保存済み状態の読込も行います。
- `start()`: stateがなければ新しい会話を開始します。既存stateがあればrequestを送りません。
- `sendText(text)`: 通常textを1 turn送ります。
- `requestLiff(app, action)`: snapshotの`liffApps`から選んだページのBotを実行し、保存後にイベント配列を返します。
- `sendPostback(token)`: APIから受け取ったopaque postback tokenを送ります。
- `sendMenu(menu, area, revision)`: 表示中のリッチメニューの領域を操作します。
- `reset()`: 選択した保存先の進行と履歴を消し、新しい会話を開始します。
- `clearHistory()`: 最新stateを残し、表示履歴だけを消します。
- `subscribe(listener)`: immutable snapshotを購読します。
- `getSnapshot()`／`getServerSnapshot()`: client／SSR用snapshotを返します。
- `destroy()`: DB接続とtab間listenerを解放します。

`sendPostback(token, { silent: true })`は、動画完了など利用者が直接押していない自動通知専用です。失敗時にerror表示を残さないため、通常のButton／Quick Replyでは指定しません。

## Snapshotとerror

snapshotには`status`、保存方式、state ID／revision、turn履歴、現在有効なresponse、notice、errorが入ります。完全な型は[index.d.ts](./index.d.ts)を参照してください。

`snapshot.richmenu`は表示中のメニュー（またはnull）です。領域操作は`sendMenu(menu.id, areaIndex, menu.revision)`で送ります。古い定義なら進行せず表示を更新するため、案内を表示して利用者の選び直しを待ってください。

履歴を削除しても`activeResponse`は残ります。画面では、`messages`に含まれない`activeResponse`も表示すると、現在の選択肢から継続できます。同じmessage IDを二重に表示しないようにします。

clientはnetwork errorやtimeoutを自動再送しません。手動再試行ではScenarioの外部処理が重複する場合があるため、UI側で利用者へ伝えてください。

## UI例

- [Svelte最小例](./examples/svelte/Webchat.svelte)
- [React hook](./examples/react/useWebchat.js)
- plain DOM参照UI: `static/webchat/`

Svelte例はtext送受信だけを示す最小例です。Quick Reply、Button、media等のrendererは利用するUI systemに合わせて実装してください。

## LIFFページとの接続

`@xstorybot/webchat-client/liff-host`は親Webchat用の接続処理です。`resolveLiffLink()`で登録ページと表示URLを選び、`attachLiffFrame()`へ`requestLiff()`・通常発話・閉じる処理を渡します。セーブとAPI通信は親Webchatが管理します。

ページ側の接続実装はページのアプリケーションで管理します。HTTP・postMessageの契約と設定は[LIFF連携仕様](../docs/liff-webchat-api.md)を参照してください。同梱の確認ページはprotocol検証用で、ページ側SDKではありません。

## API不要の体験版

`./trial`の`createTrialClient({bot, programUrl})`は、書き出した台本JSONを読み込み、同じ操作APIとブラウザ保存で会話を実行します。書き出しと対応範囲は[体験版ガイド](../docs/webchat-trial.md)を参照してください。通常の`createWebchatClient`の設定や保存形式は変わりません。
