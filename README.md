# OBS Remote Studio

用 PySide6 写的 OBS Studio 远程控制客户端（MVP），通过 **obs-websocket** 控制本机或局域网里**已经启动**的 OBS。  
同时支持 **v5（标准模式，OBS ≥ 28）** 与 **v4（兼容模式，OBS ≤ 27）**，连接时自动识别。
界面分区参照原版 OBS：左侧场景/来源、中间预览、底部控制与混音器、顶部菜单与状态栏。

## 运行前提

| 项          | 要求                                                    |
| ---------- | ----------------------------------------------------- |
| OBS Studio | ≥ 28（内置 obs-websocket v5）或 ≤ 27（插件 obs-websocket v4） |
| 服务端        | OBS 菜单「工具 → WebSocket 服务器设置」→ 启用服务器并设置密码              |
| 端口         | v5 默认 4455、v4 默认 4444（可在连接设置里改）                         |
| 局域网        | 放行对端对应端口入站                                              |
| Python     | ≥ 3.10                                                |

> 本项目按 **OBS 31 / ws 5.5** 开发，同时兼容 **obs-websocket v4（4.9.1 协议）**。
> 连上后标题与状态栏会写明当前是「标准模式」还是「兼容模式」。

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

> **注**：项目自身的包**不需要** `pip install -e .` —— `start.ps1` 会自己设
> `PYTHONPATH=src`（所以上面的 `pip install -e` 可选，不装也能跑与测）。

### 环境出错时的排查（.venv 打不开）

`.venv\Scripts\python.exe` 是**版本无关的启动器**，真正的解释器由 `.venv\pyvenv.cfg`
的 `home` 决定。若 `python.exe` 报 `did not find executable at '...'`，说明**那个 home 路径
不可读了**（常见于基准解释器装在会被重命名/迁移的目录下），此时 `.venv` 里的依赖
其实完好。按顺序排查：

1. 读 `.venv\pyvenv.cfg` 拿到 `home`，确认该路径下的 `python.exe` 是否还在、
   是否可读（用 `cmd /c dir` 确认，别看 PowerShell 的中文措辞）；
2. 若只是路径没了，**改 `home` 到本机任意可用的 Python（≥ 3.10）即可**，
   因为 PySide6 等 wheel 是 `abi3`（只依赖 `python3.dll`），换小版本仍可加载；
3. 若依赖确实损坏，用本机 Python 重建：`python -m venv .venv` 后
   `pip install -e ".[dev]"`。


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
- **预览区（F2）**：`GetSourceScreenshot` 静态缩略图，默认 **1 秒 1 帧**；
    
  演播室模式下预览/节目各 1 帧（合计 2 帧/秒）。宽高按画布比例下发、质量 60，
    
  间隔与质量可在「工具 → 设置」里调
- **转场（G1/G2）**：转场方式下拉切换、时长设置；`SetCurrentSceneTransitionDuration`
    
  走能力探测（服务端不支持或该转场不可配时输入框自动禁用）；转场进行中按钮显示「转场中…」
- **场景级转场覆盖（P3）**：转场动画面板里多一行「覆盖」—— 让**某个场景**在切换时
  用自己的转场与时长（OBS 本机是场景列表右键菜单里的「覆盖转场动画」）。
  选「无（跟随全局）」即清除覆盖、回到全局转场。三条已核实的协议要点：
  没有覆盖时服务端回 **null**；写入的两个字段都可选、都允许 null（null＝删掉该项），
  但名字不存在回 600、两个都不给回 400、时长限 50~20000（客户端先夹住再下发）；
  **OBS 只在"转场名的覆盖存在"时才读这个时长**，所以没选转场名时这个框是置灰的，
  不会让用户白设一个不生效的时长。兼容模式（v4）同样支持（4.8.0 起），
  翻译层负责把它那套 `""`/`-1` 哨兵值与 `RemoveSceneTransitionOverride` 对上 v5 的 null 语义
- **演播室模式（G3）**：开关、预览/节目双画面、点场景＝设为预览、「转场」按当前转场推进、
    
  「CUT」直接把预览顶到节目；开启后**来源列表跟随预览场景**（与 OBS 行为一致）
