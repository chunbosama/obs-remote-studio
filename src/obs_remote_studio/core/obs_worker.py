"""网络工作线程：所有 obs-websocket 通信都发生在这里，UI 线程零网络 IO。

用法：
    thread = QThread()
    worker = ObsWorker()
    worker.moveToThread(thread)
    thread.start()
    worker.request_connect.emit(cfg_dict)   # 跨线程调用槽函数
"""

from __future__ import annotations

import json
import logging

from PySide6.QtCore import QObject, Signal, Slot
from obsws_python import EventClient, ReqClient
from obsws_python.error import OBSSDKError, OBSSDKRequestError, OBSSDKTimeoutError
from websocket import (
    WebSocketConnectionClosedException,
    WebSocketException,
    WebSocketTimeoutException,
)

from . import protocol as P

logger = logging.getLogger(__name__)

# 连接阶段的失败分类，便于上层给出可操作的提示
FAIL_AUTH = "auth"
FAIL_TIMEOUT = "timeout"
FAIL_REFUSED = "refused"
FAIL_UNKNOWN = "unknown"


def _to_plain(value):
    """把 obsws-python 的响应对象拍成普通 dict，供诊断窗口 JSON 化。

    响应对象是动态挂属性的，没有 __dict__ 走不通的话就退回 str()，
    宁可显示得糙一点，也不能让诊断窗口把主链路带崩。
    """
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        try:
            return json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError):
            return str(value)
    data = getattr(value, "response_data", None)
    if isinstance(data, dict):
        try:
            return json.loads(json.dumps(data, default=str))
        except (TypeError, ValueError):
            return str(data)
    return str(value)


