# ローカルでのシナリオ開発・検証

クラウドの保存資源を使わず、TSVまたはGoogle Sheetsからシナリオをビルドできます。AI用のLINE検証と、既存の参照UIを使った実Webchatを利用できます。

保存にはSQLiteとローカルファイルを使います。同一PCの開発・テスト向けです。ネットワークドライブや稼働中DBの複数PC同期、本番運用、クラウド固有の障害・配送の再現には対応しません。

## 準備

Python 3.11以降の環境で共通依存を入れます。GCP／AWS SDKは不要です。

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

まだsettings.yamlがない場合は、リポジトリのrootでlocal用templateをコピーします。既存の設定がある場合は上書きせず、別名のファイルを作って`--settings`で指定してください。

ローカル用の設定を別に用意し、対応するpluginだけを記載してください。通常の設定に含まれる未対応pluginを黙って無視することはありません。エンジンの設定読込先は`XSBOT_SETTINGS_FILE`でも指定でき、未指定時はsettings.yamlです。このCLIでは`--settings`を使い、確定した設定を子プロセスへ渡します。

```sh
cp settings.local.yaml.template settings.yaml
```

templateの設定では、同梱TSVのQuick Replyと画像テキストを外部接続なしで確認できます。

```sh
python tools/local_scenario.py verify --settings settings.yaml --bot bot --suite examples/local/suite.json
python tools/local_scenario.py webchat --settings settings.yaml --bot bot
```

Webchatの起動後、`http://127.0.0.1:8765/chat/bot`を開きます。候補を選び、その後「画像」と入力すると複数ページの画像テキストが表示されます。`tools/webchat_dev_server.py`は人工応答によるUI確認用で、実シナリオの実行にはこのコマンドを使います。

## 入力元の切り替え

通常は`bots.<bot>.scenario`の設定を使います。TSVを選ぶ場合は次の形です。

```yaml
scenario:
  type: tsv
  params:
    manifest: examples/local/manifest.json
```

manifestは、シート名とファイルを順序付きで指定します。TSVのpathはmanifestからの相対pathです。

```json
{"sheets": [{"name": "story", "path": "story.tsv"}, {"name": "$constants", "path": "constants.tsv"}]}
```

TSVはUTF-8、headerなし、標準CSVの引用規則を使うタブ区切りです。セル内改行や、式の`"right"`等の引用符を保つため、生成時はPythonの`csv.writer(..., delimiter='\t')`を使ってください。シートの順序、定数、環境別シートの連結はSheetsと同じ規則です。

Google Sheetsの場合は既存のloader設定を使います。認証は明示したサービスアカウントファイルのみで、自動資格情報探索はしません。表の読取りにGoogleへの通信が必要ですが、会話のたびには取得しません。

```yaml
scenario:
  type: google_sheets
  params:
    sheet_id: YOUR_SPREADSHEET_ID
    key_file_json: credentials/service-account.json
```

Google Sheetsを既定にしておき、今回だけTSVへ切り替えることもできます。

```sh
python tools/local_scenario.py webchat --settings settings.yaml --bot bot --tsv examples/local/manifest.json
```

`--tsv`を省略すると設定された入力元へ戻ります。設定ファイルやSheetsの内容を書き換える操作ではありません。シナリオversionは`options.scenario_version`、`.test`等の環境別シート選択は従来どおり`XSBOT_DEPLOY_ENV`で指定します。provider=localと環境名は別です。

`--tsv`で切り替えても、`evaluate_formula`、`script_sheet`、`constant_sheet`、`ignore_sheet`は引き継ぎます。優先順は共通options、元の入力pluginの共有設定、`plugins.tsv`、Botの共有設定で、CLIのmanifest指定が入力ファイルを決めます。資格情報やsheet ID等の入力元専用設定は引き継ぎません。

SheetsからのTSV取得、差分確認、書き戻しには[Sheets同期CLI](sheets-sync.md)を使えます。

### TSVのセル参照と文字列連結

TSVの式を使う場合は、`scenario.params.evaluate_formula: true`を指定します。Google Sheetsから`--tsv`で切り替える場合も、この設定を引き継ぎます。TSV用のplugin既定値は次のように指定できます。

```yaml
plugins:
  tsv:
    evaluate_formula: true
```

既定では無効です。有効にすると、次の式をビルド前に解決します。以下はTSVの引用を外したセルの内容です。

| セルの内容 | 意味 |
|---|---|
| `=A1`、`=$B$2` | 同じシートのセルを参照 |
| `=story!B2`、`='共通資料.test'!A1` | manifestに登録した名前のシートを参照 |
| `=A1 & "さん、こんにちは"` | セルや文字列を`&`で連結 |
| `="引用: ""はい"""` | 文字列内の二重引用符は`""`で表す |

