# -*- coding: utf-8 -*-
import re
from oauth2client.service_account import ServiceAccountCredentials
from apiclient import discovery
import httplib2

import hub
import utility

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


_google_services = {}


def _get_google_service(key_file_name):
    if key_file_name not in _google_services:
        # 認証情報の作成
        scope = ["https://spreadsheets.google.com/feeds"]
        credentials = ServiceAccountCredentials.from_json_keyfile_name(key_file_name, scope)
        http = credentials.authorize(httplib2.Http(memcache))
        discovery_url = 'https://sheets.googleapis.com/$discovery/rest?version=v4'
        _google_services[key_file_name] = discovery.build('sheets', 'v4', http=http, discoveryServiceUrl=discovery_url)

    return _google_services[key_file_name]


class GoogleSheetPlugin_Loader(object):
    def __init__(self, params):
        self.params = params

    def get_service(self):
        return _get_google_service(self.params['key_file_json'])

    def _get_table_from_google_sheets(self, spreadsheet_id):
        service = self.get_service()
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

    def load_scenario(self):
        return self._get_table_from_google_sheets(self.params['sheet_id'])


class GoogleSheetPlugin_LoaderFactory(object):
    def __init__(self, params):
        self.params = params

    def create_loader(self, params):
        return GoogleSheetPlugin_Loader(utility.merge_params(self.params, params))


def load_plugin(params):
    factory = GoogleSheetPlugin_LoaderFactory(params)
    hub.register_scenario_loader_factory(
        type_name="google_sheets",
        factory=factory
    )


# if __name__ == "__main__":
#     sheet_id = settings.BOTS.values()[0]['sheet_id']
#     sheets = get_table_from_google_sheets(sheet_id)
#     for title, table in sheets:
#         print title
#         print utility.table_to_str(table)
