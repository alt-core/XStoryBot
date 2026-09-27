# Webchat UIの静的配信

既存のWebchat画面を静的ファイルへ書き出し、APIとは別のorigin・任意のサブパスへ配信できます。フロントを外部hosting、会話APIをAWS、画像・音声・動画を既存のS3＋CloudFrontに置く構成です。公開メディアのURLはLINEとも共用できます。

書出しに指定する接続設定は公開APIの基点URLとBot名だけです。`settings.yaml`や資格情報は読みません。Scenario本体、署名鍵、秘密設定、シナリオのメディア本体は配布物へ含めません。見た目用の素材は、後述するテーマフォルダから明示的に同梱できます。APIの準備は[Webchatガイド](webchat.md)を参照してください。

## 書き出す

Python 3.11以降の標準ライブラリだけで実行できます。Node、bundler、追加packageは不要です。

```sh
python3 tools/export_webchat.py \
  --api-base-url https://api.example.com \
  --bot bot \
  --output outputs/webchat-site
```

`--api-base-url`にはAPIの基点を指定します。`https://api.example.com/stage`のようなbase pathも使えますが、`/api/webchat/v1/bots/.../turn`は含めません。認証情報、query、fragmentは指定できません。本番はHTTPSを使い、HTTPはローカル確認用にしてください。

出力先は未作成・空・このツールで生成済みのディレクトリを指定します。標準出力には生成先とファイル一覧を含むJSONを一件返します。終了コードは成功が`0`、入力・保存エラーが`2`です。

既定では1回の書出しで次の公開7ファイルを生成します。

```text
index.html
LICENSE
assets/<内容hash>/app.js
assets/<内容hash>/style.css
assets/<内容hash>/ui_logic.js
assets/<内容hash>/client.js
assets/<内容hash>/liff-host.js
```

同じ出力先へ再生成すると、入口の`index.html`を最後に差し替えます。assetの内容が変われば別のhashディレクトリを使い、旧assetや利用者が追加したファイルは削除しません。

## 題名・保存方式・見た目を指定する

API版と[API不要の体験版](webchat-trial.md)の書き出しで、同じオプションを使えます。

```sh
python3 tools/export_webchat.py \
  --api-base-url https://api.example.com --bot bot \
  --output outputs/webchat-site \
  --title '短い物語' --storage memory --theme examples/webchat-theme
```

- `--title`: ブラウザのタブと画面見出しの題名。既定は`Webchat`です。HTMLではなく文字列として表示し、長い見出しは画面幅に合わせて省略します。
- `--storage indexeddb`: 既定。進行・履歴をブラウザに保存し、再読み込み後も続行します。
- `--storage memory`: 進行・履歴・メニュー開閉を、そのページのメモリーだけに保持します。新規タブや再読み込みでは最初から始まり、別のタブや以前のブラウザ保存は変更しません。タブの切替では続行します。ブラウザの「戻る・進む」で同じページが復元された場合も、その進行を維持します。
- `--theme`: 公開専用フォルダ。直下の`style.css`を参照UIのCSSの後に読み込みます。未指定なら標準の見た目です。

テーマは`assets/<内容hash>/theme/`へフォルダ構造を保って同梱します。例えば次の構成なら、CSSから`url('./fonts/story.woff2')`や`url('./images/header.svg')`で参照できます。複数のCSSは`@import './colors.css';`等でまとめられます。フォントを変更する場合は、`@font-face`を定義したうえで`--font`変数へ指定してください。

```text
theme/
  style.css
  colors.css
  fonts/story.woff2
  images/header.svg
```

このフォルダの通常ファイルをすべて公開します。`.DS_Store`等、名前が`.`で始まるファイル・フォルダは除外し、シンボリックリンクは受け付けません。設定や非公開ファイルを混ぜず、出力先とは互いに含まれない別の場所に置いてください。CSSの解析・URL書換え・外部素材の取得は行いません。同梱素材の変更も内容hashへ反映します。

色・フォントは`--accent`、`--bg`、`--bubble-in`、`--font`等のCSS変数で調整できます。明暗両方の配色を[テーマ例](../examples/webchat-theme/style.css)に示しています。CSSの指定はスマートフォンとPCへ共通に適用されます。同梱CSS・画像・フォントは現行CSPで読めますが、外部フォントCDN等の許可を自動で増やすことはありません。

メモリー保存はAPIの要否とは別の設定です。API版では通常どおり会話APIを呼びます。LIFFの進行も、親Webchatが選んだ保存方式に従います。`tools/local_scenario.py webchat`での題名・テーマ指定は、この書き出しオプションの対象外です。

## APIとhostingを接続する

例えば配布先が`https://www.example.com/games/story/index.html`なら、AWSの`WebchatAllowedOrigins`へ`https://www.example.com`を追加します。`deploy_aws.sh`では`XSBOT_WEBCHAT_ALLOWED_ORIGINS`で指定できます。登録するoriginには`/games/story`等のパスを含めません。この設定はAPI側で行います。

生成先のフォルダ構成を保ってuploadします。更新時の順序は次のとおりです。

1. `assets/<内容hash>/`内のファイルをuploadする。
2. `LICENSE`をuploadする。
3. `index.html`を最後にuploadする。

入口は配布した`index.html`です。assetとmoduleの参照は相対URLなので、hosting側のURL書換やアプリ用routeは不要です。生成ファイルをHTTP/HTTPSで配信してください。

hostingには、HTMLを`text/html`、CSSを`text/css`、`.js`を`text/javascript`等のJavaScript MIMEで配信する必要があります。配布物のJavaScript拡張子はすべて`.js`ですが、誤ったMIMEで配信されるとES moduleは実行されません。[MDNのmodule配信説明](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Modules)

生成HTMLのmeta CSPは、指定APIへの接続とHTTPSメディア等を許可します。ただし、hostingがHTTP headerで付けるCSPをmetaで緩和することはできません。hosting側が外部APIへの接続やJavaScriptを禁止している場合は、その配信条件の調整が必要です。metaでは`frame-ancestors`等も設定できないため、API側の参照UIと同じHTTP header一式を再現するものではありません。[CSPのmeta仕様](https://www.w3.org/TR/CSP/#meta-element)

## 更新とブラウザ保存

assetは内容hashで区別しますが、hostingやブラウザが古い`index.html`を返すと古い画面が使われます。設定可能ならHTMLは再検証または短いcacheとし、更新時にはhostingのcache削除等で新しいHTMLが配信されることを確認してください。

旧assetの自動削除は行いません。古いHTMLのcacheや開いたままの画面を考慮し、不要と判断した旧hashディレクトリだけを手動で削除してください。

既定の`indexeddb`では、進行と履歴をブラウザのhosting origin内に、API基点URLとBot名の組合せで保存します。同じorigin・API基点・Botなら、配布サブパスを変えても保存を共有します。hosting originやAPI基点、Botを変えた場合、既存の保存は自動で引き継がれません。API側の署名鍵・互換epochによる制約は[Webchatガイド](webchat.md)のままです。
