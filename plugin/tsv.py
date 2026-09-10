"""順序を明示したローカルTSVからシナリオ表を読み込む。"""

import csv
import json
from pathlib import Path

import hub
import utility
from plugin.scenario_table import SheetSelector, SourceRow, assemble_tables


class TsvPlugin_Loader:
    def __init__(self, params):
        self.params = params
        self.sheet_selector = SheetSelector(params)

    def load_scenario(self):
        import settings

        manifest_path = Path(self.params['manifest']).resolve()
        with manifest_path.open(encoding='utf-8') as source:
            manifest = json.load(source)
        if not isinstance(manifest, dict) or set(manifest) != {'sheets'}:
            raise ValueError('TSV manifestにはsheetsだけを指定してください')
        if not isinstance(manifest['sheets'], list):
            raise ValueError('TSV manifestのsheetsは配列で指定してください')
        paths = {}
        for entry in manifest['sheets']:
            if (
                    not isinstance(entry, dict)
                    or set(entry) != {'name', 'path'}
                    or not isinstance(entry['name'], str) or not entry['name']
                    or not isinstance(entry['path'], str) or not entry['path']):
                raise ValueError('TSVの各シートには空でないnameとpathを指定してください')
            title = entry['name']
            if title in paths:
                raise ValueError(f'TSV manifestのシート名が重複しています: {title}')
            paths[title] = (manifest_path.parent / entry['path']).resolve()

        selected = self.sheet_selector.select(paths, settings.DEPLOY_ENV)
        values = {}
        for title, _name, _is_constant in selected:
            path = paths[title]
            rows = []
            with path.open(encoding='utf-8', newline='') as source:
                reader = csv.reader(source, dialect='excel-tab', strict=True)
                while True:
                    line_no = reader.line_num
                    try:
                        row = next(reader)
                    except StopIteration:
                        break
                    except csv.Error as error:
                        raise ValueError(
                            f'TSVの形式が不正です: {title} ({path})!{line_no + 1}行目: {error}'
                        ) from error
                    rows.append(SourceRow(row, title, line_no, str(path)))
            values[title] = rows
        return assemble_tables(selected, values)


class TsvPlugin_LoaderFactory:
    def __init__(self, params):
        self.params = params

    def create_loader(self, params):
        return TsvPlugin_Loader(utility.merge_params(self.params, params))


def load_plugin(params):
    hub.register_scenario_loader_factory(
        type_name='tsv', factory=TsvPlugin_LoaderFactory(params))
