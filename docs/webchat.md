# Webchatガイド

Webchatは、Player状態を署名付きtokenとしてbrowserへ返す同期interfaceです。Webchat turnではPlayer、More、履歴をDynamoDBへ保存せず、固定したScenarioをS3から初回だけ読み、その後はprocess内で再利用します。

## 設計上の性質

- stateとpostbackはHMAC-SHA256で改ざんを検出します。暗号化はしないため内容は利用者から読めます。
- tokenに時間による有効期限はありません。Scenario更新時のstate互換性はScenario運営側で維持し、非互換更新時だけcompatibility epochを変更します。
- 古いstateのreplay、同じstateからのfork、複数端末へのcopyを防ぎません。
- replay、並行request、手動再試行では、Webhook、POSTJSON、GETJSON、`@log`等が重複実行される場合があります。clientの自動再送、server-side dedupe、receipt、outboxは実装しません。
- state、token、入力、履歴へWebchat独自のbyte数・件数上限を設けません。完成request／responseにはproviderの上限が適用されます。

## AWS設定

`template.aws.yaml`の`WebchatEnabled`を`true`にし、次のparameterを指定します。

- `WebchatImageUri`: Webchatだけを個別更新するimage URI。空の場合は共通`ImageUri`を使用
- `WebchatSigningKey`: Webchat専用の32 byte以上のbase64url鍵一件
- `WebchatScenarioUri`: `s3://.../scenario/<content digest>`形式のimmutable Scenario参照
- `WebchatCompatibilityEpoch`: Scenario運営側が管理する非秘密の互換性識別子
- `WebchatAllowedOrigins`: 同一origin参照UI以外のclientを置くHTTPS origin。複数の場合はカンマ区切り。API自身のoriginはtemplateが固定設定するため列挙不要
- `WebchatExternalHttpOrigins`: Scenarioから呼び出せる外部HTTPS origin。不要なら空
- `WebchatMediaOrigins`: 実行時展開mediaを限定するHTTPS origin。不要なら空
- `WebchatThrottleRate`／`WebchatThrottleBurst`: 無認証routeの運用上のthrottle

新規stackでは、まずWebchat無効のままbucket／builderを作成し、Scenario artifactをbuildしてから、そのURIを指定する二回目のdeployでWebchatを有効にします。

`deploy_aws.sh`を使う場合は、同名の`XSBOT_WEBCHAT_*`環境変数を読み取り、上記parameterへ渡します。追加originと外部HTTP／media originは空のまま省略できます。

署名鍵は通常のScenario更新やdeployでは維持します。鍵漏洩時は新しい鍵を注入したLambda versionへaliasを切り替え、旧token全体の失効を受け入れます。Webchat Lambdaは署名鍵を環境変数としてversionへ固定し、起動時やturnごとにParameter StoreまたはDynamoDBを読みません。

## Scenario更新

1. 通常のbuilderでScenarioをimmutable artifactとしてS3へ保存します。
2. 代表的な既存stateで新しいScenarioとの互換性を確認します。
3. 互換更新では同じcompatibility epochを維持します。非互換更新だけepochを変更します。
4. `update_webchat_scenario.sh`を実行します。変更内容を確認して同じ画面で承認すると、新しいLambda versionが発行され、`live` aliasが切り替わります。

Webchat processは固定URIのdigestを検証してScenarioを読みます。DynamoDB上の最新Scenario pointerは参照しません。

`update_webchat_scenario.sh`は指定したS3 artifactの存在確認、変更内容の表示、確認後のstack更新、完了待ちまでを一度に行います。Webchat Lambda以外の変更を検出した場合はchange setを削除して停止します。compatibility epoch変更とimage build／pushは行いません。非互換更新ではこのscriptを使わず、epoch変更を含むchange setを別途レビューしてください。`deploy_aws.sh`はcodeを含む通常deploy用です。

## APIとclient

公開turn APIは次の一つです。

```text
POST /api/webchat/v1/bots/{bot}/turn
```

完全なrequest／response契約は[OpenAPI文書](./webchat.openapi.yaml)を参照してください。state tokenとpostback tokenはopaque stringとして扱い、URL、cookie、logへ出さないでください。

`webchat-client/`にはruntime dependencyを持たないheadless ESM clientとTypeScript declarationがあります。framework固有の通信・保存処理は作らず、SvelteとReactから同じcoreを使います。公開APIとsnapshotは[client README](../webchat-client/README.md)を参照してください。