参照は環境別シートを連結する前の位置を使います。`A2`は2番目のTSVレコードで、セル内改行によって増えるファイルの行番号とは区別します。シート名はmanifestと一致させ、名前の空白や記号は単一引用符で囲みます。名前に含まれる単一引用符は`''`と書きます。`_`で始まる補助シートや別環境のシートも、明示的に参照された場合は読み込みます。参照だけでシナリオへ連結されることはありません。

通常のTSVは文字列をそのまま使い、未記載のセルは空文字とします。Sheetsの表示書式や空セル参照の数値化は行いません。参照先の式も解決しますが、結果を再び式として解釈しません。`=IMAGE(...)`は既存の画像処理へ渡すため式文字列を保持し、その引数は計算しません。

範囲、算術演算、括弧によるグループ化、CONCATENATE等の関数には対応しません。未対応の式、存在しないシート、循環参照はセル位置と元ファイルの行番号を付けてエラーにします。式評価を有効にして`=`で始まる文字列自体を使いたい場合は、`="=文字列"`のように書けます。TSVファイルは変更せず、外部通信も行いません。

Sheets同期CLIが作ったコピーでは、書き戻しと同じ値変換を使います。元が数値なら`+01`は`1`、元が真偽値なら`TRUE`は`True`として読み、元から文字列だった未変更の`=A1`等は文字列のまま扱います。元の数値・真偽値として読めない編集は文字列として受け入れるため、説明行の挿入や定数ブロックの移動もできます。この扱いは式評価の有無によらず同じで、TSVファイル自体は書き換えません。型付きの基準はシートごとに読み、全シート分を常駐させません。

## LIFF用Botとの連携

[LIFF連携仕様](liff-webchat-api.md)の`liff_apps`を使えます。LIFF用Botは先に`build --settings settings.yaml --bot menu`でビルドし、結果の`scenario_uri`をそのBotの`webchat.params.scenario_uri`へ指定してください。会話用Botと同じ`local.storage_root`でビルドした固定URIを使います。Sheets等の入力設定や資格情報は、連携先の実行時設定へ持ち越しません。

その後、会話用Botを通常の`webchat --settings settings.yaml --bot bot`で起動します。参照UIの同梱確認ページは`http://127.0.0.1:8765/webchat-client/examples/liff/index.html`です。LIFF用Botの`open`、`bump`、`sync`を用意すると、連携を確認できます。

`--watch`による自動ビルドの対象は選択した会話用Botです。LIFF用Botを変更した場合は別途buildし、固定URIを更新してください。LIFF用Botの画像は、同じrootで生成済みのlocal媒体URLを使います。

## 更新とセーブ

WebchatをCtrl-Cで停止し、同じコマンドを再実行すると再ビルドして起動します。TSVの編集を自動で反映する場合は`--watch`を付けます。

```sh
python tools/local_scenario.py webchat --settings settings.yaml --bot bot --watch
```

設定、manifest、選択されたTSV、宣言asset、カスタムfontの変更をまとめて検出し、ビルド成功後にサーバーを再起動します。次の入力から新しいシナリオを使い、既に表示した履歴は書き換えません。ビルド失敗時は旧サーバーを維持して修正を待ちます。初回のビルドが失敗している間は接続先がまだありません。新サーバーの起動に失敗した場合は、直前の設定へ一度戻します。

TSVの式評価を有効にした場合は、参照先の編集も検出するためmanifest内の全TSVを監視します。同期情報と宣言された基準ファイルは、式評価の有無によらず監視します。監視は更新日時等の確認だけで、本文はビルド時に選択・参照されたシートだけ読みます。未参照ファイルの編集でも再ビルドする場合があります。

監視はローカルTSV用です。Google Sheetsを既定にしている場合は`--tsv ... --watch`で使えます。Sheets上の編集は自動検知せず、再実行で取得します。監視中にstorage_root、public_base_url、Botを変更する場合も停止・再実行してください。終了はCtrl-Cです。サーバー切替時には短い通信中断があり得ます。

Webchatの進行はブラウザに署名付き状態で保存します。同じ保存root・Bot・host／port・互換epochであれば、再起動後も継続できます。watchでも自動リセットは行いません。シーンや選択肢の構造を変えた後、古いセーブで空応答になることは正常な仕様です。必要なときに画面の「最初から」で明示的にリセットしてください。互換性の自動判定やセーブ移行は行いません。署名鍵は保存root内に生成して再利用し、本番の鍵は使いません。

