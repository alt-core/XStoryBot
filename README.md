# XStoryBot - Transmedia Storytelling Bot

## 概要

複数のメディアを横断するストーリーテリングでの利用を意図して設計された、チャットボットシステムです。

自然文入力への対応が弱い代わりに、決められたワードの入力に反応して、インタラクティブなストーリーを提供することを得意としています。

シナリオを Google Sheets 上で記述できるため、シナリオ作成者との作業分担が行いやすいという特徴もあります。

Google App Engine 向けのサーバアプリケーションとして開発されています。

### プロジェクトの状態

- 小規模な実験では安定して動いていますが、負荷テストも行っていない、α版の品質です。
- ドキュメントがほとんどありません。
- 仕様は大幅に変わる可能性があります。
- 個人の趣味のプロジェクトですので、あまり精力的な開発はできません。

## システム構成
上半分のコンテンツ制作者から見えるシステムと、下半分のユーザから見えるシステムに分かれます。

![システム構成図](./docs/system_diagram.png)

## できること

ユーザからの入力テキストに対して、どんな反応を返すのか、スプレッドシート上で定義します。
条件は、完全一致、または正規表現で記述できます。

シナリオはシーン単位で管理されており、シーン毎にユーザへ異なるリアクションを提供できます。
ユーザ毎に現在どのシーンに居るかが保存されています。

また、記述方法がこなれていないためオススメできませんが、フラグ管理にも対応しています。

複数のbotを同時に実行できることも特徴で、全体でユーザの状態を共有しているため、複数のbotを組み合わせたユーザ体験を作ることもできます。

## 対応サービス

plugin によって拡張可能な設計になっています。
現時点で対応しているサービスは以下の通りです。

### ユーザとの対話

