# Webchatガイド

Webchatは、Player状態を署名付きtokenとしてbrowserへ返す同期interfaceです。Webchat turnではPlayer、More、履歴をDynamoDBへ保存しません。Scenarioは固定URIを使う方式と、ビルド済みの最新版に追従する方式を選べます。どちらも読み込んだScenarioをprocess内で再利用します。

Webchatは`app_webchat:app`を起動する専用processで提供します。Dockerでは`XSBOT_APP_MODULE=app_webchat:app`を指定します。通常APIの`app:app`にはWebchatのturn APIを登録しません。

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
- `WebchatScenarioUri`: 固定方式で使う`s3://.../scenario/<content digest>`形式のimmutable Scenario参照
- `WebchatFollowLatestScenario`: 既定`false`。`true`なら対象Botの最新版に共通キャッシュで追従する。固定URIは不要
- `WebchatCompatibilityEpoch`: Scenario運営側が管理する非秘密の互換性識別子
- `WebchatAllowedOrigins`: 同一origin参照UI以外のclientを置くHTTPS origin。複数の場合はカンマ区切り。API自身のoriginはtemplateが固定設定するため列挙不要
- `WebchatExternalHttpOrigins`: Scenarioから呼び出せる外部HTTPS origin。不要なら空
- `WebchatMediaOrigins`: 実行時展開mediaを限定するHTTPS origin。不要なら空
- `WebchatThrottleRate`／`WebchatThrottleBurst`: 無認証routeの運用上のthrottle

固定方式の新規stackでは、まずWebchat無効のままbucket／builderを作成し、Scenario artifactをbuildしてから、そのURIを指定する二回目のdeployでWebchatを有効にします。追従方式では固定URIを指定せず有効化できますが、初回ビルドが終わるまではturnがエラーになります。

`deploy_aws.sh`を使う場合は、設定された`XSBOT_WEBCHAT_*`環境変数だけを上記parameterへ渡し、更新時の未設定項目は前回値を維持します。既に有効なstackの通常更新では`XSBOT_WEBCHAT_ENABLED`を未設定にします。明示的に`true`を指定するときは鍵が必要で、固定方式ならScenario URIも必要です。追従方式の有効化には`XSBOT_WEBCHAT_FOLLOW_LATEST_SCENARIO=true`を指定します。`XSBOT_WEBCHAT_ENABLED=false`を指定するとWebchatを無効化します。追加originと外部HTTP／media originは明示した空文字で消去できます。鍵・epoch・throttle、固定方式のScenario URIに空文字は指定できません。imageは有効状態の指定を省略しても更新されます。

署名鍵は`deploy_aws.sh`の引数として渡すため、`sam deploy`の実行時に操作者の端末へ表示されます。個人の端末で実行する運用を前提にしています。署名鍵は通常のScenario更新やdeployでは維持します。鍵漏洩時は新しい鍵を注入したLambda versionへaliasを切り替え、旧token全体の失効を受け入れます。Webchat Lambdaは署名鍵を環境変数としてversionへ固定し、Parameter Storeを読みません。追従方式だけ、最新URIをDynamoDBから読みます。IAMは`GLOBAL`項目への`GetItem`に限定し、Player状態の読取り・書込みは許しません。

Botごとのinterface設定で方式を上書きする場合、追従するBotが一つでもあればAWSの`WebchatFollowLatestScenario`を`true`にして読取権限を付けます。固定方式のBotには、そのBotのScenario URIを指定します。

## Scenario更新

1. 通常のbuilderでScenarioをimmutable artifactとしてS3へ保存します。
2. 代表的な既存stateで新しいScenarioとの互換性を確認します。
3. 互換更新では同じcompatibility epochを維持します。非互換更新だけepochを変更します。
4. 固定方式では`update_webchat_scenario.sh`を実行します。変更内容を確認して同じ画面で承認すると、新しいLambda versionが発行され、`live` aliasが切り替わります。追従方式ではビルドが最新URIと公開世代を公開し、通常は共通キャッシュの確認間隔後の要求で反映されます。通常の更新にstack更新は不要です。

`webchat.params.follow_latest_scenario`は既定`false`です。GCP等でも、この設定を`true`にすると既存StateStoreにある実行対象Bot自身の最新URIと公開世代を、LINE等と同じ処理で確認します。`options.scenario_check_interval`は共通で、既定`10`秒、`0`なら毎回確認します。URIが変わったときだけ本体を読み直し、同じURIの再公開は本体を再利用します。固定方式はlatest pointerの確認もDB初期化も行いません。どちらもScenarioのdigestを検証します。

