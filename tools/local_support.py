"""ローカル検証で使う入力検査と非公開ファイルの保存。"""

import json
from pathlib import Path

from cloud_backend.local.storage import atomic_write


class LocalInputError(ValueError):
    """実行する前に修正できる入力・設定の誤り。"""


def write_bytes(path, data):
    atomic_write(Path(path), data)


def write_json(path, data):
    write_bytes(path, json.dumps(
        data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))


def read_json(path):
    def reject_constant(value):
        raise LocalInputError(f'JSONでは有限数を指定してください: {value}')
    return json.loads(Path(path).read_text(encoding='utf-8'), parse_constant=reject_constant)


def require_keys(value, required, optional=(), label='入力'):
    if not isinstance(value, dict):
        raise LocalInputError(f'{label}はobjectで指定してください')
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise LocalInputError(
            f'{label}: 不足key={sorted(missing)}, 未対応key={sorted(unknown)}')


def validate_suite(value, selected=None):
    require_keys(value, ('schema_version', 'cases'), label='suite')
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise LocalInputError('suiteのschema_versionは1を指定してください')
    cases = value['cases']
    if not isinstance(cases, list) or not cases:
        raise LocalInputError('casesは空でない配列にしてください')
    names = set()
    for case in cases:
        require_keys(case, ('name', 'steps'), ('seed',), 'case')
        name = case['name']
        if not isinstance(name, str) or not name or name in names:
            raise LocalInputError('case名は空でない、重複のない文字列にしてください')
        names.add(name)
        if type(case.get('seed', 0)) is not int:
            raise LocalInputError(f'{name}: seedは整数にしてください')
        steps = case['steps']
        if not isinstance(steps, list) or not steps:
            raise LocalInputError(f'{name}: stepsは空でない配列にしてください')
        assertions = 0
        for index, step in enumerate(steps, 1):
            label = f'{name} step {index}'
            require_keys(step, ('input',), ('expect',), label)
            input_data = step['input']
            require_keys(input_data, ('type',), ('action', 'text', 'index'), label)
            kind = input_data['type']
            if kind == 'start':
                require_keys(input_data, ('type',), ('action',), label)
                if not isinstance(input_data.get('action', '##line.follow'), str):
                    raise LocalInputError(f'{label}: actionは文字列にしてください')
            elif kind == 'text':
                require_keys(input_data, ('type', 'text'), label=label)
                if not isinstance(input_data['text'], str):
                    raise LocalInputError(f'{label}: textは文字列にしてください')
            elif kind == 'choice':
                require_keys(input_data, ('type', 'index'), label=label)
                if type(input_data['index']) is not int or input_data['index'] < 0:
                    raise LocalInputError(f'{label}: indexは0以上の整数にしてください')
            else:
                raise LocalInputError(f'{label}: 未対応のinput typeです: {kind}')
            expected = step.get('expect', {})
            require_keys(expected, (), ('texts', 'choices', 'flags', 'absent_flags'), label)
            for key, expected_value in expected.items():
                if key == 'flags':
                    if not isinstance(expected_value, dict):
                        raise LocalInputError(f'{label}: flagsはobjectにしてください')
                    assertions += len(expected_value)
                elif key in ('texts', 'absent_flags'):
                    if not isinstance(expected_value, list) or not all(
                            isinstance(item, str) for item in expected_value):
                        raise LocalInputError(f'{label}: {key}は文字列の配列にしてください')
                    assertions += 1 if key == 'texts' else len(expected_value)
                else:
                    if not isinstance(expected_value, list):
                        raise LocalInputError(f'{label}: choicesは配列にしてください')
                    for choice in expected_value:
                        require_keys(choice, ('type', 'label'), label=label)
                        if choice['type'] not in ('message', 'postback', 'uri') or (
                                choice['label'] is not None
                                and not isinstance(choice['label'], str)):
                            raise LocalInputError(f'{label}: choiceのtype/labelが不正です')
                    assertions += 1
        if not assertions:
            raise LocalInputError(f'{name}: 少なくとも一つの期待値が必要です')
    if selected is not None:
        if selected not in names:
            raise LocalInputError(f'caseが見つかりません: {selected}')
        cases = [case for case in cases if case['name'] == selected]
    return cases