- LINE@ のボットシステム（[LINE Messaging API](https://developers.line.me/ja/services/messaging-api/)）
  - ボタン・カルーセル・イメージマップなど、一部の特殊表示に対応しています。
  - WebHook などを契機にした Push messages にも対応していますが、友だちが50人を越えると[月額32400円が必要](https://at.line.me/jp/plan)です。
- [Twilio](https://twilio.kddi-web.com/) （電話・SMS）
  - 電話がかかってきたことをトリガーに SMS を送信し、返信の内容によって電話をかける、といったことが可能です。
  - しかし、電話にせよ、SMS にせよ、とにかく[単価が高い](https://twilio.kddi-web.com/price/)ため、大規模な利用は困難です。
- WebAPI （テスト用）

### IoT 機器などとの連携

- [Pusher](https://pusher.com/)
- 一般的な WebHook

### シナリオファイルの読み込み

- Google Sheets

## インストール手順

このリポジトリを clone した上で、追加のパッケージを pip で lib/ 以下にインストールします。

    > git clone ...
    > pip install -r requirements.txt -t lib

なお、line_bot_sdk が利用する requests が利用する urllib3 で chunked encoding を受信する際に例外が出る不具合があります。
https://github.com/agfor/requests/commit/6f7af88464504d6c9a4f84ce9c9535d2eb941b39
このパッチを lib/requests/models.py に当ててください。

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
なお、2024年より、Python2.7をGAEで利用するには特殊な組織ポリシーの設定が必要となっていますので、注意してください。

## シナリオの作成

Google Sheets 上でシナリオを作成します。
詳細は、シナリオフォーマットのドキュメント（未作成）を参照してください。

## ダッシュボードからシナリオ読み込み

デプロイ先のホストの 〜/dashboard/ にブラウザでアクセスすると管理画面が開きます。

Google アカウントでの認証が要求されますので、GAE の admin 権限を持ったアカウントでアクセスするか、もしくはアクセスを許可したい Google アカウントのメールアドレスを settings.py の OPTIONS['admins'] に設定してください。

ダッシュボードにある「シナリオ修正の反映」のボタンを押すことで、Google Sheets からシナリオを読み込み、Google Cloud Storage 上に中間ファイルを生成します。

この時、シナリオで指定された画像等のリソースファイルも全て Google Cloud Storage 上にコピーされますので、安定したサービス提供が可能です。

## ログの BigQuery での集計

@log コマンドで、ユーザがシーン中の特定の箇所に来た際にログを出力することが可能です。
Google App Engine のログは、Stackdriver Logging にまずは出力されますので、ここから直接ログを取得することも可能ですが、BigQuery を経由した方が柔軟な対応が可能となります。

### Stackdriver Logging から BigQuery の接続

Cloud Console の Logging > エクスポート から「エクスポートを作成」を選択。
シンク名は適当に。シンクサービスに BigQuery、シンクのエクスポート先に「新しい BigQuery データセットを作成」を選び、ポップアップした入力欄に「log」と設定。

この設定を行った後から、ログが BigQuery に取り込まれるようになります。

### BigQuery でのビューの設定

ログが出力されるように、bot を少し動かしてしばらく待つと、Cloud Console の BigQuery の「リソース」内の先ほど作成した log というデータセットの下に、 appengine_googleapis_com_request_log_* というテーブルが作成されます。

クエリエディタ内で以下のクエリを作成し、一度「実行」してみて、問題なさそうであれば、「ビューを保存」してください。

```
SELECT
  timestamp,
  JSON_EXTRACT_SCALAR(l.logMessage, "$.date") AS date,
  JSON_EXTRACT_SCALAR(l.logMessage, "$.uid") AS uid,
  JSON_EXTRACT_SCALAR(l.logMessage, "$.cat") AS cat,
  JSON_EXTRACT_SCALAR(l.logMessage, "$.log") AS log,
  JSON_EXTRACT_SCALAR(l.logMessage, "$.scene") AS scene
FROM
  `log.appengine_googleapis_com_request_log_*`,
  UNNEST(protoPayload.line) as l
WHERE
  _TABLE_SUFFIX BETWEEN 
  FORMAT_DATE("%Y%m%d", DATE_SUB(CURRENT_DATE(), INTERVAL 12 MONTH)) AND FORMAT_DATE("%Y%m%d", CURRENT_DATE())
  AND
  timestamp >= TIMESTAMP("2019-01-01 00:00:00+09")
  AND
  l.logMessage LIKE "%XSBLog%"
ORDER BY
  timestamp DESC
LIMIT 50000
```

INTERVAL 12 MONTH としている部分は、実際に何ヶ月前まで取得したいかを、timestamp と比較している日付は、ログの取得を開始したい日時（サービスインの日時など）を指定します。

### BigQuery のビューの利用

[データポータル（旧 Google Data Studio）](https://datastudio.google.com/)は簡易的な BI ツールです。

空のレポートを作成し、「新しいデータソースを追加」から、BigQuery を選んで、上記で作成したビューを選ぶことで、ログの簡易分析が可能です。

一番シンプルな、特定のログが何回出力されたかを表示するには、以下の手順で表を設定します。

* 期間のディメンションを「date」に（JSTでフィルタできるようになる）
* ディメンションを「cat」と「log」に
* 指標は「Record Count」に
  * ユニークユーザ数がほしい場合はカスタムから `COUNT_DISTINCT(uid)` という式を入力

日付ごとのログ出力回数のグラフを作成することなども簡単にできますので、詳細はデータポータルの使い方を調べてください。

また、１件ずつのデータを見ながら解析を行いたい場合は、Google App Script から BigQuery のビューデータを引っ張ってきて Google スプレッドシート上でデータ表示／分析を行うことも可能です。

## ユニットテスト

### 準備

追加でいくつかのパッケージが必要です。

    > pip install webtest pyyaml Pillow
    > gcloud components install app-engine-python

### 実行

    > ./test.sh

## 注意事項

Google App Engine および Google Cloud Storage, Stackdriver Logging, Google BigQuery は従量課金サービスです。

不具合により、意図しない課金が発生したとしても、補償いたしかねますので、[アラート](https://cloud.google.com/billing/docs/how-to/budgets?hl=ja&ref_topic=6288636&visit_id=1-636539550464473783-319035179&rd=1)などをご活用ください。
