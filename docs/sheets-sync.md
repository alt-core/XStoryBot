# Google SheetsとTSVの往復

`tools/sheets_sync.py`は、SheetsからTSVの作業コピーを作り、ローカルで確認した編集を書き戻す補助CLIです。Sheetsを正本として使い、同じ作業コピーへの同期CLIは一つずつ実行してください。通常のエンジン起動やクラウドの状態保存先は必要ありません。依存は既存の`requirements.txt`に含まれます。

## 取得とローカル検証

Google Sheetsを入力元にしているBotの設定から取得できます。設定は`*`と`XSBOT_DEPLOY_ENV`の環境設定、共通options、`plugins.google_sheets`、Botの`scenario.params`の順に解決します。

```sh
python tools/sheets_sync.py pull --settings settings.yaml --bot bot --output outputs/scenario-work
```

Bot設定がTSVの場合や、設定ファイルを使わない場合は直接指定します。

```sh
python tools/sheets_sync.py pull --sheet-id YOUR_SPREADSHEET_ID --credentials credentials/sheets-reader.json --output outputs/scenario-work
```

出力先は未作成のディレクトリにしてください。途中まで取得したコピーは公開せず、完成後に出力先へ移します。既存の編集ファイルをpullで上書きしません。

既定ではローダーのシート選択条件を使います。`_`で始まるシートを除き、`$`定数を含め、環境別シートは全環境分を取得します。シートの名前と順序を保ち、`story`と`story.test`等は別TSVへ保存します。必要な環境の選択と連結はビルド時に行います。

`--all-sheets`で選択条件を外し、補助シートも取得できます。`--sheet 'シート名'`は複数回指定でき、取得・比較・書込みを限定します。明示したシート名は既定の除外条件より優先するため、`--sheet _helper`だけでその補助シートを取得できます。

出力には以下が含まれます。

- `manifest.json`：既存TSVローダー用のシート一覧。
- `sheet-<sheetId>.tsv`：編集するシナリオ。UTF-8、標準CSVのタブ区切り形式。
- `sheets-sync.json`：取得元ID、シートIDと名前、行列数、取得日時。
- `.sheets-sync/base-<sheetId>.json`：型付きの基準値と内容hash。手で編集しないでください。

manifestと同期情報、TSVのファイル名は維持してください。`.sheets-sync`は削除してよいcacheではなく、ローカル検証にも必要な入力です。編集するのはTSVで、それ以外はツールが管理します。移動・保管するときは作業フォルダ一式を保持してください。

```sh
python tools/local_scenario.py verify --settings settings.yaml --bot bot --tsv outputs/scenario-work/manifest.json --suite examples/local/suite.json
python tools/local_scenario.py webchat --settings settings.yaml --bot bot --tsv outputs/scenario-work/manifest.json --watch
```

ローカル検証用のsettingsには`cloud.provider: local`等の[ローカル開発設定](local-development.md)が必要です。

## 差分の確認と書き戻し

取得元IDは作業コピーに記録されるので、以降は資格情報だけの指定でも使えます。`--settings`と`--bot`による指定も可能です。`--credentials`は設定内の資格情報ファイルより優先します。

```sh
python tools/sheets_sync.py diff --manifest outputs/scenario-work/manifest.json --credentials credentials/sheets-reader.json
python tools/sheets_sync.py push --manifest outputs/scenario-work/manifest.json --credentials credentials/sheets-editor.json --dry-run
python tools/sheets_sync.py push --manifest outputs/scenario-work/manifest.json --credentials credentials/sheets-editor.json
```

`diff`と`push --dry-run`は同じ差分を返し、Sheetsや同期基準を書き換えません。標準出力はJSON一件で、シート別の変更行数・競合の有無と、詳細ファイル`diff_file`のpathを返します。

Sheets側でシートが改名された場合も、同じsheetIdの現在のシートを比較し、警告と`remote_name`を返します。削除されたシートは`remote_status: deleted`として示し、値を取得できる他のシートの比較を続けます。これらを含む実pushは停止するため、新しくpullしてから作業を続けてください。

現在のSheetsの行列数を超える編集でも差分は確認できます。その場合はシートの結果に`write_error`を含め、実際のpushを止めます。Sheets側で削除された行との競合も、差分を見て判断できます。

