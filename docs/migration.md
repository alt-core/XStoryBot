# XStoryBot移行ガイド

この文書は、GAE／Python 2構成またはGCP専用構成から、現在のPython 3.11版へ移行する場合に必要な作業をまとめています。

## 1. 別環境へ先にデプロイする

既存環境を直接上書きせず、Cloud RunまたはAWSへ別の検証環境を作成します。現在のコードは、GCPとAWSのいずれか一方をプロセス起動時に選択します。

GAE用のデプロイ設定は含まれていません。GCPではAPI用・ビルダー用の2つのCloud Runサービスと3つのCloud Tasksキューを用意します。AWSでは`template.aws.yaml`と`deploy_aws.sh`を使用します。

## 2. 設定を作り直す

依存は`requirements-gcp.txt`と`requirements-aws.txt`に分かれ、Dockerイメージには接続先の依存だけが入ります（`--build-arg XSBOT_CLOUD_PROVIDER`）。TwilioとPusherは任意pluginになり、`settings.yaml`の`plugins`に書く場合だけ`requirements-optional.txt`を追加します。Google Sheetsの読み込みは`google-auth`のservice account認証でSheets API v4を直接呼ぶため、`google-api-python-client`は不要になりました。timezoneは標準ライブラリのzoneinfoで扱い（`pytz`は不要）、`options.timezone`などの値はこれまでどおりIANA名（`Asia/Tokyo`、`UTC`）です。以前の`settings.yaml.template`を写した設定には両pluginが残っているので、使わないなら`plugins`から外してください。残したまま依存が無いと、起動時に不足しているpackage名を示して停止します。

`settings.yaml.template`を初回に`settings.yaml`へコピーし、Botとpluginを設定します。ローカル実行とDockerはこの`settings.yaml`を使います。コンテナ用にtemplateを直接編集していた場合は、その内容を非公開の`settings.yaml`へ移してください。ファイルが無い場合や空の場合はビルドが停止します。秘密値は直書きせず、`.env.template`を参考に実行環境から渡してください。実設定は公開Gitへ追加せず、`.env`や認証情報ファイルはDockerイメージへ含めないでください。

`plugins.line`の遅延判定には`line_abort_duration`と`line_abort_duration_dont_break`を使います。以前のテンプレートからコピーした`abort_duration`と`abort_duration_dont_break`は、この2つへ置き換えてください。標準設定は27秒超過を警告し、処理を続行します。

`XSBOT_CLOUD_PROVIDER`には`gcp`または`aws`を必ず明示してください。未指定時にGCPへfallbackする挙動はなく、誤接続を避けるため起動時に失敗します。

既存シナリオでDSL version 1または2を利用している場合は、`options.scenario_version`へ同じversionを明示します。設定テンプレートの既定値は3です。

状態を共有するBotには同じ`state_namespace`を設定します。省略した場合はBot名がnamespaceになります。

### Moreを使うシナリオの設定

`settings.yaml.template`では`line.more`を有効にせず、画像テキストの標準フレームに`more_mode: quick_between`を指定しています。既存のMoreを使うシナリオを移行する場合は、`settings.yaml`の`plugins`に`line.more`を残し、記号・画像・案内先ラベルを既存設定に合わせてください。追加パッケージは不要です。

画像テキストのMore方式には、`line.image_text.more_message`（フレーム設定またはコマンド第3引数による指定も可）が必要です。`between`／`always`は`line.image_text.more_image_url`も維持します。`inner`は追加画像URLを使わず、`quick_between`／`quick_always`はこの2項目を省略できます。フレームの`more_mode`を省略した場合の既定値は、従来どおり`between`です。

ビルド済みシナリオにはMoreの内部命令が含まれることがあります。設定だけで無効化せず、画像テキストの`inner`／`between`／`always`も含めて利用箇所を確認してください。Quick Replyへ切り替える場合は再ビルドし、操作の見た目と進行中の利用者への影響を確認します。Webchatも同じ`line.more`設定を使います。

## 3. シナリオを再ビルドする

