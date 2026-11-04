# coding: utf-8

from io import BytesIO
import builtins
import importlib.util
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

import convert_image
from plugin.render_text import renderer


ROOT = Path(__file__).resolve().parents[2]


def image_bytes(mode, size, image_format, color, **save_options):
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, image_format, **save_options)
    return output.getvalue()


def open_image(content):
    image = Image.open(BytesIO(content))
    image.load()
    return image


class ConvertImageTest(unittest.TestCase):
    def test_png_transparency_resize(self):
        content = image_bytes('RGBA', (8, 4), 'PNG', (10, 20, 30, 64))

        self.assertEqual(convert_image.get_image_format(content), 'PNG')
        resized, image_format, size = convert_image.resize_image(content, 4)

        self.assertEqual(image_format, 'PNG')
        self.assertEqual(size, (4, 2))
        image = open_image(resized)
        self.assertEqual(image.format, 'PNG')
        self.assertEqual(image.mode, 'RGBA')
        self.assertEqual(image.getpixel((0, 0))[3], 64)

    def test_jpeg_and_cmyk_resize(self):
        rgb = image_bytes('RGB', (10, 5), 'JPEG', (120, 80, 40))
        resized, image_format, size = convert_image.resize_image(rgb, 5)
        self.assertEqual((image_format, size), ('JPEG', (4, 2)))
        self.assertEqual(open_image(resized).format, 'JPEG')

        cmyk = image_bytes('CMYK', (6, 3), 'JPEG', (0, 64, 128, 0))
        resized, image_format, size = convert_image.resize_image(cmyk, 3)
        image = open_image(resized)
        self.assertEqual((image_format, size), ('JPEG', (2, 1)))
        self.assertEqual(image.mode, 'CMYK')

    def test_gif_palette_is_converted_to_supported_png(self):
        image = Image.new('P', (4, 4))
        image.putpalette([0, 0, 0, 255, 0, 0] + [0, 0, 0] * 254)
        image.putdata([0, 1, 0, 1] * 4)
        output = BytesIO()
        image.save(output, 'GIF')
        content = output.getvalue()

        self.assertEqual(convert_image.get_image_format(content), 'PNG')
        resized, image_format, size = convert_image.resize_image(content, 2)
        converted = open_image(resized)
        self.assertEqual((image_format, size), ('PNG', (2, 2)))
        self.assertEqual(converted.format, 'PNG')
        self.assertEqual(converted.mode, 'P')

    def test_never_stretch_and_corrupt_input(self):
        content = image_bytes('RGB', (2, 1), 'PNG', (1, 2, 3))
        resized, image_format, size = convert_image.resize_image(
            content, 8, never_stretch=True)
        self.assertEqual((image_format, size), ('PNG', (2, 1)))
        self.assertEqual(convert_image.calc_size(resized), (2, 1))

        self.assertIsNone(convert_image.get_image_format(b'broken'))
        # 呼出し側 (scenario.py) が3要素で受け取るので、失敗時も3要素で返す
        self.assertEqual(
            convert_image.resize_image(b'broken', 8),
            (None, None, None),
        )
        self.assertIsNone(convert_image.calc_size(b'broken'))


class RenderTextTest(unittest.TestCase):
    def test_horizontal_and_vertical_rendering(self):
        horizontal, rest = renderer.render_text_to_png(
            '横書きテキスト',
            480, 180, 20, 20, 20, 20,
            font_path=str(ROOT / 'plugin' / 'render_text' / 'font' / 'ipaexg.ttf'),
            font_size=40,
        )
        image = open_image(horizontal)
        self.assertEqual((image.format, image.size), ('PNG', (480, 180)))
        self.assertIsNone(rest)

        vertical, rest = renderer.render_text_to_png(
            '縦書き\n二列目',
            320, 480, 20, 20, 20, 20,
            is_vertical=True,
            centering_x=True,
            centering_y=True,
            font_path=str(ROOT / 'plugin' / 'render_text' / 'font' / 'ipaexg_tate.ttf'),
            font_size=40,
        )
        image = open_image(vertical)
        self.assertEqual((image.format, image.size), ('PNG', (320, 480)))
        self.assertIsNone(rest)

    def test_transparent_background_is_preserved(self):
        content, rest = renderer.render_text_to_png(
            '透明',
            240, 120, 10, 10, 10, 10,
            font_path=str(ROOT / 'plugin' / 'render_text' / 'font' / 'ipaexg.ttf'),
            font_size=32,
            color=(255, 255, 255, 255),
            background='#00000000',
        )
        image = open_image(content)
        self.assertEqual(image.mode, 'RGBA')
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.assertIsNone(rest)


class ImageTextAssetCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.saved = {}
        state = SimpleNamespace(
            get_image_text_stat=self.saved.get,
            put_image_text_stat=self.saved.__setitem__,
        )
        spec = importlib.util.spec_from_file_location(
            '_image_text_asset_cache', ROOT / 'plugin/line/image_text.py')
        self.image_text = importlib.util.module_from_spec(spec)
        with patch('cloud_backend.create_state_store', return_value=state):
            spec.loader.exec_module(self.image_text)

    def _font(self, name, vertical=False):
        path = self.root / name
        source = 'ipaexg_tate.ttf' if vertical else 'ipaexg.ttf'
        shutil.copyfile(ROOT / 'plugin/render_text/font' / source, path)
        return path

    def _plugin(self, frames):
        return self.image_text.LineImageTextPlugin_Builder({'frames': {
            name: {'more_mode': 'quick_between', 'size_x': 400, 'size_y': 200,
                   'font_size': 20, **options}
            for name, options in frames.items()
        }})

    def _build(self, plugin, frames, force=False, skip=False):
        builder = SimpleNamespace(
            option_force=force, option_skip_image=skip, add_command=Mock(),
            build_image_for_imagemap_command_with_rawdata=lambda data, file_digest, **options: (
                'https://cache.invalid/' + file_digest, (400, 200)),
        )
        for frame in frames:
            plugin.build_from_command(builder, None, '@imagetext', ['本文', frame])
        return [call.args[2][0] for call in builder.add_command.call_args_list]

    def test_同名素材の変更で該当する枠だけ再描画しビルド内では素材を一度だけ読む(self):
        first_font, second_font = self._font('first.ttf'), self._font('second.ttf')
        first_bg, second_bg = self.root / 'first.PNG', self.root / 'second.png'
        first_bg.write_bytes(image_bytes('RGB', (20, 20), 'PNG', 'white'))
        second_bg.write_bytes(image_bytes('RGB', (20, 20), 'PNG', 'gray'))
        plugin = self._plugin({
            'first': {'font_path': str(first_font), 'background': str(first_bg)},
            'second': {'font_path': str(second_font), 'background': str(second_bg)},
        })
        frames = ['first', 'first', 'second']
        with patch.object(renderer, 'render_text_to_png', wraps=renderer.render_text_to_png) as render:
            with patch.object(self.image_text, 'open', wraps=builtins.open, create=True) as read:
                cold = self._build(plugin, frames)
                self.assertEqual(4, read.call_count)
            self.assertEqual(2, render.call_count)
            render.reset_mock()
            self.assertEqual(cold, self._build(plugin, frames))
            render.assert_not_called()

            first_bg.write_bytes(image_bytes('RGB', (20, 20), 'PNG', 'red'))
            changed = self._build(plugin, frames, skip=True)
            self.assertEqual(1, render.call_count)
            self.assertNotEqual(cold[0], changed[0])
            self.assertEqual(changed[0], changed[1])
            self.assertEqual(cold[2], changed[2])

            render.reset_mock()
            second_font.write_bytes(second_font.read_bytes() + b'font-update')
            self._build(plugin, frames)
            self.assertEqual(1, render.call_count)
            render.reset_mock()
            self._build(plugin, frames, force=True)
            self.assertEqual(3, render.call_count)

    def test_縦横の既定フォントも変更を検出し描画結果が同じならURLを保つ(self):
        fonts = {False: self._font('default.ttf'), True: self._font('vertical.ttf', True)}
        plugin = self._plugin({'horizontal': {}, 'vertical': {'is_vertical': True}})
        resolve = lambda font_path=None, is_vertical=False: str(fonts[is_vertical]) if font_path is None else font_path
        with patch.object(renderer, 'get_font_path', side_effect=resolve), \
                patch.object(renderer, 'render_text_to_png', wraps=renderer.render_text_to_png) as render:
            cold = self._build(plugin, ['horizontal', 'vertical'])
            self.assertEqual(2, render.call_count)
            for vertical in (False, True):
                render.reset_mock()
                fonts[vertical].write_bytes(fonts[vertical].read_bytes() + b'font-update')
                self.assertEqual(cold, self._build(plugin, ['horizontal', 'vertical']))
                self.assertEqual(1, render.call_count)

    def test_フォント名の探索結果を素材として使い背景色はファイルと見なさない(self):
        path = str(self.root / 'resolved.ttf')
        with patch.object(renderer.ImageFont, 'truetype', return_value=SimpleNamespace(path=path)):
            self.assertEqual([path], renderer.get_text_rendering_asset_paths(
                font_path='font-name.ttf', background='#ffffff'))


if __name__ == '__main__':
    unittest.main()
