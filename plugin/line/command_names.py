"""LINE表示commandの名前だけを共有する軽量module。"""


BUTTON_CMDS = ('@button', '@ボタン')
CONFIRM_CMDS = ('@confirm', '@確認')
PANEL_CMDS = ('@carousel', '@カルーセル', '@panel', '@パネル')
IMAGEMAP_CMDS = ('@imagemap', '@イメージマップ')
FLEX_CMDS = ('@flex', '@フレックス')
REPLY_CMDS = ('@reply', '@リプライ')
RICHMENU_CMDS = ('@richmenu', '@リッチメニュー')
ALL_TEMPLATE_CMDS = (
    BUTTON_CMDS + CONFIRM_CMDS + PANEL_CMDS + IMAGEMAP_CMDS
    + REPLY_CMDS + RICHMENU_CMDS
)
