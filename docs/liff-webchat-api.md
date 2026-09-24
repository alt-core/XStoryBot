# LIFFページとWebchatの連携仕様

XStoryBotのLIFF HTTP APIと、Webchatに埋め込んだページとのpostMessage通信を定義します。ページ側の接続実装、LINE SDKの初期化、画面のイベント解釈は、ページのアプリケーションで管理します。

XStoryBotはAPI・セーブ・親Webchatの接続処理を提供します。LINE用のページとWebchat内のページで、同じactionとイベント列を使えます。

## 1. 操作と状態

会話用BotをA、ページの操作対象BotをBとします。同じ進行を共有する場合はA自身をBに指定できます。

| 操作 | LINEでの接続先 | Webchatでの接続先 |
| --- | --- | --- |
| 内部actionの実行 | LIFF HTTP API | 親へ`request`を送信 |
| 利用者の発話として送信 | LINE SDKの`sendMessages` | 親へ`sendText`を送信 |
| ページを閉じる | LINE SDKの`closeWindow` | 親へ`close`を送信 |

内部actionの応答はページが解釈するイベント列です。チャットの吹き出しへは表示しません。情報を表示するactionでも、シナリオが状態を変更すれば保存されます。

Webchatでは親が最新セーブ・API通信・保存・同時操作の調停を担当します。iframeへ`state_token`を渡さず、ページ側にも別のセーブを作りません。LINEの認証情報をWebchatへ送る必要はありません。

## 2. LINEのLIFF HTTP API

LINE SDKの初期化と必要なログインを済ませ、`liff.getAccessToken()`で取得したtokenを使います。

```http
POST <apiBaseUrl>/liff/<bot>/message
Authorization: Bearer <access_token>
Content-Type: application/json

{"action":"open"}
```

`action`は文字列です。空文字も受理します。ページ側ではtrimや接頭辞の付加をせず、サーバーが設定済みの`action_prefix`（既定`##liff.`）を付けます。認証tokenをURLに載せません。

成功応答の`message`は、イベント配列をJSON文字列にしたものです。

```json
{
  "code": 200,
  "result": "Success",
  "message": "[\"{\\\"event\\\":\\\"show\\\",\\\"id\\\":\\\"box_1\\\"}\"]"
}
```

HTTP成功に加えて`result === 'Success'`を確認し、`message`をJSON parseして配列であることを確認します。`text/plain`の応答も扱ってください。配列の要素は変換しません。要素をさらにJSONとして解釈するかどうかは、ページとシナリオの取り決めです。

HTTP 200でも`Failure`／`Error`なら成功にしません。通信断や不正な応答では、処理済みかもしれないため同じ操作を自動再送しません。ページ側は通信・本文読込みのtimeoutと、処理中の二重操作抑止を実装します。