- **状态栏**：连接三态、推流信号格、录制/直播计时、CPU 与实际/期望帧率（丢帧在悬停提示里）
- **界面**：整体对齐 OBS Studio 默认布局；深色（默认）/ 浅色两套主题可切
- **混音器（E1–E7）**：
  - 音频源识别：`inputKind` 白名单 + 电平表反证（真在出声的源一定推电平）+ 手动隐藏，三重兜底
  - 非线性推子：0 dB 落在 3/4 处，与 OBS 手感一致；拖动只改本地显示，松手或停手 150ms 才发请求
  - 静音切换、电平表（含 1.5s 峰值保持，可在设置里关掉订阅）
  - 「工具 → 高级音频属性」：监听类型 / 声道平衡 / 同步偏移 / 混音轨 1–6，打开时才逐源拉取
  - dB / 百分比显示切换
- **推流字幕（P7）**：「工具 → 推流字幕…」往直播流里注入一行 CEA-608 字幕（报幕 / 公告）。
  **只在推流时可用** —— OBS 对未推流的情况回 501，所以本地先拦一道并把原因写在窗内。
  「清除字幕」单独一个按钮（走空串，协议允许）；不做“输入框清空即发”，避免误清屏。
  OBS 没有“持续显示”的概念，一条条发、新的覆盖旧的，所以窗口不保留任何字幕状态。
- **窗口与集成（H4–H7）**：布局持久化（几何 + 三处分栏 + 面板显隐 + 主题）、
    
  系统托盘（录制/直播/工作室模式/显隐窗口/退出，可关窗最小化到托盘）、
    
  全局热键（`Ctrl+Alt+R/L/M`、`Ctrl+Alt+1~9`，默认关闭）、应用内快捷键（F5 / `Ctrl+R` / `Ctrl+L` …）
- **设置面板（I2 及后续偏好项）**：仿 OBS 原版的**九个分页**（常规 / 外观 / 直播 / 输出 /
  音频 / 视频 / 快捷键 / 无障碍环境 / 高级），文案逐条对照本机 OBS 的 `zh-CN.ini`；
  底部「确定 / 取消 / **应用**」，应用就地生效且不关窗口；做不到的项按三类原因置灰并写明
  （详见下文「设置面板」一节）
- **obs-websocket v4 兼容（兼容模式）**：连接时自动识别协议（v5 连上立刻发握手帧、v4 一直静默），
  也可在连接设置里手动指定。识别为 v4 时标题与状态栏显示「**兼容模式**」，v5 显示「**标准模式**」。
  **v4 不支持而 v5 支持的组件自动隐藏或置灰**并写明原因，不会出现"点了没反应"：

  | 组件 | v5（标准模式） | v4（兼容模式） |
  | --- | --- | --- |
  | 音频电平表 | ✅ | ❌ 隐藏电平条（v4 完全没有电平表能力） |
  | 声道平衡 | ✅ | ❌ 高级音频属性里置灰 |
  | 删除场景 | ✅ | ❌ 置灰（v4 无任何删除场景的请求） |
  | 场景排序 | ❌ | ❌ 置灰（两代协议都没有排序请求） |
  | 虚拟摄像机 / 回放缓冲 | ✅ | 依 v4 版本与服务端配置显隐 |
  | 媒体控制 / 场景集合 / 配置文件 / 推流字幕 / 音轨 | ✅ | 依 v4 版本显隐（需 4.9.0+；4.9.1 起含虚拟摄像机与音轨） |

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

## 设置面板（仿 OBS 原版，9 个分页）

「工具 → 设置…」（`Ctrl+,`）打开，结构与文案一比一对照 **OBS Studio 31** 的设置窗口：

```
┌ 常规 ────────┬──────────────────────────────────────────┐
│ 常规          │ 常规                                     │
│ 外观          │ ─────────────────────────────────────    │
│ 直播          │        语言 [ 简体中文            ▾ ]     │
│ 输出          │ ☐ 启动时打开统计对话框                    │
│ 音频   ✔      │ ☐ 在屏幕采集中隐藏OBS窗口                 │
│ 视频          │                                          │
│ 快捷键        │ 更新                                     │
│ 无障碍环境    │ ─────────────────────────────────────    │
│ 高级          │      更新通道 [ 稳定版 - 最新的稳定版 ▾ ] │
├───────────────┴──────────────────────────────────────────┤
│                                     [确定] [取消] [应用]  │
└──────────────────────────────────────────────────────────┘
```

- 九个页面：**常规 / 外观 / 直播 / 输出 / 音频 / 视频 / 快捷键 / 无障碍环境 / 高级**，
  左侧带手绘图标，右侧是「细横线 + 粗体分组标题 + 右对齐标签列」的同一套栅格
