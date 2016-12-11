# coding: utf-8

BOTS = {
    'bot': {
        'line_access_token': '<<LINE_ACCESS_TOKEN>>',
        'line_channel_secret': '<<LINE_CHANEL_SECRET>>',
        # シナリオの Google Sheet ID (閲覧者に後述のサービスアカウントを招待すること)
        'sheet_id': "<<sheet_id>>",
    }
}

SHEETS = {
    # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
    'key_file_json': 'path_to_key_file.json',
}

# 変更しないでください。
STARTUP_LOAD_SHEET=True
