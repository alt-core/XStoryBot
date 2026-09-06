# XStoryBot - Transmedia Storytelling Bot

## 概要

複数のメディアを横断するストーリーテリングでの利用を意図して設計された、チャットボットシステムです。

自然文入力への対応が弱い代わりに、決められたワードの入力に反応して、インタラクティブなストーリーを提供することを得意としています。

シナリオを Google Sheets 上で記述できるため、シナリオ作成者との作業分担が行いやすいという特徴もあります。

現在の公開版は Python 3.11 で動作し、Cloud Run または AWS 向けに構成されています。

### プロジェクトの状態

- 小規模な実験では安定して動いていますが、負荷テストも行っていない、α版の品質です。
- ドキュメントがほとんどありません。
- 仕様は大幅に変わる可能性があります。
- 個人の趣味のプロジェクトですので、あまり精力的な開発はできません。

## システム構成
上半分のコンテンツ制作者から見えるシステムと、下半分のユーザから見えるシステムに分かれます。

![システム構成図](./docs/system_diagram.png)

この構成図は旧GAE版を基にした概念図です。現行版のGCP構成ではCloud Run、AWS構成ではAPI Gateway、Lambda、Fargateなどが、図中のGoogle App Engineに相当する役割を担います。Webchatだけは署名付き状態をbrowserへ保存し、図中のユーザ状態DBを利用しません。

## できること

ユーザからの入力テキストに対して、どんな反応を返すのか、スプレッドシート上で定義します。
条件は、入力に含まれる語（部分一致）、または正規表現で記述できます。

シナリオはシーン単位で管理されており、シーン毎にユーザへ異なるリアクションを提供できます。
ユーザ毎に現在どのシーンに居るかが保存されています。

また、記述方法がこなれていないためオススメできませんが、フラグ管理にも対応しています。

複数のbotを同時に実行できます。ユーザの状態は既定ではbotごとに分離され、同じ`state_namespace`を設定したbot間では共有できます。

## 対応サービス

plugin によって拡張可能な設計になっています。
現時点で対応しているサービスは以下の通りです。

### ユーザとの対話