- **文案以本机 OBS 的 `zh-CN.ini` 为准**，例如「输出 (缩放) 分辨率」是半角括号、
  是「色彩格式」而不是「颜色格式」、「在屏幕采集中隐藏OBS窗口」不加空格 —— 与用户
  装的 OBS 一字不差
- **「应用」不是摆设**：就地生效且不关窗口，方便一边调缩略图间隔一边看主窗口变化
- 原设置项全部保留并归到语义最接近的原版分组：重连与轮询在「常规 → 连接与恢复」、
  缩略图在「常规 → 预览」、混音器在「音频 → 混音器」、录制预警在「输出 → 录制预警」、
  日志与紧凑模式在「高级」

**凡做不到的项一律置灰，并写明属于哪一类**（分类经过与协议和本机配置核对，不凭印象）：

| 原因 | 含义 | 例子 | 条数 |
| --- | --- | --- | --- |
| 只有 OBS 本机能改 | 项落在 `global.ini` 或不在配置文件里，协议**没有任何请求够得着** | 语言、更新通道、渲染器、色彩格式/空间/范围、源对齐吸附、无障碍配色、进程优先级 | 38 |
| 本阶段未接入 | 项落在 profile 的 `basic.ini` 里，协议改得了（`SetProfileParameter` 一类），但本阶段不去动用户的 OBS 配置 | 视频/音频码率、录像路径与格式、采样率与声道、画布/输出分辨率、帧率、直播延迟、绑定 IP、TCP pacing | 37 |
| 只读对照 | 客户端能读，只拿 OBS 端的实际取值做参照 | 画布/输出分辨率、录像路径（显示 OBS 报来的真实目录） | 1 |

> 置灰措辞里**不会**出现"协议不支持"这类没核对过的说法 —— 例如
> `SetVideoSettings`、`SetRecordDirectory`、`SetProfileParameter` 其实都存在，
> 把它们写成"协议没有"会误导用户。

九个页面的对照图：`docs/settings-general.png` 等（见「测试」一节的渲染命令）。

## 仍未做

实时视频流预览（obs-websocket 不提供视频帧流，缩略图已是协议能力上限）、  
**来源**的新建/删除与属性编辑（属性表单要按 `inputKind` 动态生成，见 C6/C7）、
滤镜的增删改（K4）、输出与编码器设置（D9，会改用户 OBS 配置；
**现在已在设置面板的「输出」页里列出并置灰说明**，接通只差把 `SetProfileParameter`
一类请求接上）、
场景集合/配置文件的增删改（**协议不支持**：无 `RemoveSceneCollection`、无任何 Rename 请求）、
**场景排序**（协议无此请求，只能去 OBS 端手动做）。
理由见 `docs/mvp-feature-list.md`。

> obs-websocket v4 兼容**已实现**（见上文「已实现」）。注意 **v4 也没有**删除场景、
> 声道平衡与场景排序；**电平表则是 v4 完全没有的能力**，兼容模式下会直接隐藏。

## 目录结构

