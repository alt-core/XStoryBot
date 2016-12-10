# coding: utf-8

LINE = {
    # LINE Messaging API
    'access_token': '<<LINE_ACCESS_TOKEN>>',
    'channel_secret': '<<LINE_CHANEL_SECRET>>',
}
SHEETS = {
    # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
    'key_file_json': 'path_to_keyfile.json',
    # シナリオの Google Sheet ID (閲覧者にGAEアカウントを招待すること)
    'sheet_id': "<<Sheet ID>>",
}

# 変更しないでください。
STARTUP_LOAD_SHEET=True
