# OBS Remote Studio

用 PySide6 写的 OBS Studio 远程控制客户端（MVP），通过 **obs-websocket v5** 控制本机或局域网里**已经启动**的 OBS。  
界面分区参照原版 OBS：左侧场景/来源、中间预览、底部控制与混音器、顶部菜单与状态栏。

## 运行前提(源码)

| 项          | 要求                                                    |
| ---------- | ----------------------------------------------------- |
| OBS Studio | ≥ 28（内置 obs-websocket v5），本项目按 **OBS 31 / ws 5.5** 开发 |
| 服务端        | OBS 菜单「工具 → WebSocket 服务器设置」→ 启用服务器并设置密码              |
| 端口         | 默认 4455（v5 强制鉴权，没密码连不上）                               |
| 局域网        | 放行对端 TCP 4455 入站                                      |
| Python     | ≥ 3.10                                                |

## 一键启动（Windows）

双击项目根目录的 **`start.bat`** 即可。它会自动准备 `.venv`、按需安装依赖、检查 OBS 是否在运行，  
然后拉起界面（无黑色控制台窗口），运行日志写入 `logs\studio-<时间戳>.log`。

命令行里也能传参数（会透传给 `scripts\start.ps1`）：

```bat
start.bat                 :: 常规启动
start.bat -Console        :: 保留控制台窗口、实时看日志，排错用
start.bat -Reinstall      :: 强制重装依赖
start.bat -SkipDeps       :: 跳过依赖检查，最快启动
start.bat -SmokeTest      :: 启动前先跑一遍冒烟测试
start.bat -DebugObsWs     :: 打开 obsws-python 的 DEBUG 日志（会打印含密码的连接串）
```