```
src/obs_remote_studio/
├─ app.py                 入口
├─ core/                  与 UI 无关的领域层（只用 QtCore）
│  ├─ protocol.py         订阅掩码、事件表、请求常量、版本解析
│  ├─ protocol_v4.py      obs-websocket v4 翻译层（请求/应答/事件/能力合成/错误码，纯逻辑可单测）
│  ├─ client_v4.py        v4 websocket 客户端 + 协议自动探测（伪装成 obsws-python 接口）
│  ├─ models.py           dataclass
│  ├─ settings.py         QSettings 持久化
│  ├─ obs_worker.py       QThread 内的网络出口（v5 走 obsws-python、v4 走 client_v4）
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
   ├─ dialogs/            connect_dialog / advanced_audio_dialog
   │                      / quick_transitions_dialog / diagnostics_dialog
   │                      / stream_caption_dialog（P7 推流字幕）
   │  └─ settings_ui/     仿 OBS 的分页设置面板
   │     ├─ form.py       排版原语（细横线 + 分组标题 + 右对齐标签列 + 置灰助手）
   │     ├─ pages.py      九个页面，文案逐条对照本机 OBS 的 zh-CN.ini
   │     └─ dialog.py     外壳（左侧页面列表 + 右侧滚动页 + 确定/取消/应用）
   │       settings_dialog.py 只做转发，保留原有导入路径
   └─ resources/style.qss OBS 深色主题

utils/icons.py            手绘图标（眼睛/锁/时钟/信号格/指示灯/扬声器/应用图标/
                          设置面板九个页面图标）
utils/resources.py        随包资源定位（源码运行 / 打包后两态通用，都走它）
utils/global_hotkeys.py   Windows 全局热键（ctypes RegisterHotKey + 原生事件过滤器）
scripts/render_ui_preview.py        把窗口离屏渲染成 PNG，改版前后对照用
scripts/render_settings_preview.py  把设置面板九个页面渲染成 PNG，与 OBS 原版对照
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
python tests/v4_compat_test.py   # obs-websocket v4 兼容 163 项（翻译层纯逻辑 + 端到端 + 老 v4 降级 + 显式指定协议）
python tests/settings_dialog_test.py       # 设置面板 159 项（九页结构/栅格/置灰规矩与原因/可用项写回/应用与确定/主窗口接线/文案对齐 OBS 原文）
python tests/scene_override_test.py        # P3 场景级转场覆盖 73 项（v5/v4 协议语义 + 端到端 + 界面置灰）

# 改界面时用这个看效果（连假 OBS，不弹窗，直接出图）
python scripts/render_ui_preview.py docs/ui-preview.png          # 演播室模式（深色）
python scripts/render_ui_preview.py docs/ui-preview-light.png --light   # 浅色主题
python scripts/render_ui_preview.py docs/ui-preview-single.png --no-studio

# 设置面板九个页面的对照图（不需要起 OBS，也不需要连假服务器）
python scripts/render_settings_preview.py docs
python scripts/render_settings_preview.py docs --light           # 浅色主题
python scripts/render_settings_preview.py docs --page 输出        # 只出某一页

# 打包产物的自检（build.bat 每次会自动跑；也可以手动验一个现成的 exe）
dist\OBSRemoteStudio-0.1.0-win64-single.exe --selftest
```

`tests/fake_obs_server.py` 是一个实现了 v5 握手/鉴权/事件推送的假 OBS（需 `websockets`），
  
覆盖连接、场景、来源显隐、录制/推流、事件回显、缩略图、转场、演播室模式、断线重连。

`tests/fake_obs_v4_server.py` 是**忠实模拟 obs-websocket v4** 的假服务器：连上**不主动发任何帧**、
没有 op 码、没有事件订阅、错误只有字符串、**没有电平表**、认证失败不断开连接 ——
v4 兼容层的每条翻译规则都靠它验证。

也可以只起假服务器，用真实 GUI 连它：

```bash
python tests/fake_obs_server.py        # v5，打印端口，密码 testpass
python tests/fake_obs_v4_server.py     # v4，打印端口，密码 testpass
```

## 已知限制


- OBS 不会把推流失败的具体原因经 obs-websocket 返回，只在状态栏显示状态枚举，详情需看 OBS 日志。
- 录制/推流时长优先用 OBS 返回的 `outputDuration`，缺失时回退到客户端计时（断线重连后可能不准）。
- 密码目前明文存放在 QSettings；要接 keyring 只需替换 `settings.py` 里的  
  `_read_password` / `_write_password`。
- 缩略图每帧都要 OBS 端完整编码一次。默认 480px 宽 / 质量 60 已经很轻，
    
  但演播室模式下是双画面，合计 2 帧/秒；若 OBS 端 CPU 吃紧，就把间隔调大。
- T型推杆仅在OBS 29.0.2版本及以下可以正常使用。
- **兼容模式（obs-websocket v4）下**：没有音频电平表（v4 完全没有这项能力，电平条会隐藏）、
  没有声道平衡、不能在客户端删除场景或调整场景顺序；虚拟摄像机、回放缓冲、
  媒体控制、场景集合/配置文件、推流字幕等取决于是 v4 的哪个版本
  （4.9.0 / 4.9.1 之后才陆续补齐），缺的会自动隐藏或置灰。
- **协议识别**默认自动（连上等约 2 秒：v5 会主动发握手帧、v4 一直静默）。
  若中间设备缓冲了首帧导致判错，可在「连接设置」里把协议手动钉死为 v5 或 v4。
- v4 的录制/推流时长由 `recordTimecode`/`stream-timecode` 反算（v4 不返回 duration/bytes）；
  推流丢帧与帧数在 v4 上取不到，状态栏对应项为 0。