LINE検証は実PlayerStatusDBをSQLiteへ保存します。Webchatの進行とは共有しません。通常はケースごとの新しいDBで検証し、続きから実行する場合だけ保存先を指定します。

通常の`verify`は、`storage_root/verify-cache/<bot>`に画像とビルド用cacheを保持します。`--case`で1ケースだけを繰り返す場合も再利用し、同じPNGを毎回生成しません。シナリオ自体は毎回最新入力からビルドして検査し、各ケースはその固定成果物を読み込みます。画像・媒体をケースごとに複製せず、Player・NextLabel・sessionを保存するSQLiteだけを独立させます。

画像テキストの内容やframe設定が変われば、そのcacheを再生成します。fontの内容、renderer、画像変換処理、Pillowの版が変わった場合は再描画します。`--session`は指定した保存先のセーブとcacheを継続利用します。同じ保存先・Botに対する複数のbuildは同時実行せず、並行して別コンテンツを検証するときは保存先を分けてください。

```sh
python tools/local_scenario.py verify --settings settings.yaml --bot bot --suite tests/story.json --case 続き --session outputs/session-a
```

次の呼出しには、続けて実行する入力列を渡します。開始actionや前回の入力を自動で再実行しません。期待値が外れても、成功済みのturnのセーブは残ります。同じsessionを複数のrunnerから同時に操作しないでください。

## AI用の結果と期待値

`build`と`verify`はstdoutへJSON一件、ログをstderrへ返します。JSONには入力の出所とhash、失敗したcase／step、実際の最終LINE payload、保存状態、期待値との差、DB・生成画像の絶対pathを含みます。

終了コードは、0が成功、1がビルド・実行・期待値・timeoutの失敗、2が入力・設定・起動の誤りです。取得・build・各caseの上限は既定600秒で、`--timeout`で変更できます。自動再試行はしません。

suiteの例は[examples/local/suite.json](../examples/local/suite.json)にあります。

- 入力は`start`、`text`、`choice`。choiceは直前の最終送信内容の選択肢を0始まりの番号で選び、実postbackを使います。
- 期待値は`texts`、`choices`、`flags`、`absent_flags`。flagsは保存後の値を確認します。
- 準備stepの期待値は省略できますが、期待値のないcaseを成功扱いにはしません。
- ケース内は最初の失敗で止め、他のケースは独立して確認します。
- 乱数seedは同じ実装・入力・初期状態での再現用です。日時やUUID、再開を跨ぐ乱数列の一致は保証しません。

## 媒体と外部通信

画像テキストは同梱fontで生成します。外部URLの画像・音声等をローカルファイルで用意する場合は、URLとファイルの対応を指定します。

```yaml
local:
  storage_root: outputs/local-bot
  public_base_url: http://127.0.0.1:8765/local-media
  assets:
    https://example.com/cover.png: assets/cover.png
    https://example.com/voice.mp3: assets/voice.mp3
  allow_external_media: false
```

asset・資格情報・カスタムfontのpathは設定ファイルからの相対pathです。宣言assetsには画像・音声・動画の拡張子を付けてください。未登録媒体への通信は既定で拒否します。`build`／`webchat`で外部媒体の取得・表示が必要なら`allow_external_media: true`を明示します。`verify`はこの値にかかわらず媒体通信を行わず、未解決のraw媒体を結果に示します。本文・状態の検証成功と、全媒体の表示確認は区別してください。

画像URLの基点は保存rootに固定します。portを変える場合は別rootを使ってください。古い画像テキストcacheに保存されたURLを書き換える仕組みはありません。local serverは選択rootの公開媒体だけを配信し、SQLite・Scenario・設定・署名鍵・診断ファイルは配信しません。

`@delay`／`@forward`、group、外部API連携、任意pluginは初期対象外です。`@log`は使えます。LINEの送信は最終API呼出しで記録し、実送信は行いません。Webchatは既存の対応範囲を維持します。

保存先はローカルディスクを使ってください。`outputs/`はGit管理対象外です。runには主に入力・結果・ケース別SQLiteを残し、画像や宣言assetは内容hashで保存して再利用します。実行が終了し、セーブの再開先にも使っていない不要なrunは手動で削除できます。`verify-cache/<bot>`も実行停止中に削除でき、次回再生成しますが、過去の結果が参照する画像pathも無効になります。cacheを`--session`の再開先には指定しないでください。実行中のserverや`--session`で使っている保存先は残してください。`local_scenario.py`は自動掃除やSheetsへの書戻し、デプロイを行いません。Sheetsへの書戻しは別の同期CLIで明示的に行います。
