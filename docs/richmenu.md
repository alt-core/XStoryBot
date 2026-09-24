# リッチメニュー

Botごとにメニューをsettingsへ定義し、シナリオでは`@richmenu main`のように論理名を指定します。LINEとWebchatで同じ領域定義を使い、URL等はWebchatの定数で上書きできます。

以下は、既存settingsの環境セクション（`*`や`prod`）へ追加する部分です。LINE／Webchatのinterface設定は別途必要です。

```yaml
constants:
  liff_menu: https://liff.line.me/YOUR_LIFF_ID
plugins:
  webchat:
    constants:
      liff_menu: &web_menu https://pages.example.com/menu/index.html
bots:
  story:
    default_richmenu: main
    richmenus:
      main:
        image: https://media.example.com/menu.png
        size: {width: 2500, height: 843}
        chatBarText: メニュー
        selected: false
        areas:
          - bounds: {x: 0, y: 0, width: 1250, height: 843}
            action: {type: postback, data: '#help', displayText: ヘルプ, label: ヘルプ}
          - bounds: {x: 1250, y: 0, width: 1250, height: 843}
            action: {type: uri, uri: '{liff_menu}', label: 持ち物}
    interfaces:
      - type: webchat
        params:
          liff_apps:
            menu: {url: *web_menu, bot: menu}
```

論理名は小文字英数字で始まる64文字以内の英数字・`_`・`-`です。`richmenu-`で始まる名前は生IDとの混同を防ぐため使えません。参照側は全角・大文字を正規化します。`default_richmenu`は省略でき、その場合LINEの既定は変更しません。

`@richmenu`の引数は、`richmenu-`で始まる場合だけ生IDとして扱い、それ以外は論理名として解決します。未定義の論理名はエラーです。定数・変数で指定した名前も、実行時に状態保存より前に検査し、ローカルの`verify`でも失敗として報告します。生IDはWebchatでは何も変更しません。

定義中の文字列は`{定数名}`を一段だけ展開します。`{{`／`}}`は括弧そのものです。属性・添字・書式指定等は使えません。参照先はsettingsの定数で、Webchatでは上書きを重ねます。定数Sheetは定義から参照しません。本文と定義で同名の値が異なる場合はビルド時に警告します。

シナリオのビルドでは論理名とLINE用IDの対応を検査し、メニューのURL等は展開しません。定義の展開・値の検査は、LINEでは管理画面の差分確認・反映時、Webchatでは起動時にそれぞれの定数を使って行います。Webchat専用の定数を使うメニューも、LINE用の値を補わずにビルドできます。

画像はHTTPSのPNG／JPEG、1MB以下です。幅800〜2500、高さ250以上、幅÷高さ1.45以上で、設定のsizeと実画像の寸法を揃えます。領域は1〜20件、actionはmessage／postback／uriに対応します。postback.dataには`@@`を含められません。URIはHTTPSとtelに対応します。Webchatの画像は`media_origins`の設定も満たす必要があります。[LINEの画像仕様](https://developers.line.biz/en/reference/messaging-api/#upload-rich-menu-image)

## LINEへの反映

1. settingsを更新・デプロイします。
2. 管理画面でBotを選び、リッチメニューの「差分を確認」を押します。
3. 内容を確認して「LINEへ反映」を押します。メニュー単位で順に処理し、最後に既定メニューを設定します。
4. シナリオをビルドします。論理名とIDの対応が成果物に入ります。

対応表はObjectStoreのprivate領域`richmenu/<Bot名>.json`へ記録します。settingsへは書き戻しません。実行時は成果物に入った表を使うため、追加のObjectStore読込みは発生しません。

各メニューの成功を記録するので、途中失敗は差分を再確認して再実行できます。画面を閉じても開始したHTTP要求を取り消したことにはなりません。自動再送は行いません。作成と記録の間で失敗した場合、未記録のメニューが残ることがあります。旧メニューの削除や一括付け替えは行いません。

APIで作ったメニューはLINE Official Account Managerでは管理できません。不要なものを整理する場合は、公式の一覧・削除APIでIDを確認して手動操作してください。利用者が使っているIDを削除しないよう注意してください。作成上限は100件/時、保持上限は1000件です。[LINEの管理方式](https://developers.line.biz/en/docs/messaging-api/rich-menus-overview/)／[削除API](https://developers.line.biz/en/reference/messaging-api/#delete-rich-menu)

LINEの利用者別紐付けは、再び`@richmenu`を実行するまで旧IDのままです。既定メニューの変更は、利用者別紐付けのない人に反映されます。Official Account Manager等で既定が設定されている場合は「外部管理」と表示して置換対象にします。

## Webchat

定義を直接表示するので、LINEへの反映は不要です。入力欄の左のメニューボタンで開閉し、タップ後も開いたままになります。開閉は端末に保存し、別名のメニューへ切り替わると`selected`を初期値にします。

表示する論理名を署名セーブに保存します。`@reset`では選択メニューを維持し、画面の「最初から」では既定へ戻ります。同名の定義更新は次の応答で反映します。旧表示を維持したい場合は別の論理名で定義してください。

操作時にメニュー名・定義のrevision・領域番号を送ります。表示と現在の定義が違う場合は操作を実行せず、同じセーブと最新のメニューを返します（HTTP 200、`menu_updated: true`）。clientは履歴を増やさず表示と案内だけを更新します。操作を自動でやり直しません。

messageの領域は通常入力と同じ処理、postbackは指定actionの実行です。URIは通常のチャットと同じiframe／LIFF連携を使います。

LIFFの操作からも`@richmenu`を使えます。Webchatでは実行中Botの保存状態のメニューを変更し、LINEでは同じBotに設定されたLINE interfaceで利用者へ紐付けます。LINE側は同じBotの`line` interfaceが必要です。コマンド自体はLIFFのイベント配列へ文字列を追加せず、LINEの本文もpushしません。別Botの独立した状態では、そのBotのメニューを更新します。会話側の表示を直接切り替えたい場合は、会話Bot自身をLIFF連携先にする構成が簡単です。

画像を差し替える場合はURLも変更してください。Webchatは画像本体を毎回取得して比較せず、ブラウザや配信元のキャッシュも使うためです。

ローカルでは画像を`local.assets`へ登録できます。Webchat用のURI上書きには、ポート番号付きの`http://127.0.0.1`も使えます。`verify`のLINE記録ではIDが`xsb-local-<論理名>`になります。実LINEへの反映は行いません。Webchatの上書きを使った表示はローカルWebchatで確認してください。