## 安装与运行（手动）

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"     # Windows
python -m obs_remote_studio
```

首次启动后：菜单「文件 → 连接设置…」，填地址 / 端口 / 密码，可先点「测试连接」。

## 打包成 exe（分发用）

双击项目根目录的 **`build.bat`**（或 `python scripts\packaging\build.py`），约 2~4 分钟出两种产物：

| 产物                                             | 说明                                                     |
| ---------------------------------------------- | ------------------------------------------------------ |
| `dist\OBSRemoteStudio-<版本>-win64-single.exe`   | **单文件版**：拷走一个 exe 就能跑。启动时自解压到临时目录，慢几秒                  |
| `dist\OBSRemoteStudio-<版本>-win64-portable.zip` | **压缩包版**：解压后运行里面的 `OBSRemoteStudio.exe`，启动更快、缺文件一眼看得出来 |

0.1.0 实测：单文件 **31.3 MB**、压缩包 **31.3 MB**（解压后 82 MB）。目标机器**不需要装 Python**。

```bat
build.bat                     :: 两种都出
build.bat --only single       :: 只要单文件 exe
build.bat --only portable     :: 只要压缩包版
build.bat --clean             :: 先清 build\ 缓存再构建
build.bat --no-selftest       :: 跳过产物自检（不建议：等于不验货）
```

打包用到 PyInstaller，已列进 `pyproject.toml` 的可选依赖（`pip install -e ".[build]"`）；脚本发现没装会自己装进项目 `.venv`。



### 配置放在哪

配置与日志都在 `%LOCALAPPDATA%\obs-remote-studio\OBS Remote Studio\`，**不在程序目录**：  
换新版本直接覆盖程序文件即可，连接配置与密码不会丢；要彻底清干净就删那个目录。

## 已实现

- **连接**：配置持久化、握手与版本校验、认证失败/端口错/OBS 未启动的明确提示、指数退避自动重连、断线后状态清空
- **场景**：列表加载与当前高亮、点击或下拉切换、OBS 端改动实时同步
- **来源**：列表加载（含类型）、显隐切换、跟随节目/预览场景刷新
- **录制 / 推流**：启停、状态与时长回显、推流丢帧/帧数、重连中提示
- **预览区**：`GetSourceScreenshot` 静态缩略图，默认 **1 秒 1 帧**；    
  演播室模式下预览/节目各 1 帧（合计 2 帧/秒）。宽高按画布比例下发、质量 60，    
  间隔与质量可在「工具 → 设置」里调
- **转场**：转场方式下拉切换、时长设置；`SetCurrentSceneTransitionDuration`    
  走能力探测（服务端不支持或该转场不可配时输入框自动禁用）；转场进行中按钮显示「转场中…」
- **演播室模式**：开关、预览/节目双画面、点场景＝设为预览、「转场」按当前转场推进、    
  「CUT」直接把预览顶到节目；开启后**来源列表跟随预览场景**（与 OBS 行为一致）
- **状态栏**：连接三态、推流信号格、录制/直播计时、CPU 与实际/期望帧率（丢帧在悬停提示里）
- **界面**：整体对齐 OBS Studio 默认布局；深色（默认）/ 浅色两套主题可切
- **混音器**：
  - 音频源识别：`inputKind` 白名单 + 电平表反证（真在出声的源一定推电平）+ 手动隐藏，三重兜底
  - 非线性推子：0 dB 落在 3/4 处，与 OBS 手感一致；拖动只改本地显示，松手或停手 150ms 才发请求
  - 静音切换、电平表（含 1.5s 峰值保持，可在设置里关掉订阅）
  - 「工具 → 高级音频属性」：监听类型 / 声道平衡 / 同步偏移 / 混音轨 1–6，打开时才逐源拉取
  - dB / 百分比显示切换
- **推流字幕**：「工具 → 推流字幕…」往直播流里注入一行 CEA-608 字幕（报幕 / 公告）。
  **只在推流时可用** —— OBS 对未推流的情况回 501，所以本地先拦一道并把原因写在窗内。
  「清除字幕」单独一个按钮（走空串，协议允许）；不做“输入框清空即发”，避免误清屏。
  OBS 没有“持续显示”的概念，一条条发、新的覆盖旧的，所以窗口不保留任何字幕状态。
- **窗口与集成**：布局持久化（几何 + 三处分栏 + 面板显隐 + 主题）、    
  系统托盘（录制/直播/工作室模式/显隐窗口/退出，可关窗最小化到托盘）、    
  全局热键（`Ctrl+Alt+R/L/M`、`Ctrl+Alt+1~9`，默认关闭）、应用内快捷键（F5 / `Ctrl+R` / `Ctrl+L` …）

## 界面（对齐 OBS Studio）

```
菜单栏  文件(F) 视图(V) 停靠窗口(D) 工具(T) 帮助(H)
┌──────────────────────────────────────────────────────┐
│ 预览：场景 │ 转场动画/快捷转场 │ 输出：场景 2          │
│            │ 转场方式 + 时长   │                      │
│            │ T 型推杆          │                      │
│ − 94% + [缩放至窗口 ▾]                                │
├──────────────────────────────────────────────────────┤
│ 场景 │ 来源 │ 混音器 │ 转场动画 │ 控制按钮             │
├─ ● 已连接 127.0.0.1:4455 │ ▂▄▆█ │ 录制 │ 直播 │ CPU │ FPS ┤
```

- 双画面只在**演播室模式**下出现（与 OBS 相同），非演播室模式只显示「输出」
- 场景列表蓝色选中行＝当前操作场景：演播室模式下是预览场景，来源列表也跟随它
- 缩放条是从 OBS 抄的真功能：`缩放至窗口` 按控件大小自适应，`−/+` 在 10%~200% 之间步进
- **界面里置灰的按钮对应 OBS 的编辑类能力（新建/删除场景与来源、来源属性与滤镜、    
  锁定、快捷转场、T 型推杆、虚拟摄像机、转场列表增删），本阶段没有接入后端**，    
  一律置灰并带悬停说明，避免"点了没反应"。要接通的话每条都在一两个请求以内。


## 目录结构

```
src/obs_remote_studio/
├─ app.py                 入口
├─ core/                  与 UI 无关的领域层（只用 QtCore）
│  ├─ protocol.py         订阅掩码、事件表、请求常量、版本解析
│  ├─ models.py           dataclass
│  ├─ settings.py         QSettings 持久化
│  ├─ obs_worker.py       QThread 内的 obsws-python 客户端（唯一的网络出口）
│  ├─ audio.py            音量 dB↔推子位置、电平表解析（纯计算，可单测）
│  ├─ state_store.py      唯一数据源 + 信号广播
│  └─ controller.py       事件归约、轮询、重连退避
└─ ui/
   ├─ main_window.py      OBS 式布局 + 菜单
   ├─ theme.py            OBS 深色配色（QSS 与手绘图标共用）
   ├─ widgets/
   │  ├─ dock_panel.py        OBS 式面板容器（标题栏 + 内容）与工具按钮
   │  ├─ preview_panel.py     双画面 + 中间转场列 + 缩放条
   │  ├─ scene_list.py        场景面板
   │  ├─ source_list.py       来源面板（手绘眼睛/锁图标）
   │  ├─ mixer.py             混音器：推子 / 电平表 / 静音 / dB-% 切换
   │  ├─ transitions_panel.py 转场动画面板
   │  ├─ controls_panel.py    控制按钮面板
   │  └─ status_bar.py        状态栏
   ├─ tray.py             系统托盘
   ├─ dialogs/            connect_dialog / settings_dialog / advanced_audio_dialog
   │                      / quick_transitions_dialog / diagnostics_dialog
   │                      / stream_caption_dialog（P7 推流字幕）
   └─ resources/style.qss OBS 深色主题

