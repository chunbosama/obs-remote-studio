"""九个设置页，逐页复刻 OBS「设置」窗口的条目、分组与文案。

**文案基准**：本机安装的 **OBS Studio 31.1.2**
（`C:\\Program Files\\obs-studio\\data\\obs-studio\\locale\\zh-CN.ini`），
分组与控件顺序对照 `frontend/forms/OBSBasicSettings.ui`。
凡是 OBS 有的条目就**照抄它的中文原文**（连标点都照抄，例如
「输出 (缩放) 分辨率」里是半角括号、「色彩格式」而不是「颜色格式」），
这样和用户装的 OBS 一字不差，不靠记忆拼凑。

**这一层只负责"长什么样、收起哪些值"**，控件与排版原语在 `form.py`。

置灰策略（贯穿全部页面，也是本项目的硬规矩）。
**分类经过事实核对**，不是凭印象分的 —— 依据两个本机来源：
① `obsws_python/reqs.py` 的 149 条请求；② 本机 OBS 的 `basic/profiles/*/basic.ini`
与 `global.ini`（哪些项落在哪，就决定了协议够不够得着）。

- `REASON_NOT_WIRED`：落在 profile `basic.ini` 里的项（视频码率、录像路径、
  采样率、画布分辨率、直播延迟、绑定 IP、TCP pacing……）。协议有办法改
  （`SetProfileParameter` / `SetVideoSettings` / `SetRecordDirectory` /
  `SetStreamServiceSettings`），但本阶段不去动用户的 OBS 配置，所以标"未接入"。
  **绝不能把这类写成"协议没有"** —— 那是错的，会误导用户。
- `REASON_REMOTE`：落在 `global.ini` 里、或压根不在配置文件里的项
  （语言、更新通道、渲染器与色彩格式、源对齐吸附、无障碍配色、进程优先级……）。
  协议没有任何请求够得着，只有 OBS 本机界面能改。
- `REASON_READONLY`：客户端能读到、只拿来做对照的项（画布/输出分辨率、
  录像路径）。

凡是本客户端真的能改的项，都放在语义最接近的原版分组里（例如
「停止直播时弹窗确认」就在 OBS 的「常规 → 输出」分组下），
客户端独有的偏好则各自归入该页末尾一个明确标注「（本客户端）」的分组。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ....utils import global_hotkeys, logging_setup
from .form import (
    REASON_NOT_WIRED,
    REASON_READONLY,
    REASON_REMOTE,
    SettingsPage,
    button,
    checkbox,
    combo,
    dim_placeholder,
    float_spin,
    int_spin,
    readonly_line,
)

# ---------------------------------------------------------------- 页面常量
# 顺序即左侧列表的顺序，与 OBS 31 的 listWidget 完全一致。
PAGE_GENERAL = "general"
PAGE_APPEARANCE = "appearance"
PAGE_STREAM = "stream"
PAGE_OUTPUT = "output"
PAGE_AUDIO = "audio"
PAGE_VIDEO = "video"
PAGE_HOTKEYS = "hotkeys"
PAGE_ACCESSIBILITY = "accessibility"
PAGE_ADVANCED = "advanced"

PAGE_ORDER: tuple[tuple[str, str, str], ...] = (
    (PAGE_GENERAL, "常规", "general"),
    (PAGE_APPEARANCE, "外观", "appearance"),
    (PAGE_STREAM, "直播", "stream"),
    (PAGE_OUTPUT, "输出", "output"),
    (PAGE_AUDIO, "音频", "audio"),
    (PAGE_VIDEO, "视频", "video"),
    (PAGE_HOTKEYS, "快捷键", "hotkeys"),
    (PAGE_ACCESSIBILITY, "无障碍环境", "accessibility"),
    (PAGE_ADVANCED, "高级", "advanced"),
)

# 下拉候选也照抄 OBS 的 zh-CN 原文
THEME_CHOICES = (("深色（OBS 默认）", "dark"), ("浅色", "light"))
DENSITY_CHOICES = ("经典", "紧凑", "正常", "舒适")
LOG_LEVEL_CHOICES = tuple(
    (logging_setup.LEVEL_LABELS.get(name, name), name) for name in logging_setup.LEVELS
)
RESOLUTION_CHOICES = (
    ("1920x1080", (1920, 1080)),
    ("1280x720", (1280, 720)),
    ("2560x1440", (2560, 1440)),
    ("3840x2160", (3840, 2160)),
)
# Basic.Settings.Video.FPSCommon 的候选项
FPS_CHOICES = (("60", 60), ("59.94", 59.94), ("30", 30),
               ("29.97", 29.97), ("25", 25), ("24", 24))
# Basic.Settings.Video.DownscaleFilter.*
DOWNSCALE_CHOICES = (
    ("双线性插值(最快, 但会变模糊)", "bilinear"),
    ("双三次插值(锐化缩放, 16 个样本)", "bicubic"),
    ("Lanczos插值(锐化缩放, 36 个样本)", "lanczos"),
    ("区域(加权和, 4/6/9个样本)", "area"),
)
# Basic.Settings.Output.Format.*
RECORD_FORMAT_CHOICES = (
    ("Matroska 视频 (.mkv)", "mkv"),
    ("混合 MP4 [测试版] (.mp4)", "hmp4"),
    ("分片 MP4 (.mp4)", "fmp4"),
    ("分片 MOV (.mov)", "fmov"),
)
# Basic.Settings.Output.Simple.RecordingQuality.*
RECORD_QUALITY_CHOICES = (
    ("与串流画质相同", "stream"),
    ("高质量, 中等文件大小", "small"),
    ("近似无损的质量, 大文件大小", "hq"),
    ("无损的质量, 非常大的文件大小", "lossless"),
)
# Basic.Settings.Output.Simple.Encoder.* （摘常用的几条）
ENCODER_CHOICES = (
    ("软件 (x264)", "x264"),
    ("硬件 (QSV, H.264)", "qsv"),
    ("硬件 (NVENC, H.264)", "nvenc"),
    ("硬件 (AMD, H.264)", "amd"),
)
# Basic.Settings.Audio.MeterDecayRate.*
METERS_CHOICES = (("快速", "fast"), ("中速(峰值电平表I型)", "medium"),
                  ("慢速(峰值电平表II型)", "slow"))
# Basic.Settings.Audio.PeakMeterType.*
PEAK_METER_CHOICES = (("采样峰值", "sample"), ("真峰值 (更高的 CPU 使用率)", "true"))
# Basic.Settings.Advanced.*
PROCESS_PRIORITY_CHOICES = (("正常", "normal"), ("高于正常", "above_normal"),
                            ("高", "high"), ("低于正常", "below_normal"), ("低", "idle"))
COLOR_FORMAT_CHOICES = (
    ("NV12 (8 位, 4:2:0, 2 个平面)", "NV12"),
    ("I420 (8 位, 4:2:0, 3 个平面)", "I420"),
    ("I444 (8 位, 4:4:4, 3 个平面)", "I444"),
    ("P010 (10 位, 4:2:0, 2 个平面)", "P010"),
)
COLOR_SPACE_CHOICES = (("Rec. 709", "709"), ("Rec. 2100 (PQ)", "2100PQ"),
                       ("Rec. 2100 (HLG)", "2100HLG"))
COLOR_RANGE_CHOICES = (("常规 (Limited)", "partial"), ("扩展 (Full)", "full"))
HOTKEY_FOCUS_CHOICES = (
    ("任何时候都开启快捷键", "never_disable"),
    ("当主窗口获得焦点时禁用快捷键", "disable_in_focus"),
    ("主窗口失去焦点时禁用快捷键", "disable_out_of_focus"),
)
AUDIO_DEVICE_CHOICES = (("已禁用", "disabled"), ("默认", "default"))
# 音频设备下拉的实机候选项由 OBS 运行时填充，这里只列 OBS 的两条固定项


class _Page(SettingsPage):
    """所有设置页的基类：带上注册用的 key / 标题 / 图标名。"""

    key = ""
    icon = "general"

    def __init__(self, ctx, parent=None):
        self.ctx = ctx
        super().__init__(ctx.title_for(self.key), parent)


# ---------------------------------------------------------------- 常规
class GeneralPage(_Page):
    """Basic.Settings.General：语言 / 更新 / 输出 / 源对齐吸附。"""

    key = PAGE_GENERAL
    icon = "general"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        config = ctx.config

        # —— 顶部：语言与两条启动行为（OBS 里这一组的主标题就是页面名「常规」，
        # 页面上方已经写了，分组里不再重复一个标题，与 OBS 观感一致）
        self.add_section()
        self.language_combo = combo(
            (("简体中文", "zh-CN"), ("English", "en-US")), "zh-CN"
        )
        self.add_row("语言", self.language_combo, REASON_REMOTE)
        self.add_full(checkbox("启动时打开统计对话框"), REASON_REMOTE)
        self.add_full(checkbox("在屏幕采集中隐藏OBS窗口"), REASON_REMOTE)

        # —— 更新（Basic.Settings.General.Updater）
        self.add_section("更新")
        self.update_channel_combo = combo(
            (("稳定版 - 最新的稳定版本（默认）", "stable"),
             ("测试版 / RC 版 - 可能不稳定的先行版本", "beta")),
            "stable",
        )
        self.add_row("更新通道", self.update_channel_combo, REASON_REMOTE)
        self.add_full(checkbox("启动时自动检查更新", True), REASON_REMOTE)

        # —— 输出（Basic.Settings.Output，在「常规」里复用这个分组标题）
        # 7 条里只有「停止直播时弹窗确认」是本客户端真的实现了的（D10），
        # 其余要 OBS 自己编排输出，远程做不到。
        self.add_section("输出")
        self.add_full(checkbox("开始直播时弹窗确认"), REASON_REMOTE)
        self.confirm_stop_check = checkbox(
            "停止直播时弹窗确认", config.confirm_stop_stream
        )
        self.add_full(self.confirm_stop_check)
        self.add_full(checkbox("停止录制时弹窗确认"), REASON_REMOTE)
        self.add_full(checkbox("直播时自动录制"), REASON_REMOTE)
        self.add_full(checkbox("停止直播后继续录制"), REASON_REMOTE)
        self.add_full(checkbox("直播时自动启动回放缓存"), REASON_REMOTE)
        self.add_full(checkbox("停止直播后保持回放缓存开启"), REASON_REMOTE)

        # —— 源对齐吸附（Basic.Settings.General.Snapping）
        self.add_section("源对齐吸附")
        self.add_full(checkbox("启用", True), REASON_REMOTE)
        self.add_row("吸附敏感度", float_spin(0.0, 100.0, 10.0, decimals=1), REASON_REMOTE)
        self.add_full(checkbox("吸附源到屏幕边缘", True), REASON_REMOTE)
        self.add_full(checkbox("吸附源到其他的源", True), REASON_REMOTE)

        # —— 系统托盘（Basic.Settings.General.SysTray）
        self.add_section("系统托盘")
        self.tray_check = checkbox("启用", config.tray_enabled)
        self.add_full(self.tray_check)
        self.close_to_tray_check = checkbox(
            "关闭窗口时最小化到托盘，而不是退出", config.close_to_tray
        )
        self.add_full(self.close_to_tray_check)
        self.add_full(checkbox("开始时最小化到系统托盘"), REASON_REMOTE)

        # —— 预览（StudioMode.Preview）。客户端的缩略图节流就是 OBS 的"预览"概念
        self.add_section("预览（本客户端）")
        self.preview_spin = int_spin(200, 10_000, config.preview_interval_ms, " ms", 100)
        self.add_row("缩略图间隔", self.preview_spin)
        self.preview_quality_spin = int_spin(10, 100, config.preview_quality)
        self.add_row("缩略图质量", self.preview_quality_spin)
        self.preview_width_spin = int_spin(160, 1920, config.preview_width, " px", 40)
        self.add_row("缩略图宽度", self.preview_width_spin)
        self.add_hint(
            "缩略图每帧都要 OBS 端完整编码一次，间隔越短越吃 CPU"
            "（1000 ms ≈ 1 fps）。演播室模式下是双画面，合计 2 帧/秒。"
        )

        # —— 客户端自己的连接与恢复策略（原版没有对应分组）
        self.add_section("连接与恢复（本客户端）")
        self.auto_check = checkbox("启动时自动连接上次配置", config.auto_connect_on_startup)
        self.add_full(self.auto_check)
        self.reconnect_check = checkbox("断开后自动重连", config.reconnect.enabled)
        self.add_full(self.reconnect_check)
        self.initial_spin = int_spin(200, 60_000, config.reconnect.initial_delay_ms, " ms", 100)
        self.add_row("初始重连间隔", self.initial_spin)
        self.max_delay_spin = int_spin(1000, 600_000, config.reconnect.max_delay_ms, " ms", 1000)
        self.add_row("最大重连间隔", self.max_delay_spin)
        # 这两条的文案与 OBS「高级 → 自动重连」一致（Basic.Settings.Output.*）
        self.attempts_spin = int_spin(0, 999, config.reconnect.max_attempts, special="无限")
        self.add_row("最大重试次数", self.attempts_spin)
        self.poll_spin = int_spin(200, 10_000, config.poll_interval_ms, " ms", 100)
        self.add_row("状态轮询间隔", self.poll_spin)
        self.timeout_spin = float_spin(0.5, 30.0, config.request_timeout_s, " s", 1, 0.5)
        self.add_row("请求超时", self.timeout_spin)
        self.heartbeat_spin = int_spin(
            0, 60_000, max(0, int(config.heartbeat_interval_ms)),
            " ms", 1000, special="关闭",
        )
        self.add_row("心跳间隔", self.heartbeat_spin)
        self.rtt_warn_spin = int_spin(50, 10_000, int(config.rtt_warn_ms), " ms", 50)
        self.add_row("判卡阈值", self.rtt_warn_spin)
        self.add_hint(
            "重连间隔按指数退避增长，直到达到上限。\n"
            "心跳用来量「通但慢」：空闲时发一次最轻的请求算往返延迟，"
            "连续超阈值就提示卡顿；填 0 可关闭。"
        )

    def apply_to(self, config):
        from dataclasses import replace

        return replace(
            config,
            reconnect=replace(
                config.reconnect,
                enabled=self.reconnect_check.isChecked(),
                initial_delay_ms=self.initial_spin.value(),
                max_delay_ms=self.max_delay_spin.value(),
                max_attempts=self.attempts_spin.value(),
            ),
            auto_connect_on_startup=self.auto_check.isChecked(),
            poll_interval_ms=self.poll_spin.value(),
            request_timeout_s=self.timeout_spin.value(),
            heartbeat_interval_ms=self.heartbeat_spin.value(),
            rtt_warn_ms=self.rtt_warn_spin.value(),
            confirm_stop_stream=self.confirm_stop_check.isChecked(),
            tray_enabled=self.tray_check.isChecked(),
            close_to_tray=self.close_to_tray_check.isChecked(),
            preview_interval_ms=self.preview_spin.value(),
            preview_quality=self.preview_quality_spin.value(),
            preview_width=self.preview_width_spin.value(),
        )


# ---------------------------------------------------------------- 外观
class AppearancePage(_Page):
    """Basic.Settings.Appearance：主题 / 样式 / 字体大小 / 密度。"""

    key = PAGE_APPEARANCE
    icon = "appearance"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)

        self.add_section("常规")
        # 主题：本客户端真的能切（H5），两套配色都是 OBS 风格
        self.theme_combo = combo(THEME_CHOICES, ctx.theme_name)
        self.add_row("主题", self.theme_combo)
        # 样式（Variant）由所选主题提供，本客户端每套主题只有一种样式
        self.variant_combo = combo((("没有可用的样式", ""),), "")
        self.add_row("样式", self.variant_combo, REASON_REMOTE)
        self.add_row("字体大小", int_spin(8, 24, 12, " pt"), REASON_REMOTE)

        # 密度：OBS 31 是四个按钮
        self.add_section()
        self.density_row = self._density_row()
        self.add_row("密度", self.density_row, REASON_REMOTE)
        # OBS 里这是一条警告文字，不是复选框 —— 别做成可勾的样子
        self.add_hint("此样式不提供某些外观选项。")
        self.add_hint(
            "OBS 的「外观」页只改 OBS 本机界面。本客户端的主题切换走「主题」下拉，"
            "深色为默认（与 OBS 默认主题一致），浅色用于强光环境。"
        )

    def _density_row(self) -> QWidget:
        """OBS 的密度是「经典 / 紧凑 / 正常 / 舒适」四个并排按钮。"""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        for index, label in enumerate(DENSITY_CHOICES):
            item = button(label)
            item.setCheckable(True)
            item.setChecked(index == 2)  # 默认「正常」
            layout.addWidget(item)
        layout.addStretch(1)
        return holder

    def apply_to(self, config):
        return config


# ---------------------------------------------------------------- 直播
class StreamPage(_Page):
    """Basic.Settings.Stream：服务 / 终点 / 高级选项。"""

    key = PAGE_STREAM
    icon = "stream"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)

        # 服务（Basic.AutoConfig.StreamPage.Service）
        self.service_combo = combo(
            (("自定义...", "custom"), ("Twitch", "twitch"),
             ("YouTube - RTMPS", "youtube")),
            "custom",
        )
        self.add_row("服务", self.service_combo, REASON_NOT_WIRED)

        self.add_section("终点")
        self.server_edit = readonly_line("")
        self.add_row("服务器", self.server_edit, REASON_NOT_WIRED)
        self.key_edit = readonly_line("")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.add_row("推流码", self.key_edit, REASON_NOT_WIRED)
        self.add_full(button("获取推流码"), REASON_NOT_WIRED)
        self.add_full(button("使用推流码"), REASON_NOT_WIRED)

        self.add_section("高级选项")
        self.add_full(checkbox("使用身份认证"), REASON_NOT_WIRED)
        self.add_row("用户名", readonly_line(""), REASON_NOT_WIRED)
        self.add_row("密码", readonly_line(""), REASON_NOT_WIRED)
        # [Stream1] IgnoreRecommended 在 profile 里 → SetProfileParameter 能改
        self.add_full(checkbox("忽略流媒体服务的推荐设置"), REASON_NOT_WIRED)
        self.add_full(
            checkbox("动态调整码率以应对网络拥堵（Beta）", True), REASON_NOT_WIRED
        )
        self.add_full(checkbox("开启带宽测试模式"), REASON_NOT_WIRED)

        # 客户端真的会做的事：推流字幕（P7）
        self.add_section("字幕（本客户端）")
        self.add_hint(
            "「工具 → 推流字幕…」可以往直播流里注入一行 CEA-608 字幕。\n"
            "只在推流进行中可用 —— OBS 对未推流的情况回 501，客户端会先拦一道。"
        )
        self.add_hint("串流密钥属于敏感信息，本客户端不做持久化，请在「连接设置」里管理凭据。")

    def apply_to(self, config):
        return config


# ---------------------------------------------------------------- 输出
class OutputPage(_Page):
    """Basic.Settings.Output：输出模式 / 直播 / 录制 / 回放缓存。"""

    key = PAGE_OUTPUT
    icon = "output"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        config = ctx.config

        # 输出模式：[Output] Mode 在 profile 里，SetProfileParameter 能改，
        # 但"简单/高级"会整体接手 OBS 的输出配置，本阶段不碰 → 未接入
        self.mode_combo = combo((("简单", "simple"), ("高级", "advanced")), "simple")
        self.add_row("输出模式", self.mode_combo, REASON_NOT_WIRED)

        # —— 直播（简单模式：Basic.Settings.Output.Adv.Streaming）
        self.add_section("直播")
        self.add_row("视频码率", int_spin(0, 100_000, 6000, " Kbps", 100), REASON_NOT_WIRED)
        self.add_row("音频码率", int_spin(32, 512, 160, " Kbps", 32), REASON_NOT_WIRED)
        self.add_row("视频编码器", combo(ENCODER_CHOICES, "x264"), REASON_NOT_WIRED)
        self.add_row("音频编码器", combo((("AAC（默认）", "aac"), ("Opus", "opus")), "aac"),
                     REASON_NOT_WIRED)

        # —— 录制（Basic.Settings.Output.Simple.*）
        self.add_section("录制")
        # 录像路径是只读对照：OBS 端的实际目录从 GetRecordDirectory 拿得到，
        # 顺便把剩余空间一起显示 —— 这正是"只读但有用"该有的样子。
        self.record_path_edit = readonly_line(ctx.record_directory_text())
        self.add_row("录像路径", self.record_path_edit, REASON_READONLY)
        self.add_full(button("选择录像目录"), REASON_NOT_WIRED)
        self.add_row("录像质量", combo(RECORD_QUALITY_CHOICES, "stream"), REASON_NOT_WIRED)
        self.add_row("录像格式", combo(RECORD_FORMAT_CHOICES, "mkv"), REASON_NOT_WIRED)
        self.add_row("音轨", combo((("音轨 1", 1), ("音轨 2", 2), ("音轨 3", 3)), 1),
                     REASON_NOT_WIRED)
        self.add_full(checkbox("生成没有空格的文件名"), REASON_NOT_WIRED)

        # —— 回放缓存（Basic.Settings.Output.ReplayBuffer.*）
        self.add_section("回放缓存")
        self.add_row("回放时长上限", int_spin(0, 3600, 20, " 秒", 5), REASON_NOT_WIRED)
        self.add_row("最大内存", int_spin(0, 8192, 500, " MB", 50), REASON_NOT_WIRED)

        # —— D17：客户端的录制预警（原版没有对应项）
        self.add_section("录制预警（本客户端）")
        self.disk_warn_spin = float_spin(
            0.0, 1000.0, float(config.disk_warn_gb), " GB", 1, 0.5, special="关闭"
        )
        self.add_row("录制目录剩余空间预警", self.disk_warn_spin)
        self.record_minutes_spin = int_spin(
            0, 24 * 60, int(config.record_warn_minutes), " 分钟", 1, special="关闭"
        )
        self.add_row("连续录制时长预警", self.record_minutes_spin)
        self.record_warn_gb_spin = float_spin(
            0.0, 1000.0, float(config.record_warn_gb), " GB", 1, 0.5, special="关闭"
        )
        self.add_row("录制文件大小预警", self.record_warn_gb_spin)
        self.add_hint(
            "剩余空间预警只在 OBS 跑在本机时有效 —— 控局域网另一台时读不到对方的磁盘。\n"
            "输出与编码器设置会改用户 OBS 的配置，本阶段只读不写。"
        )

    def apply_to(self, config):
        from dataclasses import replace

        return replace(
            config,
            disk_warn_gb=self.disk_warn_spin.value(),
            record_warn_minutes=self.record_minutes_spin.value(),
            record_warn_gb=self.record_warn_gb_spin.value(),
        )


# ---------------------------------------------------------------- 音频
class AudioPage(_Page):
    """Basic.Settings.Audio：常规 / 全局音频设备 / 电平表。"""

    key = PAGE_AUDIO
    icon = "audio"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        config = ctx.config

        self.add_section("常规")
        self.add_row("采样率", combo((("48 kHz", 48000), ("44.1 kHz", 44100)), 48000),
                     REASON_NOT_WIRED)
        self.add_row("声道", combo((("立体声", "stereo"), ("单声道", "mono")), "stereo"),
                     REASON_NOT_WIRED)

        # 全局音频设备（Basic.Settings.Audio.Devices）
        self.add_section("全局音频设备")
        for label, default in (
            ("桌面音频", "default"),
            ("桌面音频 2", "disabled"),
            ("麦克风/辅助音频", "default"),
            ("麦克风/辅助音频 2", "disabled"),
            ("麦克风/辅助音频 3", "disabled"),
            ("麦克风/辅助音频 4", "disabled"),
        ):
            self.add_row(label, combo(AUDIO_DEVICE_CHOICES, default), REASON_NOT_WIRED)

        # 电平表（Basic.Settings.Audio.Meters）
        self.add_section("电平表")
        self.add_row("衰减速率", combo(METERS_CHOICES, "medium"), REASON_REMOTE)
        self.add_row("峰值计类型", combo(PEAK_METER_CHOICES, "sample"), REASON_REMOTE)
        self.add_full(
            checkbox("低延迟音频缓冲模式（用于 Decklink/NDI 输出）"), REASON_REMOTE
        )

        # E：客户端混音器真的实现了这些开关
        self.add_section("混音器（本客户端）")
        self.meters_check = checkbox(
            "订阅电平表（高频事件，重连后生效）", config.audio_meters
        )
        self.add_full(self.meters_check)
        self.percent_check = checkbox("推子显示百分比（默认 dB）", config.mixer_show_percent)
        self.add_full(self.percent_check)
        self.add_hint(
            "电平表是 ~20Hz 的高频事件，带宽紧张时可以关掉。\n"
            "兼容模式（obs-websocket v4）下没有电平表能力，电平条会自动隐藏。"
        )

    def apply_to(self, config):
        from dataclasses import replace

        return replace(
            config,
            audio_meters=self.meters_check.isChecked(),
            mixer_show_percent=self.percent_check.isChecked(),
        )


# ---------------------------------------------------------------- 视频
class VideoPage(_Page):
    """Basic.Settings.Video：常规一组，画布/输出分辨率与帧率。"""

    key = PAGE_VIDEO
    icon = "video"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        video = getattr(ctx.store, "video", None)
        base = f"{video.base_width}x{video.base_height}" if video and video.base_width else ""
        out = f"{video.output_width}x{video.output_height}" if video and video.output_width else ""
        fps = f"{video.fps:g}" if video and video.fps else ""

        self.add_section("常规")

        # 分辨率：协议能读、本阶段不写 —— 按"只读对照"处理，把 OBS 端实际取值列出来
        self.base_combo = combo(
            RESOLUTION_CHOICES,
            (video.base_width, video.base_height) if base else None,
        )
        self.add_row("基础（画布）分辨率", self.base_combo, REASON_NOT_WIRED)
        self.add_hint("当前 OBS 端取值：" + (base or "未连接 / 未上报"))

        self.output_combo = combo(
            RESOLUTION_CHOICES,
            (video.output_width, video.output_height) if out else None,
        )
        self.add_row("输出 (缩放) 分辨率", self.output_combo, REASON_NOT_WIRED)
        # 缩小算法落在 [Video] ScaleFilter，本机这份 profile 里没有该键
        # （OBS 只在用户改过时才写），所以归"没接入"而不是"协议没有"
        self.add_row("缩小算法", combo(DOWNSCALE_CHOICES, "bicubic"), REASON_NOT_WIRED)
        self.add_hint("当前 OBS 端取值：" + (out or "未连接 / 未上报"))

        # 帧率（Basic.Settings.Video.FPS / FPSCommon / Numerator / Denominator）
        self.fps_combo = combo(FPS_CHOICES, video.fps if fps else None)
        self.add_row("常用帧率", self.fps_combo, REASON_NOT_WIRED)
        # SetVideoSettings(numerator, denominator, ...) 确实带这两个字段，
        # 所以是"没接入"而不是"协议没有"
        self.add_row("分子：", int_spin(1, 1000, 60), REASON_NOT_WIRED)
        self.add_row("分母：", int_spin(1, 1000, 1), REASON_NOT_WIRED)
        self.add_hint(
            "当前 OBS 端取值：" + (fps or "未连接 / 未上报")
            + "\n画布与输出分辨率、帧率都能从 GetVideoSettings 读到，"
            "但本阶段只读不写 —— 改它们会直接影响正在进行的直播与录制。"
        )

    def apply_to(self, config):
        return config


# ---------------------------------------------------------------- 快捷键
class HotkeysPage(_Page):
    """Basic.Settings.Hotkeys：筛选 + 一张运行时生成的键位表。"""

    key = PAGE_HOTKEYS
    icon = "hotkeys"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        config = ctx.config

        self.add_section("全局热键（本客户端）")
        self.hotkey_check = checkbox(
            f"启用全局热键（{ctx.hotkey_summary}）", config.global_hotkeys
        )
        if not global_hotkeys.available():
            self.hotkey_check.setChecked(False)
            self.add_full(self.hotkey_check, "当前系统不支持全局热键（仅 Windows 可用）")
        else:
            self.add_full(self.hotkey_check)

        # 筛选（Basic.Settings.Hotkeys.Filter / FilterByHotkey）
        self.add_section()
        self.add_row("筛选", readonly_line(""), REASON_REMOTE)
        self.add_row("按快捷键筛选", readonly_line(""), REASON_REMOTE)

        rows = ctx.hotkey_rows()
        self.table = QTableWidget(len(rows), 2)
        self.table.setHorizontalHeaderLabels(["功能", "快捷键"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for index, (name, sequence) in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(name))
            self.table.setItem(index, 1, QTableWidgetItem(sequence))
        self.table.setMinimumHeight(200)
        self.add_full(self.table, "热键在 OBS 端配置；这里只列出本客户端已注册的键位")
        self.add_hint(
            "全局热键（Ctrl+Alt+R / +L / +M）在窗口不在前台时也能触发；"
            "应用内快捷键（F5 / Ctrl+R / Ctrl+L …）见「帮助 → 快捷键」。\n"
            "「任何时候都开启快捷键」这类焦点行为由 OBS 本机管理，与客户端热键无关。"
        )

    def apply_to(self, config):
        from dataclasses import replace

        enabled = self.hotkey_check.isChecked() if global_hotkeys.available() else False
        return replace(config, global_hotkeys=enabled)


# ---------------------------------------------------------------- 无障碍环境
class AccessibilityPage(_Page):
    """Basic.Settings.Accessibility：使用不同的颜色（预设 + 九个配色）。"""

    key = PAGE_ACCESSIBILITY
    icon = "accessibility"

    # Basic.Settings.Accessibility.ColorOverrides.*
    COLOR_ROWS = (
        "源边框（选中）",
        "源边框（裁剪）",
        "源边框（悬停）",
        "混音台音量分段（-60 至 -20dB）",
        "混音台音量分段（-20 至 -9dB）",
        "混音台音量分段（-9 至 0dB）",
        "混音台音量分段（-60 至 -20dB，激活）",
        "混音台音量分段（-20 至 -9dB，激活）",
        "混音台音量分段（-9 至 0dB，激活）",
    )

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)

        # 整页都作用在 OBS 本机界面上，远程客户端接管不了
        self.add_section("使用不同的颜色")
        self.preset_combo = combo(
            (("默认", "default"), ("色盲替代方案", "colorblind"), ("自定义", "custom")),
            "default",
        )
        self.add_row("预设颜色", self.preset_combo, REASON_REMOTE)

        self.swatches: list[QWidget] = []
        for label in self.COLOR_ROWS:
            row = self._swatch_row()
            self.swatches.append(row)
            self.add_row(label, row, REASON_REMOTE)

        self.add_hint(
            "无障碍环境只作用在 OBS 本机界面上。本客户端的可访问性做法是："
            "深色/浅色两套主题（见「外观」页）、所有控件带悬停说明、工具栏按钮均有文字标签。"
        )

    def _swatch_row(self) -> QWidget:
        """OBS 里每一项是「颜色块 + 选择颜色按钮」。"""
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        chip = QLabel("")
        chip.setFixedSize(22, 16)
        chip.setStyleSheet("background: #4a4a4a; border: 1px solid #787878;")
        layout.addWidget(chip)
        layout.addWidget(button("选择颜色"))
        layout.addStretch(1)
        return holder

    def apply_to(self, config):
        return config


# ---------------------------------------------------------------- 高级
class AdvancedPage(_Page):
    """Basic.Settings.Advanced：常规 / 视频 / 网络 / 快捷键 + 客户端项。"""

    key = PAGE_ADVANCED
    icon = "advanced"

    def __init__(self, ctx, parent=None):
        super().__init__(ctx, parent)
        config = ctx.config

        # —— 常规（Basic.Settings.Advanced.General）
        self.add_section("常规")
        self.add_row("进程优先级", combo(PROCESS_PRIORITY_CHOICES, "normal"), REASON_REMOTE)
        self.add_full(
            checkbox("如果有处于活动状态的输出，则在退出时显示警告"), REASON_REMOTE
        )

        # —— 视频（Basic.Settings.Advanced.Video）
        self.add_section("视频")
        self.add_row("渲染器", combo((("Direct3D 11", "d3d11"), ("OpenGL", "opengl")),
                                    "d3d11"), REASON_REMOTE)
        self.add_row("视频适配器", combo((("自动", "auto"),), "auto"), REASON_REMOTE)
        self.add_row("色彩格式", combo(COLOR_FORMAT_CHOICES, "NV12"), REASON_REMOTE)
        self.add_row("色彩空间", combo(COLOR_SPACE_CHOICES, "709"), REASON_REMOTE)
        self.add_row("色彩范围", combo(COLOR_RANGE_CHOICES, "partial"), REASON_REMOTE)
        self.add_row("SDR 白电平", int_spin(100, 1000, 300, " 尼特", 10), REASON_REMOTE)
        self.add_row("HDR 标称峰值电平", int_spin(100, 10000, 1000, " 尼特", 50),
                     REASON_REMOTE)

        # —— 直播延迟（Basic.Settings.Advanced.StreamDelay）
        self.add_section("直播延迟")
        self.add_full(checkbox("启用"), REASON_NOT_WIRED)
        self.add_row("延迟时间", int_spin(0, 3600, 20, " 秒", 1), REASON_NOT_WIRED)
        self.add_full(
            checkbox("重新连接时保持截止点 (增加延迟)"), REASON_NOT_WIRED
        )

        # —— 自动重连（Basic.Settings.Output.Reconnect）
        # 这三个键在 profile 的 [Output] 里（Reconnect / RetryDelay / MaxRetries），
        # SetProfileParameter 写得进去 —— 所以是"未接入"，不是"协议没有"。
        # 它们管的是 **OBS 自己的**推流重连，与客户端的断线重连是两回事。
        self.add_section("自动重连")
        self.add_full(checkbox("启用", config.reconnect.enabled), REASON_NOT_WIRED)
        self.add_row("重连尝试间隔", int_spin(200, 60_000, 1000, " ms", 100),
                     REASON_NOT_WIRED)
        self.add_row("最大重试次数", int_spin(0, 999, 20, special="无限"),
                     REASON_NOT_WIRED)
        self.add_hint(
            "这一组是 OBS 自身的推流重连设置。本客户端的断线重连在"
            "「常规 → 连接与恢复（本客户端）」里配置。"
        )

        # —— 网络（Basic.Settings.Advanced.Network）。四个键都在 profile 的
        # [Output] 里（BindIP / IPFamily / NewSocketLoopEnable / LowLatencyEnable）
        self.add_section("网络")
        self.add_row("绑定到 IP", combo((("默认", "default"),), "default"),
                     REASON_NOT_WIRED)
        self.add_row("IP 族", combo((("IPv4 和 IPv6", "both"), ("仅 IPv4", "ipv4"),
                                    ("仅 IPv6", "ipv6")), "both"), REASON_NOT_WIRED)
        self.add_full(checkbox("开启网络优化"), REASON_NOT_WIRED)
        self.add_full(checkbox("开启 TCP pacing"), REASON_NOT_WIRED)

        # —— 快捷键与窗口焦点（Basic.Settings.Advanced.Hotkeys）
        self.add_section("快捷键与窗口焦点")
        self.add_row("快捷键与窗口焦点", combo(HOTKEY_FOCUS_CHOICES, "never_disable"),
                     REASON_REMOTE)

        # —— 客户端自己的高级项（I3 日志）
        self.add_section("日志（本客户端）")
        self.log_level_combo = combo(LOG_LEVEL_CHOICES, config.log_level)
        self.add_row("日志级别", self.log_level_combo)
        self.log_to_file_check = checkbox("同时写入日志文件（排障用）", config.log_to_file)
        self.add_full(self.log_to_file_check)
        self.log_path_label = dim_placeholder(str(logging_setup.log_file_path()))
        self.log_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.add_row("日志位置", self.log_path_label)
        self.open_log_btn = button("打开日志目录")
        self.add_full(self.open_log_btn)
        self.add_hint("日志文件按 1 MB 滚动，最多保留 3 份。")

        # —— H10：紧凑模式
        self.add_section("窗口与集成（本客户端）")
        self.compact_check = checkbox("紧凑模式（只留控制按钮与状态栏）", config.compact_mode)
        self.add_full(self.compact_check)
        self.add_hint(
            "布局（几何 + 分栏 + 面板显隐 + 主题）会自动持久化；"
            "关闭窗口最小化到托盘后，用托盘菜单退出。"
        )

        # —— 协议：连接设置里的选项，这里只做只读回显
        self.add_section("协议（本客户端）")
        self.protocol_combo = combo(
            (("自动识别（推荐）", "auto"),
             ("仅 obs-websocket v5（标准模式）", "v5"),
             ("仅 obs-websocket v4（兼容模式，OBS ≤ 27）", "v4")),
            getattr(config, "protocol", "auto"),
        )
        self.add_full(self.protocol_combo, "协议选择在「文件 → 连接设置…」里改，随连接一起保存")

    def apply_to(self, config):
        from dataclasses import replace

        return replace(
            config,
            log_level=str(self.log_level_combo.currentData() or "INFO"),
            log_to_file=self.log_to_file_check.isChecked(),
            compact_mode=self.compact_check.isChecked(),
        )


PAGE_CLASSES: tuple[type[_Page], ...] = (
    GeneralPage,
    AppearancePage,
    StreamPage,
    OutputPage,
    AudioPage,
    VideoPage,
    HotkeysPage,
    AccessibilityPage,
    AdvancedPage,
)

__all__ = [
    "PAGE_ORDER",
    "PAGE_CLASSES",
    "PAGE_GENERAL",
    "PAGE_APPEARANCE",
    "PAGE_STREAM",
    "PAGE_OUTPUT",
    "PAGE_AUDIO",
    "PAGE_VIDEO",
    "PAGE_HOTKEYS",
    "PAGE_ACCESSIBILITY",
    "PAGE_ADVANCED",
]