`.sheets-sync/diff.jsonl`には、差分がある行だけを一行一JSONで保存します。`sheet`と1始まりの`row`、取得時の`base`、編集中の`local`、現在のSheetsの`remote`を型付きのセル配列で記録します。大量の台本全体を標準出力やメモリへ集めず、詳細ファイルは直近の比較結果で置き換えます。

pushは次の順序で進めます。

1. ローカルの変更候補を固定します。送信中のローカル編集は次回の差分に残ります。
2. ローカルで変更したシートだけを再取得し、シート単位で基準値と比較します。一つでも競合すれば、書き込み前に停止します。
3. 書き込み前の値をバックアップします。保存できなければ書き込みません。
4. 変更した行のA:Zだけを書きます。連続行をまとめ、大きいデータは分割します。短くした台本の末尾の値も消します。
5. 書き込み応答の値を確認し、そのシートの全変更が確認できたら同期基準を更新します。`--verify`を付けると、応答確認に加えてSheetsを再取得して照合します。

変更がないpushは認証・通信・書き込みを行いません。前回の応答が不明でも、再取得したシート全体が今回の固定候補と一致する場合は、再送せず`already_applied`として基準を更新します。

変更がないpushではSheetsを比較していないため、差分ファイルも更新せず、`diff_file`を返しません。直前のdiff結果は残ります。

## セルの型と式

取得するのはA:Zの入力値と式です。Zより右は読み書きしません。TSVの範囲外が空列だけなら無視し、値や空白文字があれば誤って切り捨てないよう停止します。通常のGRIDシートが対象です。配列数式等で展開された「直接の入力値がない計算結果」を含むシートは、値を欠落させないよう位置を示して取得を停止します。

TSVには式を原文で保存します。数値はAPIが返した数値の文字列表現、真偽値は既存ローダーに合わせて`True`／`False`で保存します。タブ・改行・引用符を含むセルは標準CSVの引用規則で保持します。

書き戻す型は次の規則です。

| 編集内容 | 書き戻す型 |
|---|---|
| 変更のないセル | 基準に保存した元の型 |
| 非空のセルを空欄へ変更 | 値の消去 |
| 編集後、先頭の空白を除いて`=`で始まるセル | 元の型によらず式 |
| 元が数値で、編集値も数値として読める | 有限の十進数・指数表記を数値として受理 |
| 元が真偽値で、編集値も真偽値として読める | 大文字小文字を問わず`TRUE`／`FALSE`を受理 |
| その他の新規・編集値 | 文字列。元が文字列や空欄の`001`、`TRUE`、日付に見える値等を勝手に変換しない |

取得時の型は編集を制限するスキーマではありません。説明行の追加、定数リストの削除、ブロックの移動も行えます。以前は数値・真偽値だった位置へ別の文字列が来ても、その文字列を受け入れます。数値の誤入力が常にエラーになるわけではないため、diffとシナリオの検証結果を確認してください。`1e999`等、数値構文から非有限数になる値は送信できません。

未変更かどうかと元の型はセル位置で比較します。行の移動後も、Sheets上の型まで移動元と一致するとは限りません。型を明示して変更したい場合はSheets側で変更して再取得してください。TSVで行を編集しても式の参照先は自動補正しないので、必要なら参照式も編集してください。

