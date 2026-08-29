# XStoryBot移行ガイド

この文書は、GAE／Python 2構成またはGCP専用構成から、現在のPython 3.11版へ移行する場合に必要な作業をまとめています。

## 1. 別環境へ先にデプロイする

既存環境を直接上書きせず、Cloud RunまたはAWSへ別の検証環境を作成します。現在のコードは、GCPとAWSのいずれか一方をプロセス起動時に選択します。

GAE用のデプロイ設定は含まれていません。GCPではAPI用・ビルダー用の2つのCloud Runサービスと3つのCloud Tasksキューを用意します。AWSでは`template.aws.yaml`と`deploy_aws.sh`を使用します。

## 2. 設定を作り直す

`settings.yaml.template`と`.env.template`を基に、Bot、plugin、環境変数を設定します。実環境の設定ファイルや認証情報をリポジトリまたはDockerイメージへコピーしないでください。

既存シナリオでDSL version 1または2を利用している場合は、`options.scenario_version`へ同じversionを明示します。設定テンプレートの既定値は3です。

状態を共有するBotには同じ`state_namespace`を設定します。省略した場合はBot名がnamespaceになります。

## 3. シナリオを再ビルドする

Google Sheetsを新環境のサービスアカウントへ共有し、管理画面からシナリオをビルドします。ビルド済みpickleや変換済みメディアを手作業で移す必要はありません。

シナリオビルドに成功し、主要actionが期待どおり動くことを確認してからWebhookや利用者を切り替えます。

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
