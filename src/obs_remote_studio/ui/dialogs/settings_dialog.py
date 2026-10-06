"""设置对话框（I2 及后续所有偏好项）—— 现为仿 OBS 原版的分页设置面板。

真正的实现在 `settings_ui/` 包里：
- `settings_ui/form.py`   排版原语（分组标题 / 细横线 / 右对齐标签列 / 置灰助手）
- `settings_ui/pages.py`  九个页面（常规 / 外观 / 直播 / 输出 / 音频 / 视频 / 快捷键 / 无障碍环境 / 高级）
- `settings_ui/dialog.py` 左侧页面列表 + 右侧滚动页 + 确定/取消/应用的外壳

这个模块只做转发，保留 `from .dialogs.settings_dialog import SettingsDialog`
这条既有导入路径不变（主窗口与测试都在用）。
"""

from __future__ import annotations

from .settings_ui.dialog import SettingsContext, SettingsDialog

__all__ = ["SettingsDialog", "SettingsContext"]
