import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class LogParserTest(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec('yaml'),
        'PyYAMLが導入済みの環境で確認する',
    )
    def test_python3_csv_output_and_utf8_bom(self):
        input_text = '''
protoPayload:
  line:
    - logMessage: '{"type":"XSBLog","date":"2026/08/11","uid":"user-1","cat":"Story","log":["一行目","二行目"],"scene":"scene-1","action":"次へ"}'
'''

        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / 'tools/logparser.py'), 'Story'],
            input=input_text.encode('utf-8'),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )

        self.assertTrue(result.stdout.startswith(b'\xef\xbb\xbf'))
        output = result.stdout.decode('utf-8-sig').splitlines()
        self.assertEqual(
            output[0], '"date","user","category","log","scene","action"'
        )
        self.assertEqual(
            output[1],
            '"2026/08/11","user-1","Story","一行目,二行目",'
            '"scene-1","次へ"',
        )


if __name__ == '__main__':
    unittest.main()