class ObsWorker(QObject):
    # ---- 发往主线程 ----
    connected = Signal(dict)  # {"obs_version", "websocket_version", "rpc_version"}
    connect_failed = Signal(str, str)  # kind, message
    disconnected = Signal(str)  # reason
    event_link_failed = Signal(str)  # 事件通道建立失败（不影响请求）
    retry_event_link = Signal()      # 重新尝试建立事件通道
    event_link_ready = Signal()      # 事件通道已就绪（首次或重试成功）
    event_received = Signal(str, object)  # eventType, payload
    result_ready = Signal(str, object)  # requestType, response（无返回值的请求为 None）
    # requestType, message, transport_lost, error_code
    # error_code 为 obs-websocket 的 requestStatus.code（成功之外的失败才带），
    # 0 表示不是协议级错误（连接中断、超时、本地异常等）。
    # 上层需要靠它区分「请求名不对(204)」和「请求合法但资源当前不可用(604)」——
    # 只看 comment 文本判不出来，而且 604 属于正常业务状态，不该弹框。
    request_failed = Signal(str, str, bool, int)
    # J3：诊断窗口用的原始 JSON 旁路。
    # (direction, kind, payload) -> direction: "<-" 收到 / "->" 发出；
    # kind: "request" / "response" / "event" / "error"；payload 是已经可读的 dict/str。
    # 与业务信号并行发送，诊断窗口关闭时没人接收，开销只是几次 emit。
    raw_trace = Signal(str, str, object)

    # ---- 主线程 -> 工作线程 ----
    request_connect = Signal(dict)
    request_disconnect = Signal()
    request_execute = Signal(str, dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._req: ReqClient | None = None
        self._events: EventClient | None = None
        self._timeout: float = 3.0
        self._subscriptions: int = P.SUBSCRIPTION_MASK
        # 会话号：每次 _on_connect 递增。请求在发出时记下自己的会话号，
        # 回来时若已不是当前会话，说明这条结果属于上一条连接，一律作废。
        # 与 _on_connect/_on_execute 都跑在 worker 线程、天然串行配合，不需要加锁。
        self._session: int = 0

        self.request_connect.connect(self._on_connect)
        self.request_disconnect.connect(self._on_disconnect)
        self.request_execute.connect(self._on_execute)
        # 事件通道重建必须接上：controller 在事件通道建失败后会带退避 emit 这个信号
        # （见 controller._schedule_event_retry）。此前**漏了这一行连接**，
        # 于是整条重试链路是死的 —— 事件通道一旦建不上就再也收不到事件，
        # 而界面还会告诉用户"正在自动重试"。
        self.retry_event_link.connect(self._on_retry_event_link)

    # ---------------------------------------------------------------- 连接
    @Slot(dict)
    def _on_connect(self, cfg: dict) -> None:
        host = str(cfg.get("host", "127.0.0.1"))
        port = int(cfg.get("port", 4455))
        password = str(cfg.get("password", "") or "")
        self._timeout = float(cfg.get("timeout", 3.0))
        self._subscriptions = int(cfg.get("subscription_mask", P.SUBSCRIPTION_MASK))
        # 事件通道是**独立的第二条 websocket**，可能单独建失败。留住参数以便重试。
        self._event_target = (host, port, password)

        # 重复连接（例如换一台 OBS）时先关掉旧连接，避免 socket 泄漏、
        # 以及旧事件通道继续往这边推事件
        self._safe_close()
        self._session += 1

        try:
            # 请求连接必须 subs=0：一旦订阅事件，事件帧会混进请求/响应流，
            # obsws-python 的 req() 会把事件当成响应解析。事件一律走 EventClient。
            self._req = ReqClient(
                host=host,
                port=port,
                password=password,
                subs=0,
                timeout=self._timeout,
            )
        except OBSSDKError as exc:
            self.connect_failed.emit(self._classify(exc, password), str(exc))
            return
        except (ConnectionRefusedError, TimeoutError, WebSocketTimeoutException) as exc:
            self.connect_failed.emit(FAIL_REFUSED, str(exc) or "无法连接到 OBS")
            return
        except (WebSocketException, OSError) as exc:
            self.connect_failed.emit(self._classify(exc, password), str(exc))
            return

        try:
            version = self._req.get_version()
        except Exception as exc:  # noqa: BLE001 - 连接已建立但拿不到版本，一并归为失败
            self._safe_close()
            self.connect_failed.emit(self._classify(exc, password), str(exc))
            return

        # 先把 GetVersion 的结果交给 controller（它会据此记录 availableRequests 做能力探测），
        # 再发 connected —— 顺序不能反，否则首次刷新会按“未知能力”乱发请求。
        self.result_ready.emit(P.REQ_GET_VERSION, version)
        self.connected.emit(
            {
                "obs_version": getattr(version, "obs_version", ""),
                "websocket_version": getattr(version, "obs_web_socket_version", ""),
                "rpc_version": getattr(version, "rpc_version", 0),
                # availableRequests 是能力探测的唯一依据：
                # 有没有 GetSceneTransitionList 之类的差异全靠它，别靠猜。
                "available_requests": list(getattr(version, "available_requests", []) or []),
            }
        )
        self._start_event_client(host, port, password)

    def _start_event_client(self, host: str, port: int, password: str) -> None:
        try:
            self._events = EventClient(
                host=host,
                port=port,
                password=password,
                subs=self._subscriptions,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._events = None
            self.event_link_failed.emit(str(exc))
            return

        handlers = [self._make_callback(evt, cb) for evt, cb in P.EVENTS.items()]
        self._events.callback.register(handlers)
        self.event_link_ready.emit()

    @Slot()
    def _on_retry_event_link(self) -> None:
        """重建事件通道。

        事件通道建失败后此前是**一锤子买卖**：建不上就再也没事件了，
        场景变化、录制状态这些全靠事件的显示项会一直不动 —— 对直播控制来说不能接受。
        控制通道还在就允许重试。
        """
        if self._req is None:
            return  # 控制通道都没了，重试没有意义
        target = getattr(self, "_event_target", None)
        if not target:
            return
        try:
            self._events = None
        except Exception:  # noqa: BLE001
            pass
        self._start_event_client(*target)

    def _make_callback(self, event_type: str, callback_name: str):
        def handler(data, _type=event_type):
            # 该回调运行在 EventClient 自己的线程里，Qt 会自动排队到主线程
            self.raw_trace.emit("<-", "event", {"eventType": _type, "eventData": _to_plain(data)})
            self.event_received.emit(_type, data)

        handler.__name__ = callback_name
        return handler

    # ---------------------------------------------------------------- 断开
    @Slot()
    def _on_disconnect(self) -> None:
        self._safe_close()
        self.disconnected.emit("已手动断开")

    # ---------------------------------------------------------------- 请求
    @Slot(str, dict)
    def _on_execute(self, request_type: str, request_data: dict) -> None:
        if self._req is None:
            self.request_failed.emit(request_type, "未连接到 OBS", True, 0)
            return

        # 记住本次用的是哪个 ReqClient 与哪个会话。换连接（重连/切 OBS）时会 `_safe_close()`
        # + 新建 client，此刻**旧 socket 上正在飞的请求**仍会返回失败 ——
        # 那条失败属于上一条连接，绝不能算到新连接头上（否则会误判掉线、触发多余重连，
        # 表现为"刚连上就跑两遍 refresh_all"）。
        client = self._req
        session = self._session

        # 直接用 send(requestType, requestData)：
        # obsws-python 生成的便捷方法参数名与协议字段并不总是一致
        # （例如 get_scene_item_list(name) 对应协议字段 sceneName），走 send 更稳。
        self.raw_trace.emit("->", "request", {"requestType": request_type, "requestData": request_data})
        try:
            response = client.send(request_type, request_data or None)
        except (OBSSDKRequestError, OBSSDKTimeoutError,
                WebSocketConnectionClosedException, WebSocketException, OSError,
                json.JSONDecodeError) as exc:
            if client is not self._req or session != self._session:
                # 连接已被替换/关闭：这条失败是上一轮的残响，直接丢弃
                logger.debug("丢弃旧连接上的失败结果：%s（%s）", request_type, exc)
                self._trace_dropped(request_type, "连接已更换，该失败属于上一条连接，已丢弃")
                return
            self._emit_request_failure(request_type, exc)
            return
        except Exception as exc:  # noqa: BLE001
            if client is not self._req or session != self._session:
                logger.debug("丢弃旧连接上的异常结果：%s（%s）", request_type, exc)
                self._trace_dropped(request_type, "连接已更换，该异常属于上一条连接，已丢弃")
                return
            self.raw_trace.emit("<-", "error", {"requestType": request_type, "error": str(exc)})
            self.request_failed.emit(request_type, str(exc), False, 0)
            return

        if client is not self._req or session != self._session:
            # 响应迟到且连接已换人：结果同样不能用
            logger.debug("丢弃旧连接上的迟到响应：%s", request_type)
            self._trace_dropped(request_type, "连接已更换，迟到的响应已丢弃")
            return
        self.raw_trace.emit("<-", "response", {"requestType": request_type, "response": _to_plain(response)})
        self.result_ready.emit(request_type, response)

    def _trace_dropped(self, request_type: str, reason: str) -> None:
        """记一条"结果被丢弃"的帧。

        这条路径原来是**完全静默**的（只写 debug 日志），于是诊断窗口里就会出现
        "有请求帧、之后再没有任何帧"的现象 —— 排障的人只能推断成"OBS 没回响应"，
        而真相是响应属于上一条连接、被我们主动丢了。
        诊断工具的职责是把这类事也说出来。
        """
        self.raw_trace.emit(
            "<-", "error",
            {"requestType": request_type, "dropped": True, "reason": reason},
        )

    def _emit_request_failure(self, request_type: str, exc: BaseException) -> None:
        """按异常类型归好类，再把失败抛给主线程。"""
        if isinstance(exc, OBSSDKRequestError):
            # obs-websocket 的 requestStatus.code 在异常里是 .code；
            # comment 只是给人看的文案（"Replay buffer is not available"），
            # 靠文案分支判断太脆，所以把数字一并传上去。
            code = int(getattr(exc, "code", 0) or 0)
            comment = getattr(exc, "comment", "") or str(exc)
            # **还原成协议原样的响应帧**（op 7 的 requestStatus 形状）。
            # 之前这里记的是自定义的 {requestType, code, comment}，而诊断窗口的用途
            # 正是"看线上到底回了什么"——用户按 requestStatus.code 去找根本找不到，
            # 反而会以为"OBS 压根没回响应"。服务端明明回了，是客户端把它转手改了样。
            self.raw_trace.emit(
                "<-",
                "response",
                {
                    "requestType": request_type,
                    "requestStatus": {
                        "result": False,
                        "code": code,
                        "comment": comment,
                    },
                },
            )
            self.request_failed.emit(request_type, comment, False, code)
            return
        if isinstance(exc, OBSSDKTimeoutError):
            self.raw_trace.emit("<-", "error", {"requestType": request_type, "error": str(exc)})
            self.request_failed.emit(request_type, str(exc), True, 0)
            return
        if isinstance(exc, json.JSONDecodeError):
            # 连接正在拆除时 recv() 会拿到空帧，obsws-python 的 json.loads 就抛这个。
            # 它是**连接级**问题，不是"这次操作失败"，按断线处理才不会弹框误导用户。
            self.raw_trace.emit(
                "<-", "error", {"requestType": request_type, "error": f"响应不是合法 JSON：{exc}"}
            )
            self.request_failed.emit(request_type, "响应不是合法 JSON（连接可能已中断）", True, 0)
            return
        self.raw_trace.emit("<-", "error", {"requestType": request_type, "error": str(exc)})
        self.request_failed.emit(request_type, str(exc) or "连接已中断", True, 0)

    # ---------------------------------------------------------------- 内部
    def _safe_close(self) -> None:
        for client in (self._req, self._events):
            if client is None:
                continue
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("关闭连接时出错", exc_info=True)
        self._req = None
        self._events = None

    @staticmethod
    def _classify(exc: BaseException, password: str) -> str:
        text = str(exc).lower()
        if isinstance(exc, (TimeoutError, WebSocketTimeoutException)) or "timed out" in text:
            return FAIL_TIMEOUT
        if isinstance(exc, ConnectionRefusedError) or "refused" in text:
            return FAIL_REFUSED
        if "identify" in text or "authentication" in text or not password:
            return FAIL_AUTH
        return FAIL_UNKNOWN
