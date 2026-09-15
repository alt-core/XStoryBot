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
        evaluate_formula = self.params.get('evaluate_formula', False)
        from plugin.tsv_values import MAX_COLUMNS, local_values, read_base_values, sync_baselines, to_text
        baseline_paths = sync_baselines(manifest_path, paths)
        values = {}
        literal_cells = set()

        def read_sheet(title):
            if title in values:
                return values[title]
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
            if title in baseline_paths:
                try:
                    typed_rows = local_values(rows, read_base_values(baseline_paths[title]))
                except ValueError as error:
                    raise ValueError(f'{title} ({path}): {error}') from error
                # 値をpushと揃え、CSV行数・範囲内の空セル・元の物理位置は変えない。
                for row_index, row in enumerate(rows):
                    # 同期範囲外は空列だけを許可し、余分なpaddingを保持しない。
                    del row[MAX_COLUMNS:]
                    typed_row = typed_rows[row_index] if row_index < len(typed_rows) else []
                    for column in range(len(row)):
                        cell = typed_row[column] if column < len(typed_row) else {}
                        row[column] = to_text(cell)
                        if (evaluate_formula and 'stringValue' in cell
                                and row[column].lstrip().startswith('=')):
                            literal_cells.add((title, row_index, column))
            values[title] = rows
            return rows

        if evaluate_formula:
            from plugin.tsv_formula import FormulaEvaluator, is_formula
            evaluator = FormulaEvaluator(read_sheet, paths, literal_cells)
            for title, _name, _is_constant in selected:
                for row_index, row in enumerate(read_sheet(title)):
                    for column, value in enumerate(row):
                        if is_formula(value):
                            row[column] = evaluator.evaluate(title, row_index, column)
        else:
            for title, _name, _is_constant in selected:
                read_sheet(title)
        return assemble_tables(selected, values)


class TsvPlugin_LoaderFactory:
    def __init__(self, params):
        self.params = params

    def create_loader(self, params):
        return TsvPlugin_Loader(utility.merge_params(self.params, params))


def load_plugin(params):
    hub.register_scenario_loader_factory(
        type_name='tsv', factory=TsvPlugin_LoaderFactory(params))