Google Sheetsを新環境のサービスアカウントへ共有し、管理画面からシナリオをビルドします。ビルド済みpickleや変換済みメディアを手作業で移す必要はありません。

シナリオビルドに成功し、主要actionが期待どおり動くことを確認してからWebhookや利用者を切り替えます。

media commandを利用する既存Scenarioでは、次の現行構文も確認してください。

- `@audio`はURLと正整数のduration millisecondsを指定します。
- `@video`の第三引数は、再生完了時に実行する内部actionです。LINEの完成`trackingId`がprovider上限へ収まらない場合はbuild errorになり、完了eventを利用できないLINE group／roomではactionを付けません。

## 4. 状態データを移行する

状態データの自動移行機能はありません。

- NDB／DatastoreからFirestoreへ移る場合は、保存形式を確認して一度だけの変換処理を用意します。
- GCPからAWSへ移る場合は、Firestoreの状態をDynamoDBへ、必要なオブジェクトをS3へ別途移行します。
- PlayerStatusのキーには`state_namespace`とユーザーIDが使われます。
- 次ラベルを利用している場合はPlayerNextLabelも移行対象に含めます。

移行処理は、dry-run、移行先の未存在確認、再実行可能性を備えたものにしてください。切り替え前に件数と代表データを照合します。

## 5. 管理画面認証を設定する

管理画面はユーザー名とパスワードによるフォーム認証を使用します。`tools/generate_admin_auth.py`で管理者認証JSONを作成し、GCPでは環境変数、AWSではParameter Storeの`SecureString`として設定します。

## 6. APIとWebhookを切り替える

action APIはGETとPOSTに対応しています。APIトークンは`X-API-Token`ヘッダーを使用してください。query／formの`token`も互換入力として受理します。

LINE Webhookを`/line/callback/<bot_name>`へ、Twilio Webhookを対応する`/twilio/`配下のURLへ設定します。切り替え後に、署名付きWebhook、状態更新、返信、遅延action、グループ配信を確認します。

### LINEの送受信はSDKを使わない

LINEとのやりとりは`plugin/line/api.py`（reply／push／rich menu紐付けの3 API）、`plugin/line/webhook.py`（署名検証とevent取り出し）、`plugin/line/messages.py`（送信JSONの組み立て）が`requests`だけで行います。`line-bot-sdk`はruntimeの依存から外し、Python更新やSDKの版上げで自分のコードを書き直す必要をなくしました。

