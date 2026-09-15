# Webchat UIの静的配信

既存のWebchat画面を静的ファイルへ書き出し、APIとは別のorigin・任意のサブパスへ配信できます。フロントを外部hosting、会話APIをAWS、画像・音声・動画を既存のS3＋CloudFrontに置く構成です。公開メディアのURLはLINEとも共用できます。

書出しに指定する接続設定は公開APIの基点URLとBot名だけです。`settings.yaml`や資格情報は読みません。Scenario本体、署名鍵、秘密設定、メディア本体は配布物へ含めません。APIの準備は[Webchatガイド](webchat.md)を参照してください。

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

1回の書出しで次の公開6ファイルを生成します。

```text
index.html
LICENSE
assets/<内容hash>/app.js
assets/<内容hash>/style.css
assets/<内容hash>/ui_logic.js
assets/<内容hash>/client.js
```

同じ出力先へ再生成すると、入口の`index.html`を最後に差し替えます。assetの内容が変われば別のhashディレクトリを使い、旧assetや利用者が追加したファイルは削除しません。

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

進行と履歴はブラウザのhosting origin内に、API基点URLとBot名の組合せで保存します。同じorigin・API基点・Botなら、配布サブパスを変えても保存を共有します。hosting originやAPI基点、Botを変えた場合、既存の保存は自動で引き継がれません。API側の署名鍵・互換epochによる制約は[Webchatガイド](webchat.md)のままです。
