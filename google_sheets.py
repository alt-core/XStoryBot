# -*- coding: utf-8 -*-
import re
from oauth2client.service_account import ServiceAccountCredentials
from apiclient import discovery
import httplib2

if __name__ == "__main__":
    import os, sys, json, subprocess
    gcloud_info = json.loads(subprocess.check_output(['gcloud', 'info', '--format=json']))
    sdk_path = os.path.join(gcloud_info["installation"]["sdk_root"], 'platform', 'google_appengine')
    sys.path.append(sdk_path)
    sys.path.append(os.path.join(sdk_path, 'lib', 'yaml', 'lib'))
    sys.path.insert(0, './lib')
    from google.appengine.api import memcache
    from google.appengine.ext import testbed
    tb = testbed.Testbed()
    tb.activate()
    tb.init_memcache_stub()
else:
    from google.appengine.api import memcache

import settings

# 認証情報の作成
scope = ["https://spreadsheets.google.com/feeds"]
credentials = ServiceAccountCredentials.from_json_keyfile_name(settings.SHEETS['key_file_json'], scope)
http = credentials.authorize(httplib2.Http(memcache))
discoveryUrl = 'https://sheets.googleapis.com/$discovery/rest?version=v4'
service = discovery.build('sheets', 'v4', http=http, discoveryServiceUrl=discoveryUrl)

def table_to_str(values):
    if not values: return 'No entry\n'
    output = u''
    for row in values:
        for cell in row:
            output += u"'{}',".format(cell)
        output += u"\n"
    return output

def get_table_from_google_sheets(spreadsheet_id):
    result = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets(properties(sheet_id,title))"
    ).execute()
    sheet_titles = [sheet_prop[u'properties'][u'title'] for sheet_prop in result.get('sheets', [])]

    sheets = []
    for sheet_title in sheet_titles:
        if re.match(u'^[_＿]', sheet_title):
            # _で始まるシート名はスキップ
            continue
        result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=sheet_title+"!A:Z", valueRenderOption="FORMULA"
        ).execute()
        sheets.append((sheet_title, result.get('values', [])))

    return sheets

if __name__ == "__main__":
    sheet_id = settings.BOTS.values()[0]['sheet_id']
    sheets = get_table_from_google_sheets(sheet_id)
    for title, table in sheets:
        print title
        print table_to_str(table)
