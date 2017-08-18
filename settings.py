# coding: utf-8

import os

SERVER_NAME = os.getenv('SERVER_NAME', '')
if SERVER_NAME == '<<project-name-prod>>.appspot.com':
    # リリース環境のサーバ設定
    BOTS = {
        'bot': {
            'line_access_token': '<<LINE_ACCESS_TOKEN>>',
            'line_channel_secret': '<<LINE_CHANEL_SECRET>>',
            # シナリオの Google Sheet ID (閲覧者に後述のサービスアカウントを招待すること)
            'sheet_id': "<<sheet_id>>",
        },
    }
    SHEETS = {
        # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
        'key_file_json': 'path_to_keyfile_sheets_prod.json',
    }
    # Twilio の各種設定
    PHONE = {
        'twilio_sid': '<<TWILIO_SID>>',
        'twilio_auth_token': '<<TWILIO_AUTH_TOKEN>>',
        'dial_from': '<<TEL_FOR_DIAL>>',
        'sms_from': '<<TEL_FOR_SMS_SEND>>',
    }
elif SERVER_NAME == '<<project-name-dev>>.appspot.com':
    # 開発環境のサーバ設定
    BOTS = {
        'bot': {
            'line_access_token': '<<LINE_ACCESS_TOKEN>>',
            'line_channel_secret': '<<LINE_CHANEL_SECRET>>',
            # シナリオの Google Sheet ID (閲覧者に後述のサービスアカウントを招待すること)
            'sheet_id': "<<sheet_id>>",
        },
    }
    SHEETS = {
        # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
        'key_file_json': 'path_to_keyfile_sheets_dev.json',
    }
    # Twilio の各種設定
    PHONE = {
        'twilio_sid': '<<TWILIO_SID>>',
        'twilio_auth_token': '<<TWILIO_AUTH_TOKEN>>',
        'dial_from': '<<TEL_FOR_DIAL>>',
        'sms_from': '<<TEL_FOR_SMS_SEND>>',
    }
else:
    # ローカルテストはこの設定を使う
    BOTS = {
        'bot': {
            'line_access_token': '<<LINE_ACCESS_TOKEN>>',
            'line_channel_secret': '<<LINE_CHANEL_SECRET>>',
            # シナリオの Google Sheet ID (閲覧者に後述のサービスアカウントを招待すること)
            'sheet_id': "<<sheet_id>>",
        },
    }
    SHEETS = {
        # Google Sheets API を呼び出すサービスアカウントのクレデンシャルファイル（JSON形式）
        'key_file_json': 'path_to_keyfile_sheets_test.json',
    }
    # Twilio の各種設定
    PHONE = {
        'twilio_sid': '<<TWILIO_SID>>',
        'twilio_auth_token': '<<TWILIO_AUTH_TOKEN>>',
        'dial_from': '<<TEL_FOR_DIAL>>',
        'sms_from': '<<TEL_FOR_SMS_SEND>>',
    }

# 変更しないでください。
STARTUP_LOAD_SHEET=True