- LINE@ のボットシステム（[LINE Messaging API](https://developers.line.me/ja/services/messaging-api/)）
  - ボタン・カルーセル・イメージマップなど、一部の特殊表示に対応しています。
  - 送受信は`requests`で直接行い、LINEのSDKには依存しません（[移行ガイド](./docs/migration.md)を参照）。
  - WebHook などを契機にした Push messages にも対応していますが、友だちが50人を越えると[月額32400円が必要](https://at.line.me/jp/plan)です。
- [Twilio](https://twilio.kddi-web.com/) （電話・SMS）
  - 電話がかかってきたことをトリガーに SMS を送信し、返信の内容によって電話をかける、といったことが可能です。
  - しかし、電話にせよ、SMS にせよ、とにかく[単価が高い](https://twilio.kddi-web.com/price/)ため、大規模な利用は困難です。
- WebAPI（認証付き action API）
- Webchat（AWSの署名付きclient state方式）
  - 導入方法、対応範囲、client組込みは[Webchatガイド](./docs/webchat.md)を参照してください。
  - API契約は[OpenAPI 3.1](./docs/webchat.openapi.yaml)、表示データは[JSON Schema](./docs/webchat-message-spec.schema.json)で公開しています。

### IoT 機器などとの連携

- [Pusher](https://pusher.com/)
- 一般的な WebHook

### シナリオファイルの読み込み

- Google Sheets

## インストール手順

このリポジトリを clone した上で、Python 3.11 環境へ必要なパッケージをインストールします。依存は接続先ごとに分かれています。

    > git clone https://github.com/alt-core/XStoryBot.git
    > python3 -m pip install -r requirements-gcp.txt      # GCP に接続する場合
    > python3 -m pip install -r requirements-aws.txt      # AWS に接続する場合
    > python3 -m pip install -r requirements-optional.txt # Twilio／Pusher plugin を使う場合だけ

`requirements.txt`は両者に共通の依存で、単独では使いません。開発・テストには`requirements-dev.txt`（全部入り）を使います。

Twilio と Pusher は任意 plugin です。使う場合は`requirements-optional.txt`を追加で install し、`settings.yaml`の`plugins`に次のように書きます（値は環境変数から渡します）。Docker では`--build-arg XSBOT_EXTRA_REQUIREMENTS=requirements-optional.txt`を付けます。

    plugins:
      twilio:
        sid: !env TWILIO_SID
        auth_token: !env TWILIO_AUTH_TOKEN
        phone_number: !env TWILIO_PHONE_NUMBER
      pusher:
        app_id: !env PUSHER_APP_ID
        key: !env PUSHER_APP_KEY
        secret: !env PUSHER_APP_SECRET
        cluster: !env PUSHER_APP_CLUSTER

More（`line.more`）も任意 plugin です。追加パッケージは不要で、既存シナリオの移行などで使う場合だけ`settings.yaml`の`plugins`に追加します。

    line.more:
      command: ["▼"]
      image_url: "https://example.com/path/to/more_button.png"
      message: "「続きを読む」"
      action_pattern: null
      ignore_pattern: "^「|^リセット$|^\\*共通/リセット$"
      please_push_more_button_label: "##please_push_more_button"

`command`はシナリオで使う記号に合わせ、案内先の`##please_push_more_button`ラベルも用意してください。画像テキストの標準フレームは`more_mode: quick_between`でQuick Replyを使います。`inner`／`between`／`always`でMoreを使うフレームには`line.more`が必要です。WebchatのMoreも同じ設定で有効になります。

画像テキストでMoreを使う場合は、`line.image_text.more_message`を指定します。フレームの`more_message`またはコマンドの第3引数でも指定できます。`between`／`always`では`line.image_text.more_image_url`も必要です。`inner`は本文画像をボタンにするため追加画像URLは不要で、`quick_between`／`quick_always`では両項目とも不要です。

Cloud Runへデプロイする場合は、利用するGCPプロジェクトを準備してください。

以下、特殊な前準備が必要です。

- https://console.developers.google.com/apis/api/sheets.googleapis.com/overview
  - 展開先のプロジェクトにて、Sheets API を有効化
- 同様の手順で Google Cloud Storage も有効化
- Firestore に複合 index を作成（collection `group_message_tasks`、`bot_name` 昇順 + `created_at` 降順）
  - ダッシュボードのグループ配信履歴で使います。未作成の場合はその一覧だけが失敗し、ログに作成用URLが出ます
- GCP のダッシュボードでサービスアカウントを作成
  - json 形式でクレデンシャルファイルをダウンロード
- Google Sheets でシナリオのスプレッドシートを作成
  - 共有で上述のサービスアカウントのメールアドレスに招待
    - 招待する時は「通知」のチェックボックスを外す
- LINE@ を使う場合
  - LINE@ のアカウントを作成し、接続に必要な情報をメモ
  - LINE@ の webhook に 〜/line/callback/＜botname＞ を設定
- Twilio を使う場合
  - Twilio の電話番号を取得し、必要な情報をメモ
  - Twilio の webhook に 〜/twilio/callback/＜botname＞ を設定

続いて、設定ファイルと環境変数を準備します。初回に記入例をコピーし、コンテンツ用の設定を作成します。既存の`settings.yaml`がある場合は上書きしないでください。

    > cp settings.yaml.template settings.yaml

`settings.yaml`を必要に応じて編集し、`.env.template`に列挙された環境変数を実行環境へ設定してください。ローカル実行とDockerイメージは同じ`settings.yaml`を使います。ファイルが無い場合や空の場合、Dockerビルドは停止します。`settings.yaml`は公開Gitへ追加せず、秘密値は直書きせずに`!env`等で実行環境から渡してください。別コンテンツの設定やバックアップはビルド対象ディレクトリの外に保管します。`XSBOT_DEPLOY_ENV`には適用する環境別設定（例: `prod`、`stg`、`dev`、`test`、`local`）を指定します。`XSBOT_CLOUD_PROVIDER`は`gcp`または`aws`を必ず明示し、未設定時は誤ったbackendへ接続せず起動を停止します。

GCPとGoogle Sheetsで使うサービスアカウントJSONはコンテナイメージへ含めず、Secret Managerから読み取り専用ファイルとしてマウントし、それぞれのコンテナ内パスを`GOOGLE_APPLICATION_CREDENTIALS`と`SHEETS_SERVICE_ACCOUNT`へ指定してください。同じサービスアカウントを使う場合は、両方に同じパスを指定できます。

sheet_id は Google Sheets の編集時に URL に含まれるランダム英数字です。
api_token は、WebAPI などでの認証のために使われる情報です。必ず独自の値を設定してください。

利用するpluginとBot interfaceは`settings.yaml`で設定します。`settings.yaml.template`は記入例として維持します。

設定後、`Dockerfile`からコンテナイメージを一度ビルドし、同じイメージをCloud RunのAPI用サービスとビルダー用サービスへデプロイします。イメージには接続先の依存だけが入ります（`--build-arg XSBOT_CLOUD_PROVIDER=gcp`、既定はgcp）。Twilio／Pusher pluginを使う場合は`--build-arg XSBOT_EXTRA_REQUIREMENTS=requirements-optional.txt`を付けます。`XSBOT_CLOUD_PROVIDER`はイメージの環境変数として焼き込まれるため、Cloud Run側で別途設定する必要はありません（設定する場合は同じ値にしてください）。API用は既定の`app:app`を使い、ビルダー用だけ`XSBOT_APP_MODULE=app_builder:app`を設定します。それぞれのURLを`XSBOT_APP_BASE_URL`と`XSBOT_BUILDER_BASE_URL`へ指定し、Cloud Tasksには同じプロジェクト・リージョンで`build-queue`、`action-queue`、`group-message-queue`の3キューを作成します。現行のTaskQueueはOIDCトークンを付けないため、両サービスはCloud IAMで未認証HTTP呼び出しを許可し、保護が必要なrouteはWebhook署名、フォーム認証、または`X-API-Token`で保護します。

Cloud RunのCPU、メモリ、最小・最大インスタンス数は、Cloud Runサービス側で設定してください。

現行GCP実装はシナリオとメディアをオブジェクトACLで公開します。そのため、保存先にはオブジェクト単位の公開を許す専用バケットが必要で、Uniform bucket-level accessとPublic Access Preventionは有効にできません。バケット全体を公開する必要はありません。

共有APIトークンで利用できるグループ管理APIとして、`POST /api/v1/groups/<group_id>/add_members`と`GET /api/v1/groups/<group_id>/members`があります。認証には`X-API-Token`ヘッダーを使用します。

### AWSへデプロイする場合

AWS CLI、AWS SAM CLI、DockerとAWS認証情報、既存のECR repositoryを準備してください。Google Sheets資格情報、管理者認証JSON、runtime秘密値JSONは、AWS管理KMSキーを使うParameter Storeの`SecureString`へ事前に登録します。

管理者認証JSONは`python3 tools/generate_admin_auth.py`で生成できます。

`AWS_REGION`、`XSBOT_AWS_STACK_NAME`、`XSBOT_AWS_ECR_REPOSITORY`、`XSBOT_AWS_ENVIRONMENT`、`XSBOT_AWS_SHEET_ID`、`XSBOT_AWS_SHEETS_CREDENTIAL_PARAMETER`、`XSBOT_AWS_ADMIN_AUTH_PARAMETER`、`XSBOT_AWS_RUNTIME_SECRETS_PARAMETER`を環境変数に設定し、`./deploy_aws.sh`を実行します。`XSBOT_AWS_ALARM_EMAIL`を設定すると、DLQ・HTTP API 5xx・Webchat errorのalarmがそのアドレスへ通知されます（初回はSNSの購読確認メールを承認してください）。通常のruntime秘密値そのものはスクリプトへ渡しません。Webchatを有効にする場合は、`XSBOT_WEBCHAT_ENABLED=true`、`XSBOT_WEBCHAT_SIGNING_KEY`、`XSBOT_WEBCHAT_SCENARIO_URI`も必要です。詳細は[Webchatガイド](./docs/webchat.md)を参照してください。

更新時に未設定のWebchat設定・通知先は、前回値を維持します。既にWebchatが有効なstackを通常更新する場合は、`XSBOT_WEBCHAT_ENABLED`を未設定にします。明示した`false`は無効化、明示したepochは互換性の変更です。許可origin・通知先は空文字を明示すると消去できます。既存の`.env`に設定が残っている場合も明示値として扱うため、維持したい項目はexportしないでください。Webchatのimageは、有効状態の指定を省略しても更新されます。

`.env.template`のAWSデプロイ入力を設定すれば、table、queue、subnetなどのruntime値はSAMが各実行環境へ供給します。これらを手入力するのはローカルからAWS backendを直接使う場合です。`.env`ファイルは自動では読み込まれないため、必要な値を環境変数としてexportしてから実行してください。

API、2つのworker、Fargateで同じECR imageを共用するため、スクリプトはDockerで一度だけ`linux/amd64` imageをbuild/pushし、`ImageUri`をSAMへ渡します。同一imageの再buildを避けるため`sam build`は実行しません。imageにはAWS向けの依存だけが入り、Twilio／Pusher pluginを使う場合は`XSBOT_EXTRA_REQUIREMENTS=requirements-optional.txt`を設定してから実行します。

## シナリオの作成

Google Sheets 上でシナリオを作成します。
基本的なSheet構成、scene、message、主要commandは[Scenario作成ガイド](./docs/scenario-authoring.md)を参照してください。

## ダッシュボードからシナリオ読み込み

デプロイ先のホストの 〜/dashboard/ にブラウザでアクセスすると管理画面が開きます。

ダッシュボードではユーザー名とパスワードによる認証が要求されます。管理者認証JSONは環境変数またはAWS Parameter Storeから設定してください。

ダッシュボードにある「シナリオ修正の反映」のボタンを押すことで、Google Sheets からシナリオを読み込み、選択したクラウドプロバイダーのオブジェクトストレージ上に中間ファイルを生成します。

この時、build処理の対象となる画像等のリソースファイルも同じオブジェクトストレージ上にコピーされますので、安定したサービス提供が可能です。

## ログ

@log コマンドで、ユーザがシーン中の特定の箇所に来た際にログを出力することが可能です。
GCPではアプリケーションログがCloud Logging、AWSではCloudWatch Logsに出力されます。GCPでBigQueryへ集計する場合は、Cloud LoggingからBigQueryへのシンクを設定してください。

Cloud LoggingからBigQueryへエクスポートされるテーブル名とスキーマは、シンク設定とログ形式によって異なります。実際に作成されたテーブルとフィールドを確認してクエリを作成し、ビューとして保存してください。

## ユニットテスト

### 準備

    > python3 -m pip install -r requirements-dev.txt

### 実行

    > ./test.sh

## 注意事項

Cloud Run、Firestore、Cloud Storage、Cloud Tasks、Cloud Logging、BigQueryに加え、AWSのLambda、API Gateway、DynamoDB、S3、CloudFront、SQS、EventBridge Scheduler、Fargate、CloudWatchは従量課金の対象です。

不具合により、意図しない課金が発生したとしても、補償いたしかねますので、[アラート](https://cloud.google.com/billing/docs/how-to/budgets?hl=ja&ref_topic=6288636&visit_id=1-636539550464473783-319035179&rd=1)などをご活用ください。

以前のデプロイから移行する場合は、[移行ガイド](./docs/migration.md)を参照してください。