セーブには実行したBotごとの公開世代を署名して記録します。セーブがキャッシュ以下の世代なら再確認せず、キャッシュより新しい場合だけ期限を待たず再確認します。内容hashの大小で新旧を判断しません。公開世代のない既存セーブや、固定方式で進んだセーブを追従方式で使う場合は、409を返して「最初から」を求めます。

追従方式から固定方式へ切り替える場合は、使用するScenario URIも指定してください。AWSテンプレートは前回値を含む実際のparameterで、固定方式のURIが空でないことを検査します。

要求内ではScenario・URI・revision・公開世代を保持し、同じBotへの同期forwardも同じ版を使います。連携Botは最初に実行する時に選んだ版をその要求内で再利用します。全Botの更新を一度に切り替える保証ではありません。再確認や本体取得に失敗した場合はエラーを返し、未確認の旧版へのfallbackやclientのセーブ更新は行いません。互換epochの管理は運営側の責任のままです。非互換の変更やエンジンのコード・設定変更では、通常のデプロイを行ってください。

取得失敗も共通の確認間隔だけキャッシュします。同じ世代以下のセーブは、読取りが復旧しても既定では最大10秒エラーを返します。失敗時に確認した世代より新しいセーブだけ期限前に再確認します。

公開世代の導入時はreaderとbuilder・CLIを一緒に更新し、旧writerを混在させずに再ビルドしてください。公開世代は互換性の自動判定ではありません。保存先の作り直しで世代を失った場合や、公開先を旧URIへ戻す場合も、運営側で互換epochとセーブの扱いを確認してください。

`update_webchat_scenario.sh`はデプロイ済みstackの`Original`テンプレートとparameter名を取得し、`WebchatScenarioUri`だけを変更します。SAMが付けるMetadataを保ち、他のparameterは`UsePreviousValue`で維持するため、署名鍵などの値を表示する必要はありません。YAMLとJSONのテンプレートに対応します。操作者には既存の更新権限に加えて`cloudformation:GetTemplate`の読取権限が必要です。

指定したS3 artifactの存在確認、change setの表示、確認後のstack更新、完了待ちまでを一度に行います。Webchat Function・version・alias以外の変更があれば、change setを削除して停止します。確認で`n`を選べばstackは更新されません。compatibility epoch変更とimage build／pushは行いません。非互換更新ではこのscriptを使わず、epoch変更を含むchange setを別途レビューしてください。

手元の`template.aws.yaml`は使用しないため、未適用のworker・queue・alarm等の変更や、新しいparameterはシナリオの切替へ入りません。基盤とコードの更新は`deploy_aws.sh`で明示的に行ってください。新しいエンジン機能を使うシナリオへ切り替える場合は、先に対応するコードをデプロイします。

この補助スクリプトは、テンプレート本文の直送上限である51,200 byteまでを扱います。超える場合はchange setを作る前に停止し、S3の`TemplateURL`を使った更新を案内します。テンプレートの一時ファイルは非公開の権限で作り、正常終了・失敗・中止時に削除します。

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

既存UIは[静的ファイルへ書き出し](webchat-export.md)、APIとは別のhosting・任意のサブパスへ配信できます。独自の画面にはheadless clientをSvelteやReact等へ組み込み、配信元originを`allowed_origins`へ登録してください。同一originの参照UI（`/chat/{bot}`、`/static/webchat/*`、`/webchat-client/*`）も引き続き使えます。このrouteからの静的配信はWebchat Lambdaの負荷になります。

npm等への公開は別作業です。現時点ではrepository内のpackageをworkspace／file dependencyとして取り込むか、配布物へ同梱してください。

参照UIのモックと回帰テストは[開発用確認手順](./webchat-development.md)へ分けています。

LINEとWebchatでLIFFページのコードを共用できます。[LIFF連携仕様](./liff-webchat-api.md)に、LIFF用Botの状態遷移、イベント列、メニューからの発話、連携Botの設定をまとめています。

## browser保存と複数tab

headless clientはstateと履歴をIndexedDB transactionで同時に更新します。Web Locksが利用できるbrowserでは同じ会話のnetwork requestも直列化し、利用できない場合はIndexedDBのstate ID比較で先にcommitされた応答だけを採用して履歴破損を防ぎます。

