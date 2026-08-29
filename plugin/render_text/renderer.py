from io import BytesIO
from PIL import Image, ImageFont, ImageDraw, ImageColor
import re

SPECIAL_CHAR_TABLE = [
    #    ['ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ', +0.1, -0.12, 0],
    #    ['、。', +0.6, -0.55, 0],
    #    ['ー—―〜「」（）【】', 0, 0, 270],
]

NON_PRINTABLE_CHARS = "\r"

BURASAGARI_CHARS = "ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ、。」）】"


def _convert_color(color):
    if isinstance(color, str):
        color = ImageColor.getrgb(color)
    if isinstance(color, list):
        color = tuple(color)
    return color


def draw_text_horizontal(image, text, margin_left, margin_right, margin_top, margin_bottom, font, font_size, color,
                         line_height, base_line_offset, disable_word_wrap, burasagari_chars, centering_x, centering_y):
    #print(f"DRAW TEXT({margin_left}, {margin_top})")
    color = _convert_color(color)
    text = text.rstrip() # 末尾の空白文字を除去
    draw = ImageDraw.Draw(image)

    sx, sy = (margin_left, margin_top)
    size_x, size_y = image.size
    ny = int((size_y - margin_top - margin_bottom + font_size * (line_height - 1.0)) / (font_size * line_height))
    wx = size_x - margin_left - margin_right

    process_modes = [False, True] if centering_y else [True]
    offset_x = 0
    offset_y = 0
    for actual_mode in process_modes:
        iy = 0
        line_str = ''
        f_burasagari = False

        for i in range(len(text)):
            c = text[i]

            if c == "\n":
                # 現在処理中の行を出力
                y = sy + iy * font_size * line_height + font_size * base_line_offset
                if centering_x:
                    bbox = draw.textbbox((0, 0), line_str, font=font)
                    line_width = bbox[2] - bbox[0]
                    offset_x = (wx - line_width) // 2
                if actual_mode:
                    draw.text((sx+offset_x, y+offset_y), line_str, fill=color, font=font)

                # 改行
                iy += 1
                f_burasagari = False
                line_str = ''

            elif c in NON_PRINTABLE_CHARS:
                continue

            else:
                bbox = draw.textbbox((0, 0), line_str + c, font=font)
                line_width = bbox[2] - bbox[0]
                if line_width > wx:
                    # 現在の文字で行が溢れたので、溜めていた行を描画する
                    if c in burasagari_chars and not f_burasagari:
                        # 1文字だけ禁則ぶら下がりを許す
                        f_burasagari = True
                    else:
                        next_line = ''
                        if not disable_word_wrap:
                            # word wrap 処理
                            m = re.match(r'^(.*\S[-\s]+)([a-zA-Z0-9]+)[a-zA-Z0-9,.]$', line_str + c)
                            if m:
                                line_str = m.group(1)
                                next_line = m.group(2)

                        # 現在処理中の行を出力
                        y = sy + iy * font_size * line_height + font_size * base_line_offset
                        if centering_x:
                            bbox = draw.textbbox((0, 0), line_str, font=font)
                            line_width = bbox[2] - bbox[0]
                            offset_x = (wx - line_width) // 2
                        if actual_mode:
                            draw.text((sx+offset_x, y+offset_y), line_str, fill=color, font=font)

                        # 改行
                        iy += 1
                        f_burasagari = False
                        line_str = next_line

                line_str += c

                if not disable_word_wrap and line_str == ' ':
                    # word wrap モード時は行頭の半角スペースを削除する
                    line_str = ''

            if iy >= ny:
                # 縦の表示領域から溢れた
                if actual_mode:
                    return True, line_str + text[i+1:]

        if line_str != '':
            # 最後の行を出力
            y = sy + iy * font_size * line_height + font_size * base_line_offset
            if centering_x:
                bbox = draw.textbbox((0, 0), line_str, font=font)
                line_width = bbox[2] - bbox[0]
                offset_x = (wx - line_width) // 2
            if actual_mode:
                draw.text((sx+offset_x, y+offset_y), line_str, fill=color, font=font)

        if centering_y and not actual_mode:
            # センタリング用のオフセット値を計算
            offset_y = (size_y - margin_top - margin_bottom - iy * font_size * line_height - font_size) // 2

    return False, None