同期ツール自体は式を計算しません。TSVの`evaluate_formula: true`は[セル参照と`&`連結の限定機能](local-development.md#tsvのセル参照と文字列連結)です。参照する補助シートもmanifestへ含めてください。A:Z外の値、未取得のシート、Sheets固有の関数や型・表示書式まで含む計算結果との一致は保証しません。

同期コピーでは、基準値と同じ位置・同じ文字列の`=A1`等は、元が文字列ならローカルでも文字列として扱います。そのセルを別の式から参照しても計算しません。編集した`=`開始セルは式として扱うため、文字列として表示したい新しい値は`="=表示したい文字列"`のように書けます。同期情報や必要な基準ファイルが破損している場合は、型を推測せずエラーにします。

ローカルの読込みでもpushと同じ値変換を使うため、元が数値の`+01`と`1`、元が真偽値の`TRUE`と`True`等は、書き戻す値と同じ表現で検証できます。元が文字列の`001`や`TRUE`は文字列として保ちます。この扱いは`evaluate_formula`の有無によらず同じで、同設定は式の計算だけを切り替えます。

## 認証と変更範囲

資格情報は明示したサービスアカウントJSONファイルだけを使い、自動探索はしません。対象のスプレッドシートをそのサービスアカウントへ共有してください。pull／diff／dry-runには閲覧権限、pushには編集権限が必要です。

`.netrc`によるHTTP認証の上書きは使わず、proxyや証明書の環境設定は維持します。

読み取り操作は`spreadsheets.readonly`、push用セッションだけ`spreadsheets`スコープを使います。既存のエンジン用ローダーはreadonlyのままです。Drive APIや追加のクラウド資源は使いません。

更新するのはセルの入力値だけです。通常のセル書式、メモ、列幅、保護範囲を設定するAPIは呼びません。ただし、更新したセルの部分的な文字装飾やスマートチップが失われる場合があります。[Googleのセル仕様](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/cells)

シートの追加・削除・改名と、グリッドの行列追加は行いません。必要な行数・列数はSheets側で用意してください。manifestから外したシートは警告だけを返し、Sheets上では削除しません。`--force`や自動マージはありません。

## 競合・中断・復旧

比較と書き込みは一つのアトミック操作ではありません。比較後に他者が同じセルを編集すると、上書きする場合があります。書き込み後の再取得も、その失われた編集の検出を保証するものではありません。

自動判定は、見つかった競合や誤った書き込み先を知らせる補助です。検出できた問題は確認し、検出されなかったことだけをシナリオの正しさや同時編集の安全性の保証にはしないでください。

バックアップの既定先は`outputs/sheets-backup/<日時と識別子>/`です。`--backup-dir`で親ディレクトリを変更できます。manifest＋TSVと型付きの旧値に加え、`.sheets-sync/planned-<sheetId>.json`へ実際の送信候補も残します。値のバックアップであり、書式等を含むスプレッドシート全体のコピーではありません。

分割書き込みの途中失敗では、以前のバッチまで成功している場合があります。JSONの`writes`にシート別の状態と、確認済みの`written_rows`を返します。

| status | 状態 |
|---|---|
| `pending` | 未実行 |
| `partial` | 一部の行だけ成功を確認 |
| `unconfirmed` | 書き込み結果または値の照合が未確認 |
| `rejected` | 書き込みが拒否された |
| `awaiting_verify` | 書き込み応答を確認し、追加の再取得待ち |
| `confirmed` | シート全体の変更を確認し、基準を更新済み |
| `already_applied` | 現在のSheetsが候補と一致したため、再送せず基準を更新済み |

HTTPエラーやCtrl-Cで結果が不明になった書き込みを自動再送・自動復元しません。競合や部分失敗が残る場合は、新しいディレクトリへpullし、元の作業コピーやバックアップと比較して、必要な編集だけを取り込んでください。差分を再確認してから通常のpushで反映します。Sheetsの版履歴による復旧も利用できます。

バックアップのmanifestを直接pushしても復元にはなりません。バックアップ内のTSVと基準値は同じなので、未変更と判定されます。また、新しいコピーへ旧TSVの値を移しても、元のSheetsの型まで自動復元するわけではありません。型も含めて戻す必要がある場合は、保存した型付きデータを確認してSheets側で直すか、版履歴を使ってください。

標準出力は通常の実行ではJSON一件、ログは標準エラーです。終了コードは`0`が成功、`1`がpushの競合・照合不一致・書き込み対象の削除や改名、`2`が入力・認証・通信エラーや中断です。値の差分やシート削除・改名を報告するdiff／dry-runは`0`を返します。

## 大量データ

取得は5シートずつ行い、必要な値をファイルへ順次保存します。pushはローカルで変更したシートだけを比較し、変更行をまとめて送信します。送信payloadは約1.8MBを目安に分割しますが、単一行は分割しません。

同じCLI内で直近60秒に55回の書き込みへ達した場合だけ、必要な時間を待機します。通常の少数回のpushは待ちません。別プロセス等と資格情報を共有した際のAPI上限までは管理せず、書き込みの429は停止して報告します。[Googleの利用制限](https://developers.google.com/workspace/sheets/api/limits)

差分ファイルは直近一件に置き換えます。バックアップは自動削除しません。復旧に不要になったものは利用側で削除してください。