```js
import { createWebchatClient } from '@xstorybot/webchat-client';

const client = createWebchatClient({
  apiBaseUrl: 'https://api.example.com',
  bot: 'bot',
});

await client.initialize();
await client.start();
await client.sendText('こんにちは');
```

- Svelteのtext最小例: `webchat-client/examples/svelte/Webchat.svelte`
- React hook: `webchat-client/examples/react/useWebchat.js`
- plain DOM参照UI: `GET /chat/{bot}`

npm等への公開は別作業です。現時点ではrepository内のpackageをworkspace／file dependencyとして取り込むか、配布物へ同梱してください。

参照UIのモックと回帰テストは[開発用確認手順](./webchat-development.md)へ分けています。

## browser保存と複数tab

headless clientはstateと履歴をIndexedDB transactionで同時に更新します。Web Locksが利用できるbrowserでは同じ会話のnetwork requestも直列化し、利用できない場合はIndexedDBのstate ID比較で先にcommitされた応答だけを採用して履歴破損を防ぎます。

別tabで先に進行していた場合、後から操作したtabは最新の履歴へ更新し、入力の再確認を促します。BroadcastChannelは更新通知にだけ使い、利用できない場合はlocalStorageへ小さな更新beaconだけを書きます。state tokenと履歴本文をlocalStorageへ保存しません。

IndexedDBを利用できない環境ではmemory-onlyへ切り替え、pageを閉じると進行が失われることをsnapshotの`notice`で通知します。private browsingではsession終了時の消去、browserのstorage evictionでは保存内容の消去が起こり得ます。

browser quotaへ実際に到達した場合だけ、最新stateの保存を優先して古い履歴から削除し、その事実を`notice`へ表示します。事前に独自の履歴件数／byte数上限は設けません。

## 初期対応範囲

Webchatで許可する既存commandは、通常の状態遷移・条件・表示に加え、`@log`、Webhook、POSTJSON、GETJSONです。表示はtext、sender、image、video、audio、Button、Confirmの単純表示、Imagemap、Quick Reply、Moreへ対応します。

参照UIではimageとvideoをチャット内viewerで開きます。対応browserではnative dialogを使い、非対応browserでは同じ表示を固定overlayへfallbackします。videoはnative controlsを表示せず、thumbnailのtapから直接再生し、動画面のtapまたはSpaceで再生／一時停止を切り替えます。

URI actionの通常HTTPS URLはsandbox付きiframeでチャット内viewerへ表示します。query parameterに`openExternalBrowser=1`があるHTTP／HTTPS URLは新しいtabで開き、`tel:`は端末の標準処理へ渡します。外部指定のない`http:` URLとその他のschemeは開きません。リンク先が`X-Frame-Options`またはCSPでiframeを拒否する場合は、ScenarioのURLへ`openExternalBrowser=1`を付けてください。

`@video`の第三引数へ`#`または`*`で始まる内部actionを指定した場合は、再生完了後にpostback相当として一つのvideo表示につきpage内で一度だけ実行します。別turnの送信中に完了した場合は、そのturnが終わってから実行します。

Imagemapのactionは画像上のfocus可能なhotspotとして表示し、支援技術向けの`aria-label`を付けます。画像内の隠し要素や物語上の選択肢を一覧化しないため、可視fallback buttonは自動生成しません。画像が読めない利用者にも導線を明示する必要があるScenarioでは、画像のaltまたは前後のtextで必要な説明を用意してください。

次は初期対象外です。

- `@delay`、`@forward`
- group操作
- server push
- Carousel／Panel
- Flex

Rich menuはWebchatではno-opです。未知commandはdefault denyで`bot-not-web-compatible`を返します。対象Scenarioの導線で非対応機能が必要になった場合は、共通機能を増やす前にScenario導線の単純化または対象Botだけのserver保存型を検討してください。

## ログ

検証済みturnはrequest ID、conversation ID、state ID、revision、action、Scenario revision、scene、生成件数で追跡できます。外部HTTPはmethod、origin、status、byte数を記録します。

raw state／postback token、署名鍵、認証header、API token、秘密設定は記録しません。例外は型とstack frameを記録しますが、秘密値を含み得る例外messageはWebchatのerror logへ出しません。