def draw_text_vertical(image, text, margin_left, margin_right, margin_top, margin_bottom, font, font_size, color,
                       line_height, base_line_offset, burasagari_chars, special_char_table, centering_x, centering_y):
    color = _convert_color(color)
    text = text.rstrip() # 末尾の空白文字を除去
    draw = ImageDraw.Draw(image)

    size_x, size_y = image.size
    wx = size_x - margin_left - margin_right
    wy = size_y - margin_top - margin_bottom

    sx = size_x - margin_right
    # nx: 最大列数（横方向の文字配置数）、ny: 1列あたりの最大文字数（縦方向）
    nx = (wx + font_size * (line_height - 1.0)) // (font_size * line_height)
    ny = wy // font_size

    offset_x = 0
    col_offset_y = []

    ix = 0
    iy = 0
    f_burasagari = False
    i = 0
    rest = None

    column_heights = []

    while i < len(text):
        c = text[i]
        if c == "\n":
            column_heights.append(iy)
            ix += 1
            iy = 0
            f_burasagari = False
            i += 1
            if ix >= nx:
                rest = text[i:]
                break
            continue
        elif c in NON_PRINTABLE_CHARS:
            i += 1
            continue

        if iy >= ny:
            # 1列に入りきらない場合
            if c in burasagari_chars and not f_burasagari:
                # ぶら下がりを1文字だけ許容
                f_burasagari = True
            else:
                column_heights.append(iy)
                ix += 1
                iy = 0
                f_burasagari = False
                if ix >= nx:
                    rest = text[i:]
                    break
        iy += 1
        i += 1
    if i >= len(text) and ix < nx:
        column_heights.append(iy)

    if (centering_x or centering_y):
        # シミュレーションパス
        n_cols = len(column_heights)
        if n_cols > 0:
            block_width = font_size + (n_cols - 1) * (font_size * line_height)
        else:
            block_width = 0
        offset_x = (wx - block_width) // 2 if centering_x else 0
        col_offset_y = []
        for count in column_heights:
            off_y = (wy - (count * font_size)) // 2 if centering_y else 0
            col_offset_y.append(off_y)

    # 描画パス
    new_sx = size_x - margin_right - offset_x
    ix = 0
    iy = 0
    f_burasagari = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\n":
            ix += 1
            iy = 0
            f_burasagari = False
            i += 1
            if ix >= nx:
                return True, text[i:]
            continue
        elif c in NON_PRINTABLE_CHARS:
            i += 1
            continue
        if iy >= ny:
            if c in burasagari_chars and not f_burasagari:
                f_burasagari = True
            else:
                ix += 1
                iy = 0
                f_burasagari = False
                if ix >= nx:
                    return True, text[i:]
        x = new_sx - ix * font_size * line_height - font_size
        current_offset_y = col_offset_y[ix] if ix < len(col_offset_y) else 0
        y = margin_top + current_offset_y + iy * font_size
        base_x, base_y = x, y

        bbox = font.getbbox(c)
        char_width = bbox[2] - bbox[0]
        x += (font_size - char_width) // 2
        y += font_size * base_line_offset

        dx = 0
        dy = 0
        rotation = False
        for entry in special_char_table:
            if c in entry[0]:
                _, dx, dy, rotation = entry
                break
        x += font_size * dx
        y += font_size * dy

        draw.text((x, y), c, fill=color, font=font)

        if rotation:
            tmp = image.crop((base_x, base_y, base_x + font_size, base_y + font_size))
            tmp2 = tmp.rotate(270)
            image.paste(tmp2, (base_x, base_y))
            del tmp2, tmp

        iy += 1
        i += 1

    if rest is not None:
        return True, rest

    return False, None


def get_background_image(size, color, background):
    color = _convert_color(color)
    if re.search(r'\.(png|jpeg|jpg)$', background, re.IGNORECASE):
        im = Image.open(background)
        return im.copy().resize(size, resample=Image.LANCZOS)
    else:
        if color in ['white', 'black'] and background in ['white', 'black']:
            return Image.new('L', size, background)
        else:
            return Image.new('RGBA', size, background)


def create_image_with_text(text, size_x, size_y, margin_left, margin_right, margin_top, margin_bottom, centering_x=False, centering_y=False, auto_scaling=None, is_vertical=False, font_path=None, font_size=100, color='black', background='white', line_height=1.5, base_line_offset=0, disable_word_wrap=False, burasagari_chars=BURASAGARI_CHARS, special_char_table=SPECIAL_CHAR_TABLE):
    color = _convert_color(color)
    scales = [1.0] if auto_scaling is None else auto_scaling
    for scale in scales:
        image = get_background_image((size_x, size_y), color, background)
        if is_vertical:
            if font_path is None:
                font_path = 'plugin/render_text/font/ipaexg_tate.ttf'
            font = ImageFont.truetype(font_path, int(font_size * scale))
            flag, rest = draw_text_vertical(image, text, margin_left, margin_right, margin_top, margin_bottom, font, font_size, color, line_height, base_line_offset, burasagari_chars, special_char_table, centering_x, centering_y)
        else:
            if font_path is None:
                font_path = 'plugin/render_text/font/ipaexg.ttf'
            font = ImageFont.truetype(font_path, int(font_size * scale))
            flag, rest = draw_text_horizontal(image, text, margin_left, margin_right, margin_top, margin_bottom, font, font_size, color, line_height, base_line_offset, disable_word_wrap, burasagari_chars, centering_x, centering_y)
        if auto_scaling is None or not flag:
            # 納まりきったか、スケーリングが指定されていない場合は終了
            break

    return image, rest


def render_text_to_png(text, size_x, size_y, margin_left, margin_right, margin_top, margin_bottom, **text_rendering_options):
    image, rest = create_image_with_text(text, size_x, size_y, margin_left, margin_right, margin_top, margin_bottom, **text_rendering_options)

    output = BytesIO()
    image.save(output, 'PNG', compress_level=9, optimize=True)
    output_buffer = output.getvalue()
    output.close()

    del image
    return output_buffer, rest

if __name__ == "__main__":
    text = "「縦書きテキスト」\n複数行にわたる"
    size_x, size_y = 800, 1000
    margin = 20

    # 中央揃え
    image, _ = render_text_to_png(text, size_x, size_y, margin, margin, margin, margin,
                                  is_vertical=True, centering_x=True, centering_y=True)
    with open("test_centered.png", "wb") as f:
        f.write(image)

    # 左揃え
    image, _ = render_text_to_png(text, size_x, size_y, margin, margin, margin, margin,
                                  is_vertical=True, centering_x=False, centering_y=True)
    with open("test_left_aligned.png", "wb") as f:
        f.write(image)

    # 上揃え
    image, _ = render_text_to_png(text, size_x, size_y, margin, margin, margin, margin,
                                  is_vertical=True, centering_x=True, centering_y=False)
    with open("test_top_aligned.png", "wb") as f:
        f.write(image)

    # 標準
    image, _ = render_text_to_png(text, size_x, size_y, margin, margin, margin, margin, is_vertical=True)
    with open("test_default.png", "wb") as f:
        f.write(image)