発話は`liff.sendMessages([{type: 'text', text}])`で送ります。その成功はSDKの送信成功で、Botの返信や非同期転送の完了ではありません。送信先はLIFFを開いたトークルームです。必要な起動条件と`chat_message.write`の許可は[LINEの仕様](https://developers.line.biz/en/reference/liff/#send-messages)に従ってください。外部ブラウザでは閉じる動作を保証できません。[closeWindowの仕様](https://developers.line.biz/en/reference/liff/#close-window)

### 認証とCORS

```yaml
- type: liff
  params:
    action_prefix: '##liff.'
    login_channel_id: '1234567890'
    allow_origin:
      - https://pages.example.com
      - https://preview.example.com
    ignore_unhandled_action: false
```

`login_channel_id`はLIFFを登録した**LINE Loginチャネル**のIDです。Messaging APIチャネルIDとは区別します。実LINEのAPIでは必須で、未設定なら503で停止します。Webchat内だけで使うLIFF interfaceには不要です。

サーバーはtokenの発行先と有効期限を検証してからprofileを取得します。各LINE認証要求のtimeoutは10秒で、tokenのキャッシュは行いません。tokenや認証URLをエラーログへ出しません。[LINE公式の検証手順](https://developers.line.biz/en/docs/liff/using-user-profile/)

`allow_origin`は文字列（`*`を含む）かoriginの配列です。配列では要求のOriginを照合し、`Vary: Origin`を付けます。これはLINE側のLIFF API設定です。Webchatのiframeを使うために、このCORSを広げる必要はありません。

`ignore_unhandled_action: true`では入口actionに該当するシナリオがなければ空配列を返します。既定のfalseでは従来のエラー応答へ進みます。内部ジャンプの誤りは無視しません。LINE／Webchat双方に適用します。

## 3. ページとBotの登録

Aの`webchat.params.liff_apps`へページ名・実ページURL・Bを登録します。Bには有効な`webchat`と`liff`の両interfaceが必要です。Webchatの設定は、署名付き状態で実行するScenario URIと互換epochにも使います。署名鍵・許可origin等は[Webchatガイド](./webchat.md)に従って用意してください。

```yaml
bots:
  story:
    interfaces:
      - type: webchat
        params:
          liff_apps:
            menu:
              bot: menu
              url: https://pages.example.com/menu/
              liff_id: YOUR_LIFF_ID
              match: prefix
  menu:
    interfaces:
      - type: liff
        params:
          allow_origin: https://pages.example.com
      - type: webchat
        params:
          scenario_uri: s3://YOUR_PRIVATE_BUCKET/scenario/YOUR_MENU_DIGEST
          scenario_compatibility_epoch: menu-v1
```

会話とページで同じ進行を共有する場合は、`liff_apps.menu.bot: story`とし、story自身に`liff`も設定します。フラグだけでなく現在のシーンも共有します。LIFF操作中の`$$service_name`は両環境とも`liff`です。

`match`の既定は`exact`で、同じorigin・pathのリンクを対象とします。queryとfragmentは一致判定に使わず、ページへ引き継ぎます。`prefix`では登録パスとその配下を対象にします。`/menu/`が`/menus/`へ誤一致することはありません。複数候補では最長の登録パスを選びます。

`liff_id`は任意です。指定すると、例えば`https://liff.line.me/YOUR_LIFF_ID/words/?id=1#item`を`https://pages.example.com/menu/words/?id=1#item`へ読み替えます。登録URLのqueryへリンク側のqueryを追加し、深いリンクにもmatchの規則を適用します。LINEは元のLIFF URLで起動します。[LINEのリダイレクト仕様](https://developers.line.biz/en/docs/liff/opening-liff-app/)

LINE／WebchatでURL構成を変えたい場合は、[定数上書き](./scenario-authoring.md#定数とリッチメニュー)も使えます。実ページURLへ一律に統一すると、LINE側の`sendMessages`に必要な起動条件を満たさなくなることがあります。

`openExternalBrowser=1`は登録済みでも外部タブを優先し、読替え前のURLを開きます。この場合、Webchatの接続やセーブを渡しません。通常の登録ページではiframeを使います。参照UIの登録済みiframeはpopupを許可し、外部タブへsandbox制限を継承しません。ページ側では外部リンクに`rel="noopener noreferrer"`等を指定してください。

## 4. WebchatのpostMessage protocol

親は表示URLへ`xsb_client=webchat`を付けます。これは接続方法の指定で、認証情報ではありません。ページ側はLINE SDKを初期化せず、設定済みの親originへ接続します。接続に失敗してもLINEへ自動切替しません。

親originはページの運営者が設定する信頼先です。URLパラメータや受信messageから無条件に採用せず、`postMessage`の送信先へ具体的なoriginを指定します。ページ内を遷移するときは、アプリ内リンクへ起動指定を引き継ぎ、新しいページで接続し直します。[postMessageの仕様](https://developer.mozilla.org/en-US/docs/Web/API/Window/postMessage)

全messageに次の値を付けます。

```json
{"protocol":"xstorybot-liff","version":1}
```

| 方向・type | 追加するフィールド | 意味 |
| --- | --- | --- |
| 子→親 `connect` | `id: string` | 接続を開始する |
| 親→子 `connected` | `id, session: string` | 要求のidと新しい接続識別子を返す |
| 子→親 `invoke` | `id, session, method, args` | `method: 'request'`と`args: {action: string}`、または`method: 'sendText'`と`args: {text: string}` |
| 親→子 `result` | `id, session, ok: true, value` | requestはイベント配列、sendTextはnull |
| 親→子 `result` | `id, session, ok: false, error` | errorは`{code, message?, requestId?}` |
| 子→親 `close` | `session` | 親にviewerを閉じるよう通知する。応答なし |
| 子→親 `disconnect` | `session` | 現在の接続を解放する。応答なし |

`id`はページ側が要求ごとに発行し、対応する応答だけを受け取ります。`session`は親が接続ごとに発行し、その後のmessageへ付けます。重複実行を防ぐreceiptではありません。

親は現在のiframeの`contentWindow`と登録originを照合し、子は`window.parent`と設定済み親originを照合します。protocol・version・id・sessionも確認します。新しい接続を作ると、以前の接続へ結果を送りません。同じorigin内でページを移動しても、操作対象Botは最初に開いた時のままです。別の登録アプリは親で開き直します。

ページを移るなどして同じiframeから接続し直した場合、親は旧接続を無効にし、その接続で開始済みの操作が完了してから`connected`を返します。待機中にさらに接続要求が来た場合は最新の1件だけを残し、frameを破棄した場合は応答しません。操作を再送・再実行する仕組みではなく、`connected`は前の操作の成功も保証しません。

ページ側の接続timeoutには、この待ち時間も含まれます。サーバーの処理期限（既定29秒）は変更可能で、通信・保存を含む親の待機時間の上限ではありません。ページ側は適切な待ち時間と読み込み表示を用意してください。

親は同じ接続で処理中の追加要求を`busy`で拒否し、操作を貯める自動queueは作りません。通常発話との競合などでも`busy`になる場合があります。意図した連続操作は前の結果を待ってから送ってください。

閉じる・切断・timeoutは、開始済み処理の取消しではありません。親は成功したセーブの採用を続け、古い接続へイベントを送りません。ページ側も期限切れidへの後着応答を演出に使わず、結果不明の操作を自動再送しません。

### エラー

| code | ページ側の扱い |
| --- | --- |
| `invalid-input` | action／textの型やmethod等を修正する |
| `unavailable` | 認証・実行環境・許可設定を確認する |
| `busy` | 当該要求は未実行。操作が引き続き必要なら、待機後に上限付きで再試行できる |
| `state-refreshed` | 別tabとの競合。旧結果を表示せず、最新状態で画面を再構成する |
| `request-failed` | APIが失敗を返した。内容に応じて案内する |
| `outcome-unknown` | 通信や応答の問題で結果不明。実行済みの可能性がある |

`busy`の再試行は初期表示の取得など必要な操作に限定し、待機時間や回数に上限を設けます。成功・別のエラー・ページ離脱で終了し、すべてのactionを自動再送する共通処理にはしません。Webchatの`busy`はAPIへ送る前の拒否なので、拒否された試行によるシナリオの実行はありません。

通信断やtimeoutによる結果不明時には再送せず、ページを開き直すなどの復帰導線を用意します。前のページの操作が完了したかどうかを、別の要求への`busy`応答から判断することはできません。画面再構成用actionもシナリオ側で明示し、繰返しで進行を重複させない内容にします。表示演出とセーブを一体で巻き戻す機構はありません。

## 5. 親Webchatの組込み

`webchat-client/liff-host.js`は親専用です。`resolveLiffLink(href, apps)`は`{app, url}`またはnullを返し、`findLiffApp()`はappだけを返します。appsはheadless clientのsnapshotにある`liffApps`を使います。

読替え後URLに`xsb_client=webchat`を付けてiframeへ設定し、次のように親の操作へ接続します。

```js
const connection = attachLiffFrame(frame, {
  app,
  request: action => client.requestLiff(app, action),
  sendText: text => client.sendText(text),
  close: closeViewer,
});
// viewerを閉じる・差し替える時に呼ぶ。
connection.destroy();
```

APIへ送る`app.url`は読替え後URLではなく登録値です。Botやセーブを子のmessageから採用しません。送信中に新しいLIFFページを開かず、通常発話とLIFF操作を同じclientで調停してください。参照UIとexport版には組込み済みです。

親→サーバーは既存の`/api/webchat/v1/bots/<A>/turn`を使います。入力は`{type: 'liff', app, app_url, action}`で、セーブは親が添えます。[OpenAPI](./webchat.openapi.yaml)に従い、iframeへセーブや署名鍵を渡しません。親のoriginから通信するため、LIFFページをWebchat APIのCORS許可先へ追加する必要はありません。ページ配信元は親からの埋込みをCSPの`frame-ancestors`等で許可してください。

## 6. セーブと転送

状態のキーは`state_namespace`で、未指定ならBot名です。独立進行するA/Bは別namespaceにします。操作を受けると署名と対象Botを検証し、保存状態をメモリへ復元して実行します。

同期forwardは現在のBotを完了してから転送先を順に実行し、最新状態を引き継ぎます。開始actionを含む100回の上限と、連鎖全体の実行期限があります。更新しなかったBotの状態も保持してセーブ一式を返し、親が保存してからイベントを子へ返します。

Bだけの操作では、Aの履歴や選択肢を置き換えません。転送でAが進行した場合はAの応答も更新し、内部転送を利用者の発話として追加しません。古い画面からのactionが現在のシーンで有効かはシナリオで判断します。ページはBot名やセーブの書換APIを持たず、通常のLIFF actionにpostback tokenを要求しません。Webchatの会話内選択肢は既存の署名検証を維持します。

`@forward Bot名 action [interface]`の同期転送先はAと登録したB群です。省略時はAへ`webchat`、Bへ`liff`を使います。明示指定できるのはAの`webchat`と各Botの`liff`です。転送actionへ接頭辞は自動付加しません。LIFFのイベントは実行順に連結します。通常会話からBへ転送した場合は状態を更新し、イベントを吹き出しへは表示しません。

LINE上のLIFFからの非同期転送では、省略時は転送先の`line`を使い、応答があればpushします。`liff`を明示する場合は転送先にも同interfaceが必要です。そのJSON応答はtask結果であり、元ページへ返りません。指定は後続へ自動継承しません。[転送の書式](./scenario-authoring.md)を参照してください。

複数tabのWeb Locksと保存時のstate ID比較を維持し、競合結果のイベントは成功として配りません。親の「最初から」は状態一式をリセットし、履歴消去はセーブを残します。連携Botの追加や未実行Botの変更・削除では進行を維持します。実行済みBotの非互換変更・削除はリセットが必要です。[移行ガイド](./migration.md)を参照してください。

## 7. 検証

[同梱の確認ページ](../webchat-client/examples/liff/index.html)は、WebchatのpostMessageを直接送受信する検証用ページです。LINE用接続や再利用するSDKは含みません。`tools/webchat_dev_server.py`を起動し、チャットで`liff`と入力して試せます。実シナリオでは`open`・`bump`・`sync`を用意してください。別originではHTMLの`data-parent-origin`に信頼先を固定します。

```sh
./test.sh
node --test webchat-client/test.mjs webchat-client/liff-host.test.mjs tests/webchat_ui_logic.test.mjs
```

親のprotocolテストは人工messageで接続、送り元照合、二重操作、再接続、切断後の結果を検証します。ページ側のLINE SDK・HTTP adapter・UIのテストはページ側アプリケーションで行います。
