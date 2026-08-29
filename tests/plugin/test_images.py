# coding: utf-8

from io import BytesIO
from pathlib import Path
import unittest

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

        self.assertEqual(convert_image.get_image_format(b'broken'), (None, None))
        self.assertEqual(
            convert_image.resize_image(b'broken', 8),
            (None, None),
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


if __name__ == '__main__':
    unittest.main()