- 送信JSONは、旧SDKを使っていた実装の出力を記録した`tests/fixtures/line/wire_golden.json`と一致することをテストで固定しています
- 開発環境に`line-bot-sdk`（v3、`requirements-dev.txt`）があれば、公式SDKのモデルが作るJSONとも比較します。LINEの仕様変更はこの比較の差分として検出します
- 旧実装との意図した差は4つです。replyTokenを持たないevent（unfollow／leave、standby modeのevent）はログだけ残して送信しない。`X-Line-Retry-Key`はpushだけに付く。`User-Agent`が`python-requests`になる。旧SDKが知らないmessage typeを受けたときは、500ではなく`:LINE_ETC:<type>`のactionとしてScenarioへ渡す（旧実装が意図していた挙動）
- 新しいメッセージ項目やeventを使うときは、[公式リファレンス](https://developers.line.biz/ja/reference/messaging-api/)のkey名で`messages.py`に引数を足し、`_construct_action`に分岐を足します。SDKの版上げを待つ必要はありません

## 7. 切り替えを完了する

次を確認してから旧環境への新規入力を停止します。

- シナリオビルドが成功する
- 既存ユーザー状態を読み込める
- LINEなどの主要な入力と返信が動く
- 遅延actionとグループ配信が動く
- 管理画面へログインできる
- ログからユーザーの進行とエラーを追跡できる

外部送信と状態更新は原子的ではありません。切り替え中の二重実行を避けるため、同じWebhookや配信処理を旧環境と新環境で同時に有効にしないでください。

Cloud LoggingからBigQueryへログを出力する場合は、実際に作成されたテーブルとフィールドを基準に集計を作り直します。GAEのrequest logテーブルを前提にしたクエリはCloud Runでは使用できません。

### WebchatのLIFF連携

`liff_apps`を有効にすると、同じプレイに属するLIFF用Botの状態を含むversion 2の署名tokenを返します。既存の単独Botのversion 1セーブは、会話用Botの互換epochが同じなら引き継ぎ、LIFF用Botを未開始から追加します。連携Botの追加や未実行Botの変更・削除では既存セーブを維持します。そのセーブで実行済みのLIFF用Botの互換epoch変更や連携先削除を検出した場合は、状態を黙って捨てずリセットを求めます。namespaceを共有するBotも、実行したBotごとに互換epochを確認します。

version 2を使ったセーブは、この機能に対応しない古いエンジンへ戻すと読めません。旧版へ戻す場合は旧セーブを使うか、明示的にリセットしてください。ページ側はtokenを解釈せず、親Webchatに管理を任せます。

### 転送時のinterface指定

`@forward Bot名 action [interface]`と`@delay 秒 Bot名 action [interface]`の末尾で実行interfaceを指定できます。以前は読み捨てていた位置のセルを使うため、`@forward`の第3引数、`@delay`の第4引数にメモ等を書いている場合はコメント行へ移してから再ビルドしてください。通常の2引数の`@forward`、2・3引数の`@delay`は変更不要です。

非同期転送の指定省略時は、引き続き利用者のサービスで実行します。LINE上のLIFFからの転送では、事前検査も実際に実行する`line`へ揃えました。これまで検査のためだけに必要だった転送先の`liff`は不要です。明示的に`liff`を指定して使うBotには残してください。利用者IDと`state_namespace`の保存キーは変更しません。

interface指定付きtaskを処理するには、API／workerもこの版に更新する必要があります。API／workerを先に更新し、その後にinterface指定を使うシナリオを再ビルドしてください。

### 定数とリッチメニュー

settingsの定数名をNFKC＋小文字化します。大文字・全角のキーが参照できるようになる一方、正規化後に衝突するキーは整理が必要です。また、同名がsettingsと定数Sheetの両方にある場合は、定数Sheetの値を優先します。これまで参照できなかったキーが有効になることで、同名の状態変数より先に見つかる場合もあります。

既存の生IDによる`@richmenu`は変更不要です。論理名へ移す場合は、settingsへ定義 → デプロイ → 管理画面でLINEへ反映 → シナリオの再ビルドの順で行います。Webchatの既存セーブもそのまま読めます。メニュー状態のないセーブは既定メニューを使います。

### LIFFのチャネル検証とページ連携

実LINEのLIFF APIを利用する場合は、`liff.params.login_channel_id`（またはplugin共通設定）へ、LIFFを登録したLINE LoginチャネルIDを文字列で追加してから更新します。テンプレートでは環境変数`LIFF_LOGIN_CHANNEL_ID`を参照します。Messaging APIチャネルIDとは異なります。未設定ではLINE側のLIFF APIが503となり、発行先の違うtokenは401で拒否します。Webchat内だけのLIFF利用にはこの設定は不要です。認証要求のtimeoutは各10秒です。

`@richmenu`はLIFF interfaceでも実行するようになります。LINEでは同じBotのline interfaceが必要で、Webchatでは実行中Botの保存状態を変更します。これまでLIFFへ文字列として返っていた`@richmenu`をイベントとして使っていないことを確認してください。

`ignore_unhandled_action`は既定falseで、従来の未定義ラベルの扱いを維持します。入口に任意イベントを送るページだけtrueにできます。内部ジャンプの誤りは無視しません。

`liff_id`・`match: prefix`は任意です。従来の実ページURL・完全一致・定数上書きも維持します。読替えを使う場合は、親Webchatのclient／参照UI（export版は再export）も更新してください。iframe protocolはversion 1のままです。子ページだけを更新しても、古い親のURL判定やsandboxは変わりません。
