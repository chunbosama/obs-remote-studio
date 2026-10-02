"""配色方案（H5）。

两套主题共用同一组键名：
- `style.qss` 是模板，里面的 @TOKEN@ 会被替换成当前主题的颜色；
- 手绘图标（utils/icons.py）、自绘控件（电平表/预览区）在绘制时读 `theme.color()`。

因此改配色只需要改这里，QSS 与代码里的颜色不会各写一份。
新增颜色时两套主题都要加，缺键会在启动时报错（比默默变成黑色好）。
"""

from __future__ import annotations

DARK: dict[str, str] = {
    # 底色
    "BG": "#1e1e1e",            # 窗口底
    "SURFACE": "#252525",       # 面板内容 / 列表
    "SURFACE_ALT": "#2b2b2b",   # 菜单栏、菜单、状态栏、输入框
    "SURFACE_HOVER": "#303030",
    "HEADER": "#2e2e2e",        # 面板标题栏
    "BORDER": "#3c3c3c",
    "BORDER_STRONG": "#4a4a4a",
    # 文字
    "TEXT": "#d6d6d6",
    "TEXT_DIM": "#8f8f8f",
    "TEXT_DISABLED": "#6a6a6a",
    "TEXT_ON_ACCENT": "#ffffff",
    "HEADER_TEXT": "#c8c8c8",
    "FADER_VALUE": "#b8b8b8",
    "FADER_SCALE": "#7a7a7a",
    # 交互
    "SELECT": "#35618f",
    "ACCENT": "#2563a8",
    "ACCENT_BORDER": "#2f74c0",
    "HOVER": "#3a3a3a",
    "BUTTON": "#3a3a3a",
    "BUTTON_HOVER": "#454545",
    "BUTTON_PRESSED": "#2f2f2f",
    "BUTTON_DISABLED": "#2c2c2c",
    "BUTTON_DISABLED_BORDER": "#3a3a3a",
    "CHECK_BORDER": "#5a5a5a",
    "ARROW": "#b0b0b0",
    # 滚动条
    "SCROLL_HANDLE": "#4a4a4a",
    "SCROLL_HANDLE_HOVER": "#5a5a5a",
    # 预览
    "PREVIEW_BG": "#000000",
    # 指示灯
    "RED": "#e04b4b",
    "GREEN": "#5cb85c",
    "YELLOW": "#d9a03a",
    # 混音器
    "GROOVE_1": "#1f6f3f",
    "GROOVE_2": "#6f6f1f",
    "GROOVE_3": "#7f2020",
    "SLIDER_HANDLE": "#d0d0d0",
    "SLIDER_HANDLE_BORDER": "#8a8a8a",
    "METER_BG": "#101010",
    "METER_PEAK": "#f2f2f2",
    "METER_LOW": "#2f9e44",
    "METER_MID": "#cbc02a",
    "METER_HIGH": "#e03131",
    "MUTED_BG": "#7f2020",
    "MUTED_BORDER": "#a03030",
}

LIGHT: dict[str, str] = {
    "BG": "#f0f2f4",
    "SURFACE": "#ffffff",
    "SURFACE_ALT": "#f6f7f9",
    "SURFACE_HOVER": "#eef1f4",
    "HEADER": "#e3e7ea",
    "BORDER": "#c9ced4",
    "BORDER_STRONG": "#b3bac2",
    "TEXT": "#23272b",
    "TEXT_DIM": "#5f6771",
    "TEXT_DISABLED": "#9aa2ab",
    "TEXT_ON_ACCENT": "#ffffff",
    "HEADER_TEXT": "#3c434a",
    "FADER_VALUE": "#4a5158",
    "FADER_SCALE": "#7b838b",
    "SELECT": "#2f6feb",
    "ACCENT": "#2563a8",
    "ACCENT_BORDER": "#2f74c0",
    "HOVER": "#e9edf1",
    "BUTTON": "#e8ebee",
    "BUTTON_HOVER": "#dfe5ea",
    "BUTTON_PRESSED": "#d3d9df",
    "BUTTON_DISABLED": "#f0f2f4",
    "BUTTON_DISABLED_BORDER": "#d5dae0",
    "CHECK_BORDER": "#9aa2ab",
    "ARROW": "#5f6771",
    "SCROLL_HANDLE": "#b8bfc7",
    "SCROLL_HANDLE_HOVER": "#a3abb4",
    "PREVIEW_BG": "#000000",
    "RED": "#c62828",
    "GREEN": "#2e7d32",
    "YELLOW": "#a06800",
    "GROOVE_1": "#1f6f3f",
    "GROOVE_2": "#6f6f1f",
    "GROOVE_3": "#7f2020",
    "SLIDER_HANDLE": "#4a5057",
    "SLIDER_HANDLE_BORDER": "#6b737b",
    "METER_BG": "#ffffff",
    "METER_PEAK": "#33393f",
    "METER_LOW": "#2f9e44",
    "METER_MID": "#cbc02a",
    "METER_HIGH": "#e03131",
    "MUTED_BG": "#c62828",
    "MUTED_BORDER": "#a01f1f",
}

THEMES: dict[str, dict[str, str]] = {"dark": DARK, "light": LIGHT}
THEME_LABELS = {"dark": "深色（OBS 默认）", "light": "浅色"}
DEFAULT_THEME = "dark"

_current = DEFAULT_THEME


def available() -> list[str]:
    return list(THEMES)


def set_theme(name: str) -> str:
    """切换当前主题，返回真正生效的主题名。"""
    global _current
    _current = name if name in THEMES else DEFAULT_THEME
    return _current


def current_name() -> str:
    return _current


def current() -> dict[str, str]:
    return THEMES[_current]


def color(key: str) -> str:
    return THEMES[_current][key]


def render_stylesheet(template: str, extra: dict[str, str] | None = None) -> str:
    """把 QSS 模板里的 @TOKEN@ 换成当前主题的颜色。

    `extra` 用来注入非颜色的东西（目前是数字框箭头图片的绝对路径），
    它和颜色一样在「残留标记」检查之前替换掉 —— 否则会被当成漏掉的颜色标记报错。

    故意不用 str.format：QSS 自身的花括号会和格式化冲突。
    """
    text = template
    for key, value in {**(extra or {}), **THEMES[_current]}.items():
        text = text.replace(f"@{key}@", value)
    leftovers = {token for token in text.split("@") if token.isupper() and "_" in token}
    if leftovers:
        raise KeyError(f"QSS 模板里有未定义的标记：{sorted(leftovers)}")
    return text
