# 会話APIを使わないWebchat体験版

限定したシナリオを静的ファイルへ書き出し、ブラウザ内で会話を進めます。画像・音声・動画は必要なときに取得するため、完全オフライン用ではありません。実行時にBot API、クラウドの利用者DB、署名鍵は不要です。

通常版と同じ画面、ブラウザ保存、Quick Reply、固定リッチメニューを使います。同じBotの台本を使うLIFFページも開けます。LINEや通常版へのセーブ移行は行いません。

## 書き出し例を試す

Python 3.11以降と共通依存の`requirements.txt`を使います。書き出しにNodeは不要です。

```sh
cp examples/trial/settings.yaml.template examples/trial/settings.yaml
python3 tools/export_webchat_trial.py \
  --settings examples/trial/settings.yaml \
  --bot trial-demo \
  --output outputs/trial-site

mkdir -p outputs/trial-site/liff
cp examples/trial/liff.html outputs/trial-site/liff/index.html
cp webchat-client/examples/liff/app.js outputs/trial-site/liff/app.js
python3 -m http.server 8767 --bind 127.0.0.1 --directory outputs/trial-site
```

[体験版の画面](http://127.0.0.1:8767/index.html)を開きます。道を選ぶと広場へ進み、固定メニューからページを開けます。ページの「次の場面へ」で同じBotのシーンが変わり、「発話して閉じる」で会話へ戻れます。再読み込み後も進行は残ります。

同梱ページは親子通信の動作確認用です。アプリケーション向けのページ側SDKはXStoryLIFFで管理します。実際の公開では、設定中のLIFFページURLを公開先へ変更し、ページのparentOriginと配信元のframe-ancestorsをWebchatの配信元へ合わせてください。

書き出しには`--title '短い物語' --storage memory --theme examples/webchat-theme`も指定できます。`memory`なら再読み込みや新規タブで最初から始まり、他タブや以前の保存へ影響しません。未指定ならブラウザ保存で続行します。題名・公開テーマフォルダの形式は[API版と共通](webchat-export.md#題名保存方式見た目を指定する)です。

## 入力と設定

体験版は、例のような**書き出し専用の設定ファイル**で管理することを推奨します。同じSheetsドキュメントを参照でき、シートを別のファイルへ複製する必要はありません。設定項目の形式は通常版と共通ですが、体験版Botをサーバーが読む本番設定へ追加する必要はありません。

```sh
python3 tools/export_webchat_trial.py \
  --settings scenarios/trial/settings.yaml \
  --bot trial \
  --tsv scenarios/trial/manifest.json \
  --output outputs/trial-site
```

`--tsv`を省略すると、選択BotのSheets／TSV設定を使います。TSV上書き時は元の入力元の資格情報を読まず、`script_sheet`等の共有設定だけを引き継ぎます。Sheetsを読む場合、サービスアカウントはファイルパスで指定します。Sheetへの書込みは行いません。

設定項目の場所は通常版と共通です。

| 項目 | 設定元 |
|---|---|
| シナリオ入力 | `bots.<bot>.scenario` |
| v3の指定、リセット用の語 | `options.scenario_version: 3`、`options.reset_keyword` |
| ビルド用plugin | `plugins.line`、必要に応じて`line.quick_reply`、`line.image_text`等 |
| 開始・保存互換性 | `plugins.webchat`またはBotのwebchat paramsの`start_action`、`scenario_compatibility_epoch`。両方を指定する |
| 話者・表示・媒体制限 | 同じwebchat paramsの`sender_icon_urls`、`alt_text`、`reply_fallback_message`、`media_origins` |
| 定数の上書き | `plugins.webchat.constants`とBotのwebchat paramsの`constants`。Bot側を優先 |
| 固定リッチメニュー | Bot直下の`richmenus`、`default_richmenu` |
| LIFFページ | webchat paramsの`liff_apps`。連携先botは自分自身にする |
| LIFFの実行設定 | 同じBotのliff paramsの`action_prefix`、`ignore_unhandled_action` |

LINEのアクセストークン、Webchatの署名鍵、APIの許可origin、クラウドScenario URI、LIFFのLoginチャネルIDは要求しません。設定にあっても配布物には含めません。

### サーバー用設定と共用する場合

書き出しツールは設定を書き換えず、Botをサーバーへ公開しません。ただし、同じファイルをLINE／API Webchatのprocessへ渡すと、そこに定義したBotのinterfaceも初期化されます。

- 体験版専用Botではwebchat paramsに`enabled: false`を明示してください。`plugins.webchat.enabled: true`を継承すると、固定Scenario URI等の設定次第でAPI側へ意図せず公開されたり、起動に失敗して他Botにも影響したりします。`enabled`はAPI側の有効化であり、静的な書き出しを無効にはしません。
- 互換epochは、独立して更新したい体験版Bot自身のwebchat paramsへ明示することを推奨します。共通epochを変更して体験版を再生成すると、その新しい配布物では古いセーブが非互換となり「最初から」を求めます。継承自体は許容しますが、更新範囲を意識して使ってください。
- `enabled: false`で止まるのはAPI Webchatです。同じBotのliff interfaceやscenario loaderの初期化、LIFFエンドポイントや管理画面への登録まで止める設定ではありません。LINE／LIFF用の設定要件も残ります。体験版専用Botを管理画面でビルド・運用する必要はなく、これらを意図しない場合は設定ファイルを分けてください。

体験版Botのwebchat paramsの例です。

```yaml
enabled: false
start_action: '##line.follow'
scenario_compatibility_epoch: trial-v1
```

書き出し結果の`warnings`と標準エラーに、API Webchatの有効化と互換epochの継承を知らせます。意図的な設定共用は妨げず、警告だけでは終了コードを失敗にしません。警告がないことを本番設定の安全性の保証とは扱わないでください。


本編と同じスプレッドシートを使い、体験版用のシートだけを選べます。例えばscenario paramsへ次を指定します。

```yaml
script_sheet: '^体験版_'
constant_sheet: '^\$(共通|体験版)$'
```

本編側でも体験版用シート・定数を除外する設定をしてください。既定の広い選別条件では、追加したシートが本編へ混ざることがあります。環境別シートの選択には通常と同じ`XSBOT_DEPLOY_ENV`を使います。

## 対応範囲

- シナリオv3の固定シーン・ラベル移動、固定include／継承／fallback、`@or`、リセット。
- 台詞・固定話者・画像・音声・動画、Button、Confirm、Imagemap、Quick Reply。
- `line.quick_reply`の`＞`、既定の「続きを読む」、再提示、`ignore_pattern`。
- 文字列条件、正規表現、独自オプション`i`・`L`・`N`・`X`。
- 固定メニュー一つの開閉とmessage／postback／URI操作。
- 同一BotのLIFF request、文字列イベント列、sendText、close。

`@set`、フラグ条件、条件セルの`[式]`やAND／OR結合、動的な移動先、`@call`／`@return`、seq／loop／random、外部HTTP命令、forward、delay、group、`@richmenu`による切替、`line.more`、`quick_reply_v2`、独自runtime pluginは初期対象外です。未実行の枝にある非対応命令も書き出し時に診断します。正規表現のパターン内の`|`は使えます。

画像テキストは生成結果で判断します。Quick Reply方式や待ちを生成しない設定は使えます。More方式の命令が出た場合は元の行・frameを示して診断します。 frameの`more_mode`を省略すると既定は`between`で、通常のビルダーがページ数にかかわらず`line.more`を要求します。体験版では`quick_between`（必要に応じて`quick_always`）か、待ちを生成しない`none`を明示すると確実です。

### 入力・定数・正規表現

通常の文字列条件は、NFKC正規化・小文字化した入力への部分一致です。Quick Replyは前処理後の入力と表示文言の完全一致で選びます。`#`等から始まる手入力は通常版と同じく内部actionとして扱いません。

本文・引数・子行の定数はPython側で一度だけ展開します。優先順位はWebchat上書き、定数Sheet、settingsのconstantsです。話者名・条件セルは展開しません。`$$bot_name`は固定できますが、`$$service_name`や入力・状態・マッチ結果などの実行時値は対象外です。未定義名はビルドエラーになります。

| 正規表現の接尾辞 | 意味 |
|---|---|
| `i` | 大文字・小文字を区別しない |
| `N` | 入力をNFKC正規化する |
| `L` | 入力だけを小文字化する |
| `X` | 最初のマッチが入力先頭から全体を覆う場合だけ一致する |

組合せも使えます。`/yes/NLX`は`ＹＥＳ`に一致します。NとLは記述順にかかわらずNFKC→小文字化の順です。条件正規表現の`.`は改行にも一致します。`X`はパターンをアンカーで包む処理ではなく、例えば`/a|ab/X`の入力`ab`は、最初のマッチが`a`なので不一致です。

元台本はPythonビルダーの構文検査を通る必要がありますが、ブラウザでのマッチはJavaScriptのRegExpに従います。Python固有の構文や文字範囲を変換・補正しません。全パターンをページ初期化時にコンパイルし、JavaScriptで使えなければ進行を開始しません。

Quick Replyの`ignore_pattern`は、現在と同じく区切りや接尾辞のないパターンです。正規化やDOTALLは暗黙に加えません。

例えば`/[\w]+/X`はJavaScriptでは`日本`に一致せず、`/\d/X`は全角の`１`に一致しません。構文が正しくても意味が異なる例です。これらを自動変換したり、使用しただけで警告したりはしないため、実際に受け付けたい入力を体験版で確認してください。


### 固定メニューとLIFF

メニューは既存の定義形式を使い、`default_richmenu`で指定した一つだけを書き出します。利用者の開閉状態は保存しますが、シナリオでの切替は行いません。メニューのmessage／postbackは会話のQuick Reply guardに従います。常時開ける案内にはURIを使えます。

LIFFは同じBotの現在シーンを使い、`action_prefix`（既定`##liff.`）を付けて実行します。requestには会話用のQuick Reply前処理を適用しません。台詞は定数展開した文字列の配列として返し、JSONらしい文字列もそのまま返します。シートへJSONの括弧を書く場合は、通常の書式と同じく`{{`／`}}`で記述します。

LIFFでシーンを移動すると会話のシーン・操作世代も変わり、現在の選択肢表示は消えます。ただしQuick Reply待ちを自動で解除するわけではありません。情報表示だけなら移動しないラベルで処理し、会話を進めたい場合はsendTextを使うと導線を揃えやすくなります。

LIFFのrequestから画像・Quick Reply等の会話用命令を実行した場合は、その入力を失敗にして保存しません。`ignore_unhandled_action`は入口の未定義入力だけに適用し、途中の移動先欠落はエラーラベルへ進みます。

入力の不一致は次のように扱います。

- 手入力等の通常actionが見つからなければ`##error_unhandled_action`を探します。
- `#`で始まる内部actionや移動先シーンが見つからなければ`##error_invalid_label`を探します。
- 既定のLIFF actionは`##liff.`で始まるため、入口の不一致も`ignore_unhandled_action: false`なら`##error_invalid_label`になります。trueなら入口の不一致だけを空のイベント列で返し、途中の移動先欠落は引き続きエラーラベルへ進みます。

エラーブロックも呼出し元と同じモードで実行します。LIFFから呼ばれ得るエラーブロックは台詞だけにしてください。Button等を含むと、そのLIFF操作は失敗して保存されません。入口の未定義actionを空応答にしたい場合は、例と同じく`ignore_unhandled_action: true`を使えます。エラーラベルもなければ再度エラー探索はしません。


## 配布・保存・更新

生成されたフォルダを、任意のサブパスの静的ホスティングへ置けます。HTML、JavaScript、CSS、台本JSON、同梱媒体を配信してください。`file://`起動は対象外です。

LIFFやメニュー等のURL欄に`127.0.0.1`、`localhost`、`::1`等のloopbackが残ると、結果JSONの`warnings`に場所を返し、標準エラーにも表示します。手元で例を動かす用途は許容して書き出しを続けます。公開時には、閲覧者自身の端末を指してしまうURLを配信先へ変更してください。


- 台本JSONは一件取得し、会話はブラウザ内で実行します。媒体は同梱ファイルか指定したHTTPS URLから表示時に取得します。
- ビルド時は外部画像の取得があり得ます。`local.assets`で手元のファイルを対応付けられ、`local.allow_external_media: false`ならビルド時の外部媒体取得を禁止できます。
- 非公開のビルドcacheは設定ファイル横の`outputs/.trial-cache/<bot>`へ置きます。cache、元設定、資格情報、pickle、SQLiteは配布しません。別のコンテンツ用リポジトリでも`.trial-cache/`をGit管理から除外してください。
- 再生成では内容hash付きのassetを先に置き、index.htmlを最後に更新します。旧assetと利用者が追加したページは自動削除しません。アップロードも入口を最後にしてください。
- 既定では進行を配信originとBot名に対応する体験版専用キーで保存し、通常版と混ぜません。同じoriginで複数の体験版を分離する場合はBot名も分けます。`--storage memory`なら各ページ内だけで保持します。
- 台詞・媒体だけの修正ではepochを維持できます。シーン・生成ラベル・待ち状態等を非互換に変える場合は`scenario_compatibility_epoch`を変えます。不一致では保存を黙って捨てず「最初から」を案内します。
- ブラウザ保存が使えなければ、そのページ内だけの進行へ切り替えて通知します。複数タブの競合では先に確定した保存を維持します。

配布した台詞・分岐・結末は利用者から読めます。セーブも署名のないデータなので変更できます。秘密情報を台本・定数へ入れず、体験版のセーブをLINEやサーバー側の信頼できる状態として取り込まないでください。

標準出力はJSON一件、ログは標準エラーです。終了コードは成功0、台本の非対応・検証失敗1、引数・設定・入出力失敗2です。1入力の実行は10,000命令・100出力を上限とし、超過時は途中の応答や状態を保存しません。

## 検証

通常のPythonテストに変換・書き出しの回帰が含まれます。JSの検証は別に実行します。

```sh
./test.sh
node --test webchat-client/test.mjs webchat-client/storage.test.mjs webchat-client/trial.test.mjs webchat-client/liff-host.test.mjs tests/webchat_ui_logic.test.mjs
```

PythonとJSは`tests/fixtures/trial/`の台本・期待値を共有します。通常テストはfixtureを書き換えません。意図して書き出し形式を変えた場合だけ、`python3 -m tests.test_trial_scenario --write-fixture`でprogram.jsonを生成し、差分を確認してください。

画面と実保存の検証には、上記の例を専用のローカル静的サーバーで配信したうえで、次を追加します。テストはtrial-demoと専用テストIDの保存だけを作成・削除します。

```sh
cp tests/webchat_trial.browser.test.mjs outputs/trial-site/trial-test.mjs
cp examples/trial/test.html outputs/trial-site/trial-test.html
```

[画面・保存テスト](http://127.0.0.1:8767/trial-test.html)で、4画面サイズの選択・媒体・メニュー・LIFF・再開、実IndexedDB、並行入力、epoch変更、reset、履歴削除を検証します。実機のキーボードや各モバイルブラウザ固有の挙動までは再現しません。

保存方式とテーマも確認する場合は、同じ例を追加で書き出します。

```sh
python3 tools/export_webchat_trial.py \
  --settings examples/trial/settings.yaml --bot trial-demo \
  --output outputs/trial-site/memory --storage memory \
  --title 'スマートフォンの幅でも操作しやすい長い題名の例' \
  --theme examples/webchat-theme
```

[追加オプションの画面テスト](http://127.0.0.1:8767/trial-test.html?options=1)では、同一Botの永続保存と複数のmemoryページを同時に開き、進行・LIFF・メニュー開閉が混ざらないこと、題名とテーマが反映されることも確認します。
