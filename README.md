# sheetbot
* 概要

チャットボットのリアクションを Google Sheets で記述できるようにする試みです。
GAE 専用です。

* インストール

    > git clone ...
    > pip install -r packages.txt -t lib

GAEに存在していないモジュールはローカルから提供しなければならないため。

    > gcloud auth ...
    > gcloud app --project="<<your-project-name>>" create

通常の GAE のセットアップ手順を参照。

以下、特殊な前準備
- https://console.developers.google.com/apis/api/sheets.googleapis.com/overview
  - 適切なプロジェクトにて、Sheets API を有効化
- GCP のダッシュボードでサービスアカウントを作成
  - json 形式でクレデンシャルファイルをダウンロード
- Google Sheets でシナリオのスプレッドシートを作成
  - 共有で上述のサービスアカウントのメールアドレスに招待
    - 招待する時は「通知」のチェックボックスを外す
- LINE@ を使う場合
  - LINE@ のアカウントを作成し、接続に必要な情報をメモ
  - LINE@ の webhook に 〜/line/callback/<<botname>> を設定
- Twilio を使う場合
  - Twilio の電話番号を取得し、必要な情報をメモ
  - Twilio の webhook に 〜/twilio/callback/<<botname>> を設定

    > vim settings.py

前準備で準備した情報を記入。
sheet_id は Google Sheets の編集時に URL に含まれるランダム英数字。

    > gcloud app deploy

通常の GAE のデプロイ手順を参照。

* 注意事項

- LINE@ はユーザIDで、Twilio は電話番号でユーザ管理しているため
  両者を跨いだ連携はきません。
- LINE@ と Twilio で使えるコマンドが全く違いますが、
  リファレンスの整備ができていません。
- Twilio に対応してみたものの、SMS送信1通10円ですので、実用性はありません。
- GAE のインスタンスが立ち上がる度に Google Sheets を読みに行く設計は
  やっぱり間違っていました。