utils/icons.py            手绘图标（眼睛/锁/时钟/信号格/指示灯/扬声器/应用图标）
utils/resources.py        随包资源定位（源码运行 / 打包后两态通用，都走它）
utils/global_hotkeys.py   Windows 全局热键（ctypes RegisterHotKey + 原生事件过滤器）
scripts/render_ui_preview.py  把窗口离屏渲染成 PNG，改版前后对照用
scripts/packaging/        打包：obs_remote_studio.spec（PyInstaller 配置）、
                          make_icon.py（现画图标拼 .ico）、build.py（一键构建）
```

分层约定：`core` 不 import Qt Widgets，`ui` 只订阅 `state_store` 的信号；  
所有阻塞网络调用都在 `ObsWorker` 所在线程，UI 线程零网络 IO。

## 测试

```bash
python tests/smoke_test.py       # 主链路 229 项（连接/场景/来源/录制推流/事件回显/重连/协议能力与 204 自愈）
python tests/audio_test.py       # 混音器 54 项（源识别/推子映射/音量静音/电平表/高级属性/隐藏/批量静音统计/配置坏值兜底）
python tests/ui_style_test.py    # UI 170 项（配置隔离、双主题、OBS 式面板、置灰按钮、托盘、混音器界面）
python tests/ui_layout_stability_test.py   # 布局稳定性 16 项（连跑缩略图，尺寸不许漂）

# 改界面时用这个看效果（连假 OBS，不弹窗，直接出图）
python scripts/render_ui_preview.py docs/ui-preview.png          # 演播室模式（深色）
python scripts/render_ui_preview.py docs/ui-preview-light.png --light   # 浅色主题
python scripts/render_ui_preview.py docs/ui-preview-single.png --no-studio

# 打包产物的自检（build.bat 每次会自动跑；也可以手动验一个现成的 exe）
dist\OBSRemoteStudio-0.1.0-win64-single.exe --selftest
```

`tests/fake_obs_server.py` 是一个实现了 v5 握手/鉴权/事件推送的假 OBS（需 `websockets`），  
覆盖连接、场景、来源显隐、录制/推流、事件回显、缩略图、转场、演播室模式、断线重连。

也可以只起假服务器，用真实 GUI 连它：

```bash
python tests/fake_obs_server.py     # 打印端口，密码 testpass
```

## 已知限制


- OBS 不会把推流失败的具体原因经 obs-websocket 返回，只在状态栏显示状态枚举，详情需看 OBS 日志。
- 录制/推流时长优先用 OBS 返回的 `outputDuration`，缺失时回退到客户端计时（断线重连后可能不准）。
- 密码目前明文存放在 QSettings；要接 keyring 只需替换 `settings.py` 里的  
  `_read_password` / `_write_password`。
- 缩略图每帧都要 OBS 端完整编码一次。默认 480px 宽 / 质量 60 已经很轻，    
  但演播室模式下是双画面，合计 2 帧/秒；若 OBS 端 CPU 吃紧，就把间隔调大。
- T型推杆仅在OBS 29.0.2版本及以下可以正常使用。在连接到高于此版本的OBS时，不会显示推杆。
