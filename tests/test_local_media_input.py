"""画像・動画の実Builderがlocal入力を使い、未登録URLへ接続しないことを確認する。"""

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests

import cloud_backend
import convert_image
import expression
from tests.test_scenario_formatting import _load_module


class LocalMediaInputTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = patch.multiple(expression, **{
            name: getattr(expression, name) for name in (
                'EXPRESSION_VERSION', 'TRUE_VALUE', 'FALSE_VALUE', 'NONE_VALUE')
        })
        patcher.start()
        self.addCleanup(patcher.stop)

        self.object_store = Mock()
        self.object_store.store_public.side_effect = (
            lambda key, _data, content_type: f'http://127.0.0.1:8765/local-media/test/{key}')
        with patch.object(cloud_backend, 'create_object_store', return_value=self.object_store):
            self.scenario = _load_module('tests._local_media_scenario', 'scenario.py')

        self.stats = types.ModuleType('models')
        self.stats.ImageFileStatDB = Mock()
        self.stats.ImageFileStatDB.get_cached_image_file_stat.return_value = None
        self.stats.MediaFileStatDB = Mock()
        self.stats.MediaFileStatDB.get_cached_media_file_stat.return_value = None
        self.image_path = self.root / 'image.png'
        with Image.new('RGB', (32, 16), 'white') as image:
            image.save(self.image_path)
        self.video_path = self.root / 'video.mp4'
        self.video_path.write_bytes(b'local-video-data')

    def _builder(self, options):
        builder = self.scenario.ScenarioBuilder(options, version=3)
        builder.node = self.scenario.SyntaxTree('story', 4, ['媒体'])
        return builder

    def test_画像と動画はlocalのbytesから既存変換と保存を行う(self):
        image_url = 'https://example.invalid/表紙 1.png'
        video_url = 'https://example.invalid/video.mp4'
        builder = self._builder({'local_media_files': {
            image_url: str(self.image_path), video_url: str(self.video_path),
        }})
        with (
                patch.dict(sys.modules, {'models': self.stats}),
                patch.object(requests, 'get', side_effect=AssertionError('外部取得禁止')) as get):
            image_result, image_size = builder.build_image_for_image_command(
                requests.utils.requote_uri(image_url))
            video_result = builder.build_video(video_url)

        get.assert_not_called()
        self.assertTrue(image_result.endswith('_1024.png'))
        self.assertEqual((32, 16), image_size)
        self.assertTrue(video_result.endswith('.mp4'))
        writes = self.object_store.store_public.call_args_list
        self.assertEqual(len(writes), 3)
        for write in writes[:2]:
            with Image.open(BytesIO(write.args[1])) as image:
                self.assertEqual((32, 16), image.size)
            self.assertEqual('image/png', write.kwargs['content_type'])
        self.assertEqual(b'local-video-data', writes[2].args[1])
        self.assertEqual('video/mp4', writes[2].kwargs['content_type'])
        self.stats.ImageFileStatDB.put_cached_image_file_stat.assert_called_once()
        self.stats.MediaFileStatDB.put_cached_media_file_stat.assert_called_once()

    def test_空mapでは画像も動画も作成位置付きで拒否しHTTPへfallbackしない(self):
        for kind in ('image', 'video'):
            with self.subTest(kind=kind):
                builder = self._builder({'local_media_files': {}})
                with (
                        patch.dict(sys.modules, {'models': self.stats}),
                        patch.object(requests, 'get') as get,
                        self.assertRaises(self.scenario.ScenarioSyntaxError) as caught):
                    if kind == 'image':
                        builder.build_image_for_image_command('https://example.invalid/missing')
                    else:
                        builder.build_video('https://example.invalid/missing')
                get.assert_not_called()
                self.assertIn('ローカル媒体が登録されていません', str(caught.exception))
                self.assertIn('＠story!5行目', str(caught.exception))

    def test_map未指定とNoneでは従来どおりHTTPを読む(self):
        for options in ({}, {'local_media_files': None}):
            with self.subTest(options=options):
                builder = self._builder(options)
                response = Mock(content=b'original-http-body')
                with patch.object(requests, 'get', return_value=response) as get:
                    self.assertEqual(
                        b'original-http-body', builder._read_media_bytes('https://example.invalid/media'))
                get.assert_called_once_with('https://example.invalid/media')
                response.raise_for_status.assert_called_once_with()

    def test_HTTPへのfallbackはtrueの明示時だけ許す(self):
        response = Mock(content=b'allowed-http-body')
        for allow in (False, 'false', 'true', True):
            with self.subTest(allow=allow):
                builder = self._builder({
                    'local_media_files': {}, 'allow_external_media': allow,
                })
                with patch.object(requests, 'get', return_value=response) as get:
                    if allow is True:
                        self.assertEqual(b'allowed-http-body', builder._read_media_bytes('https://example.invalid/media'))
                        get.assert_called_once()
                    else:
                        with self.assertRaises(ValueError):
                            builder._read_media_bytes('https://example.invalid/media')
                        get.assert_not_called()

    def test_登録ファイルの欠落は外部許可があってもHTTPへ逃がさない(self):
        url = 'https://example.invalid/media'
        builder = self._builder({
            'local_media_files': {url: str(self.root / 'missing')},
            'allow_external_media': True,
        })
        with patch.object(requests, 'get') as get:
            with self.assertRaises(FileNotFoundError):
                builder._read_media_bytes(url)
        get.assert_not_called()

    def test_requote後に重なるURLは起動時に拒否する(self):
        url = 'https://example.invalid/画像.png'
        with self.assertRaisesRegex(ValueError, 'URLが重複'):
            self._builder({'local_media_files': {
                url: str(self.image_path),
                requests.utils.requote_uri(url): str(self.image_path),
            }})


if __name__ == '__main__':
    unittest.main()
