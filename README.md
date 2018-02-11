# XMBot - Transmedia Bot

## 概要

複数のメディアを横断的に利用できることを目指して設計された、チャットボットシステムです。

シナリオを Google Sheets 上で記述できるため、シナリオ作成者との作業分担が行いやすいという特徴もあります。

Google App Engine 向けのサーバアプリケーションとして開発されています。

### プロジェクトの状態

- 小規模な実験では安定して動いていますが、負荷テストも行っていない、α版の品質です。
- ドキュメントがほとんどありません。
- 仕様は大幅に変わる可能性があります。
- 個人の趣味のプロジェクトですので、あまり精力的な開発はできません。

## できること

ユーザからの入力テキストに対して、どんな反応を返すのか、スプレッドシート上で定義します。
条件は、完全一致、または正規表現で記述できます。

シナリオはシーン単位で管理されており、シーン毎にユーザへ異なるリアクションを提供できます。
ユーザ毎に現在どのシーンに居るかが管理されています。

また、記述フォーマットがこなれていないためオススメできませんが、フラグ管理にも対応しています。

LINE では、ボタン・カルーセル・イメージマップなど、一部の特殊表示に対応しています。

## 対応サービス

plugin によって拡張可能な設計になっています。
現時点で対応しているサービスは以下の通りです。

### ユーザとの対話

- LINE@ のボットシステム（[LINE Messaging API](https://developers.line.me/ja/services/messaging-api/)）
  - WebHook などを契機にした Push messages にも対応していますが、友だちが50人を越えると[月額32400円が必要](https://at.line.me/jp/plan)です。
- [Twilio](https://twilio.kddi-web.com/) （電話・SMS）
  - SMS は1通当たりの[料金が高い](https://twilio.kddi-web.com/price/)ため、チャットボットとして実用での利用は困難です。
- WebAPI （テスト用）

### IoT 機器などとの連携

- [Pusher](https://pusher.com/)
- 一般的な WebHook

### シナリオファイルの読み込み

- Google Sheets

## インストール手順

このリポジトリを clone した上で、追加のパッケージを pip で lib/ 以下にインストールします。

    > git clone ...
    > pip install -r packages.txt -t lib

続いて、GAE のセットアップです。
詳しくは、一般的な GAE のセットアップ手順を参照してください。

    > gcloud auth ...
    > gcloud app --project="<<your-project-name>>" create

以下、特殊な前準備が必要です。

- https://console.developers.google.com/apis/api/sheets.googleapis.com/overview
  - 展開先のプロジェクトにて、Sheets API を有効化
- 同様の手順で Google Cloud Storage も有効化
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

続いて、設定ファイルを編集します。

    > vim settings.py

前準備で準備した情報を記入してください。
sheet_id は Google Sheets の編集時に URL に含まれるランダム英数字です。
api_token は、WebAPI などでの認証のために使われる情報です。必ず独自の値を設定してください。

    > vim app.yaml

plugin の下の app.yaml を include していますので、使わない plugin をコメントアウトしてください。

設定が終われば、デプロイします。

    > gcloud app deploy

詳細は、通常の GAE のデプロイ手順を参照してください。

## シナリオの作成

Google Sheets 上でシナリオを作成します。
詳細は、シナリオフォーマットのドキュメント（未作成）を参照してください。

## ダッシュボードからシナリオ読み込み

デプロイ先のホストの 〜/dashboard/ にブラウザでアクセスすると管理画面が開きます。

Google アカウントでの認証が要求されますので、GAE の admin 権限を持ったアカウントでアクセスするか、もしくはアクセスを許可したい Google アカウントのメールアドレスを settings.py の OPTIONS['admins'] に設定してください。

ダッシュボードにある「シナリオ修正の反映」のボタンを押すことで、Google Sheets からシナリオを読み込み、Google Cloud Storage 上に中間ファイルを生成します。

この時、シナリオで指定された画像等のリソースファイルも全て Google Cloud Storage 上にコピーされますので、安定したサービス提供が可能です。

## 注意事項

Google App Engine および Google Cloud Storage は従量課金サービスです。

不具合により、意図しない課金が発生したとしても、補償いたしかねますので、[アラート](https://cloud.google.com/billing/docs/how-to/budgets?hl=ja&ref_topic=6288636&visit_id=1-636539550464473783-319035179&rd=1)などをご活用ください。
