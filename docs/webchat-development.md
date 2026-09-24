# Webchat開発用確認

参照UIを外部サービスやcredentialなしで確認するためのローカル手順です。本番APIのHMACやAWS構成を検証するものではありません。

## モックserver

標準ライブラリだけで起動できます。

```sh
python3 tools/webchat_dev_server.py
```

`http://127.0.0.1:8765/chat/bot`を開き、次のkeywordで主要表示を確認します。

```text
image / audio / video / button / imagemap / long / more / slow / error / liff
```

`video`は音声track付きの小型fixtureを使い、一覧のposter、1回のtapによる拡大表示と再生開始、動画面tapによる再生／一時停止、native controlsを表示しないことを確認できます。`button`は通常HTTPSのチャット内iframeと、`openExternalBrowser=1`を付けた外部tab用linkを確認できます。モックのstate token／postbackは非署名で、HMAC検証には利用できません。

`image`と`video`は、メディアを選ぶとチャット領域内の拡大表示を開きます。背景、閉じるボタン、Escで閉じられることを確認します。`imagemap`はチャット領域の横幅で表示し、設定されたhotspot以外を選んでもactionを実行しません。

`long`は4メッセージごとに「続きを読む」のQuick Replyを表示し、3回に分けて全12メッセージを確認します。これはScenarioで4メッセージごとに`＞`を置き、`line.quick_reply`の`default_reply`を「続きを読む」にする構成に対応します。

`liff`は別Bot相当の状態を持つiframeの確認用です。「イベントを進める」で値を増やし、閉じて開き直しても値が残ること、発話して閉じると会話へ1回だけ送信されることを確認します。

## browser保存テスト

モックserverを起動した状態で、[保存テスト画面](http://127.0.0.1:8765/devtest/storage)を開きます。実IndexedDBを使い、履歴削除後の選択肢保持、再読込、transactionの中断、容量不足時のmemory移行を確認します。画面に成功と確認項目が表示されることを確認してください。失敗時は同じ画面へ例外を表示します。

テスト専用の人工Botの記録だけを作成し、終了時に削除します。外部APIやモックのturn APIへの通信は行いません。

`tests/webchat_storage.browser.test.mjs`はbrowser専用です。`node --test`では実行せず、上記の画面から実行してください。Node回帰テストだけの成功では、IndexedDBの動作を確認したことにはなりません。

## Node回帰テスト

```sh
node --test webchat-client/test.mjs webchat-client/liff-host.test.mjs tests/webchat_ui_logic.test.mjs
```

headless clientに加え、同一controlの二重発火防止、動画完了actionの送信中待機と一回送信、controlなし動画の再生切替、URIのiframe／外部tab分類を確認します。

## Python契約テスト

`./test.sh`にはモックresponseと参照UIの静的契約テストが含まれます。実browserでは別途、reload、複数tab、320px幅、200%文字、IME入力を確認してください。

チャットで`richmenu`と入力すると、メニューの開閉・入力・LIFFへのリンクを確認できます。