別tabで先に進行していた場合、後から操作したtabは最新の履歴へ更新し、入力の再確認を促します。BroadcastChannelは更新通知にだけ使い、利用できない場合はlocalStorageへ小さな更新beaconだけを書きます。state tokenと履歴本文をlocalStorageへ保存しません。

保存形式は最新応答を含むheadだけを読みます。それより前のclientが保存した進行は「保存した進行の形式が不正」となるため、「最初から」で再開してください。

IndexedDBを利用できない環境ではmemory-onlyへ切り替え、pageを閉じると進行が失われることをsnapshotの`notice`で通知します。private browsingではsession終了時の消去、browserのstorage evictionでは保存内容の消去が起こり得ます。

browser quotaへ実際に到達した場合だけ、最新stateの保存を優先して古い履歴から削除し、その事実を`notice`へ表示します。事前に独自の履歴件数／byte数上限は設けません。

## 初期対応範囲

Webchatで許可する既存commandは、通常の状態遷移・条件・表示に加え、`@log`、Webhook、POSTJSON、GETJSONです。表示はtext、sender、image、video、audio、Button、Confirmの単純表示、Imagemap、Quick Reply、Moreへ対応します。

参照UIではimageとvideoをチャット内viewerで開きます。対応browserではnative dialogを使い、非対応browserでは同じ表示を固定overlayへfallbackします。videoはnative controlsを表示せず、thumbnailのtapから直接再生し、動画面のtapまたはSpaceで再生／一時停止を切り替えます。

URI actionの通常HTTPS URLはsandbox付きiframeでチャット内viewerへ表示します。query parameterに`openExternalBrowser=1`があるHTTP／HTTPS URLは新しいtabで開き、`tel:`は端末の標準処理へ渡します。外部指定のない`http:` URLとその他のschemeは開きません。リンク先が`X-Frame-Options`またはCSPでiframeを拒否する場合は、ScenarioのURLへ`openExternalBrowser=1`を付けてください。

`@video`の第三引数へ`#`または`*`で始まる内部actionを指定した場合は、再生完了後にpostback相当として一つのvideo表示につきpage内で一度だけ実行します。別turnの送信中に完了した場合は、そのturnが終わってから実行します。

Imagemapのactionは画像上のfocus可能なhotspotとして表示し、支援技術向けの`aria-label`を付けます。画像内の隠し要素や物語上の選択肢を一覧化しないため、可視fallback buttonは自動生成しません。画像が読めない利用者にも導線を明示する必要があるScenarioでは、画像のaltまたは前後のtextで必要な説明を用意してください。

`liff_apps`を設定したWebchatでは、登録Botへの`@forward`を同じリクエスト内で順に実行します。会話とLIFFの状態を一式で保存し、LINE用の非同期転送は維持します。詳細は[LIFF連携仕様](./liff-webchat-api.md)を参照してください。

次は初期対象外です。

- `@delay`
- group操作
- server push
- Carousel／Panel
- Flex

`allowed_commands`による追加許可でも、`@delay`とgroup操作は実行できません。`@forward`はLIFF連携の同期実行が用意された場合だけ使えます。Webchatの署名セーブを、通常の非同期タスクへ渡して実行することはできません。

リッチメニューは[Bot単位の定義](./richmenu.md)を表示し、開閉・message／postback／URI操作に対応します。生のLINE IDによる指定はno-opです。未知commandはdefault denyで`bot-not-web-compatible`を返します。対象Scenarioの導線で非対応機能が必要になった場合は、共通機能を増やす前にScenario導線の単純化または対象Botだけのserver保存型を検討してください。

## ログ

検証済みturnはrequest ID、conversation ID、state ID、revision、action、Scenario revision、scene、生成件数で追跡できます。外部HTTPはmethod、origin、status、byte数を記録します。

raw state／postback token、署名鍵、認証header、API token、秘密設定は記録しません。例外は型とstack frameを記録しますが、秘密値を含み得る例外messageはWebchatのerror logへ出しません。

会話とLIFFが同じ進行を共有する場合は、`liff_apps.<name>.bot`に会話Bot自身を指定できます。そのBotへ`webchat`と`liff`を設定し、LIFF操作では`$$service_name`が`liff`になります。フラグだけでなく現在のシーンも共有します。複数ページの登録、LINE用URLの読替え、LINE側の認証設定は[LIFF連携ガイド](./liff-webchat-api.md)を参照してください。
