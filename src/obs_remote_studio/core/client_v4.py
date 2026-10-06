"""obs-websocket v4 的网络客户端。

对上层（`obs_worker.py`）刻意**伪装成 obsws-python 的 ReqClient / EventClient**：
- `V4ReqClient.send(v5请求名, 字段)` 内部做 v5->v4 翻译（`protocol_v4.resolve`），
  再把 v4 应答翻回 v5 形状；
- 失败时抛的异常就是 `obsws_python.error.OBSSDKRequestError`（把 v4 的错误文案
  反推成 v5 的数字码塞进 `.code`），于是 `obs_worker._emit_request_failure`
  与 `controller._on_request_failed` 那套"204 自愈 / 506 纠正 / 604 置灰"
  一行都不用改就能在 v4 上生效。

三条 v4 特有的传输事实（都已对照 4.9.1 源码）：
1. **连上后服务端不发任何东西** —— 所以协议判定只能靠"等一会儿有没有收到 op 帧"。
2. **没有事件订阅** —— 所有事件无条件推给每条连接。这意味着**请求连接上也会
   收到事件帧**，因此 `send()` 收响应时必须按 `message-id` 过滤、把中间夹进来的
   事件帧丢掉（事件另有专用连接，不会丢信息）。照搬 v5 那种"发一条收一条"会错位。
3. **认证失败不关连接**，只是回一条 `Authentication Failed.` 错误帧。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid

import websocket
from obsws_python.error import OBSSDKError, OBSSDKRequestError, OBSSDKTimeoutError
from websocket import (
    WebSocketConnectionClosedException,
    WebSocketException,
    WebSocketTimeoutException,
)

from . import protocol_v4 as V4

logger = logging.getLogger(__name__)

# 协议判定时等第一帧的时间。v5 的 Hello 是在 onOpen 里同步发的（毫秒级到达），
# 2 秒已经非常宽松；而 v4 永远不会主动发，只能靠这个静默期反证。
PROBE_WAIT_S = 2.0

# 单条请求最多容忍多少个"别人的帧"（事件/过期响应）再放弃。
# 正常情况下事件连接才是事件的主要来源，这里只是兜底防饿死。
_MAX_SKIPPED_FRAMES = 500


def detect_protocol(host: str, port: int, timeout: float = 3.0) -> str:
    """连一下、等一会儿，判断对端是 v5 还是 v4。

    - 收到带 `op` 的帧 => v5（v5 的 Hello 就在 onOpen 里立刻发出）
    - 静默超时       => v4（v4 从不主动发任何东西）

    **探针自己开一条连接、判完就关**，再交给对应的正式客户端去连。
    这样就不必把"已经收到过 Hello 的 socket"硬塞给 obsws-python（它自己会在
    `ObsClient.__init__` 里收 Hello，塞过去反而要改库）。
    """
    ws = websocket.WebSocket()
    try:
        ws.connect(f"ws://{host}:{port}", timeout=timeout)
    except Exception:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        raise

    try:
        ws.settimeout(PROBE_WAIT_S)
        try:
            frame = ws.recv()
        except WebSocketTimeoutException:
            # 静默 —— v4 的典型特征
            return V4.MODE_COMPAT
        except (WebSocketConnectionClosedException, OSError):
            # 连上就被关：更像 v5 在握手前把不合规连接踢掉，按 v5 试
            return V4.MODE_STANDARD

        try:
            data = json.loads(frame)
        except (TypeError, ValueError):
            return V4.MODE_STANDARD
        if isinstance(data, dict) and "op" in data:
            return V4.MODE_STANDARD
        # 收到了帧却没有 op：v4 不会主动发帧，这种情况按 v5 处理更安全
        logger.warning("协议判定：收到不带 op 的主动帧，按 v5 处理（%r）", frame)
        return V4.MODE_STANDARD
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def expected_auth(password: str, salt: str, challenge: str) -> str:
    """v4 认证应答。算法与 v5 完全一致（已对照 Config::CheckAuth）。"""
    import base64
    import hashlib

    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest())
    return base64.b64encode(
        hashlib.sha256(secret + challenge.encode()).digest()
    ).decode()


class V4Version:
    """`get_version()` 的返回值，字段名对齐 obsws-python 的 v5 版本对象。

    `available_requests` 放的是**合成后的 v5 请求名**（见 protocol_v4.synthesize_capabilities），
    这样 worker 把同一份载荷交给 `controller._on_connected` 时能力探测无需分支。
    """

    def __init__(self, normalized: dict) -> None:
        self.protocol = normalized["protocol"]
        self.obs_version = normalized["obs_version"]
        # 注意名字：obsws-python 的 v5 dataclass 把 obsWebSocketVersion 转成
        # obs_web_socket_version，worker 就是按这个读的
        self.obs_web_socket_version = normalized["websocket_version"]
        self.rpc_version = normalized["rpc_version"]
        self.available_requests = list(normalized["available_requests"])
        self.v4_requests = list(normalized.get("v4_requests") or [])


class _V4Socket:
    """一条 v4 websocket 的公共部分：连上 + 认证。

    v4 的请求与事件都在同一条连接上（没有订阅机制），所以请求客户端也要能
    识别并跳过事件帧 —— 见 `V4ReqClient.send`。
    """

    def __init__(self, host: str, port: int, password: str, timeout: float) -> None:
        self.host = host
        self.port = port
        self.password = password or ""
        self.timeout = float(timeout)
        self._lock = threading.Lock()
        self._closed = False
        self.authenticated = False
        self.v4_requests: set[str] = set()
        self.ws = websocket.WebSocket()
        self.ws.connect(f"ws://{host}:{port}", timeout=self.timeout)
        self.ws.settimeout(self.timeout)
        try:
            self._handshake()
        except Exception:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            raise

    # ---------------------------------------------------------------- 握手
    def _handshake(self) -> None:
        data = self._exchange("GetAuthRequired", None)
        if data.get("authRequired"):
            if not self.password:
                # 与 obsws-python 同样的口径：让 obs_worker._classify 归到"认证失败"
                raise OBSSDKError("authentication enabled but no password provided")
            auth = expected_auth(
                self.password,
                str(data.get("salt", "") or ""),
                str(data.get("challenge", "") or ""),
            )
            try:
                self._exchange("Authenticate", {"auth": auth})
            except OBSSDKRequestError as exc:
                # v4 认证失败**不关连接**，是我们主动把它当成连接失败
                raise OBSSDKError(f"authentication failed: {exc}") from exc
            self.authenticated = True
        else:
            self.authenticated = True

        # 顺手拿一次能力清单：v4 的 available-requests 是逗号分隔字符串
        self._read_version()

    def _read_version(self) -> None:
        data = self._exchange("GetVersion", None)
        raw = str(data.get("available-requests", "") or "")
        self.v4_requests = {item.strip() for item in raw.split(",") if item.strip()}
        self._version_fields = data

    # ---------------------------------------------------------------- 收发
    def _exchange(self, request_type: str, fields: dict | None) -> dict:
        """发一条 v4 请求并等它的响应（按 message-id 关联，事件帧会被跳过）。"""
        if self._closed:
            raise OBSSDKError("v4 connection is closed")
        message_id = uuid.uuid4().hex
        payload: dict = {"request-type": request_type, "message-id": message_id}
        if fields:
            payload.update(fields)

        with self._lock:
            try:
                self.ws.send(json.dumps(payload))
            except (WebSocketConnectionClosedException, OSError) as exc:
                raise OBSSDKError(f"发送失败：{exc}") from exc

            skipped = 0
            while True:
                try:
                    raw = self.ws.recv()
                except WebSocketTimeoutException as exc:
                    raise OBSSDKTimeoutError(
                        f"等待 {request_type} 的响应超时"
                    ) from exc
                if not raw:
                    # 连接被拆掉时 recv 可能拿到空帧，按 JSON 错误处理（worker 已有该分支）
                    raise json.JSONDecodeError("empty frame", "", 0)
                try:
                    frame = json.loads(raw)
                except (TypeError, ValueError) as exc:
                    raise json.JSONDecodeError(str(exc), str(raw), 0) from exc
                if not isinstance(frame, dict):
                    continue

                # v4 把事件也推给这条连接，而事件没有 message-id —— 跳过即可
                if frame.get("message-id") != message_id:
                    skipped += 1
                    if skipped > _MAX_SKIPPED_FRAMES:
                        raise OBSSDKTimeoutError(
                            f"等待 {request_type} 的响应时跳过了过多无关帧"
                        )
                    logger.debug("跳过无关帧（等待 %s）：%s", request_type, frame)
                    continue

                if str(frame.get("status", "")) == "error":
                    message = str(frame.get("error", "") or "未知错误")
                    if message.strip().lower() == "invalid request type":
                        # 请求名在 v4 上不存在 —— 用 V4Unsupported 表达，
                        # 让解析器有机会退回老请求名（例如 GetSceneItemList -> GetSceneList）
                        raise V4.V4Unsupported(
                            f"{request_type} 在当前 v4 服务端上不存在", V4.ERR_INVALID_REQUEST
                        )
                    raise V4.V4Error(message, V4.error_code(message))
                return frame

    def disconnect(self) -> None:
        self._closed = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 v4 连接时出错", exc_info=True)


class V4ReqClient(_V4Socket):
    """伪装成 obsws-python 的 ReqClient。"""

    def __init__(self, host: str, port: int, password: str = "", timeout: float = 3.0,
                 subs: int = 0) -> None:
        # subs 只为了签名兼容（v4 没有订阅机制），刻意忽略
        super().__init__(host, port, password, timeout)

    def get_version(self) -> V4Version:
        capabilities = V4.synthesize_capabilities(self.v4_requests)
        normalized = V4.normalize_version(self._version_fields, capabilities)
        return V4Version(normalized)

    def raw_call(self, v4_request: str, fields: dict | None):
        """给 protocol_v4 的解析器用的回调（发真·v4 请求，拿真·v4 应答）。"""
        if v4_request not in self.v4_requests:
            # 服务端没上报这个请求名 = 能力不存在。抛 V4Unsupported（而不是直接
            # 抛 OBSSDKRequestError）是**必须的**：解析器靠它做名字降级，
            # 例如老 v4（<4.9）没有 GetSceneItemList 时要退回 GetSceneList。
            raise V4.V4Unsupported(f"{v4_request} 不在服务端能力列表里")
        return self._exchange(v4_request, fields)

    def send(self, param: str, data: dict | None = None, raw: bool = False):
        """v5 请求名 -> 执行 -> v5 形状的应答对象（无返回数据则 None）。"""
        try:
            result = V4.resolve(param, data or {}, self.raw_call)
        except V4.V4Unsupported as exc:
            # 这条 v5 请求在 v4 上没有对应物。统一成 v5 的 204 语义，让
            # controller 的"名字不对→自愈/降级"分支照常工作。
            raise OBSSDKRequestError(param, V4.ERR_INVALID_REQUEST, str(exc)) from exc
        except V4.V4Error as exc:
            raise OBSSDKRequestError(
                param, exc.code or V4.error_code(exc.message, param), exc.message
            ) from exc
        if raw or result is None:
            return result
        return V4.as_object(result)


class V4Callback:
    """对齐 obsws-python 的 `Callback`（按 `on_<snake_case>` 找处理器）。"""

    def __init__(self) -> None:
        self._callbacks: list = []

    def register(self, fns) -> None:
        try:
            iterator = iter(fns)
        except TypeError:
            iterator = iter([fns])
        for fn in iterator:
            if fn not in self._callbacks:
                self._callbacks.append(fn)

    def deregister(self, fns) -> None:
        try:
            iterator = iter(fns)
        except TypeError:
            iterator = iter([fns])
        for fn in iterator:
            if fn in self._callbacks:
                self._callbacks.remove(fn)

    def clear(self) -> None:
        self._callbacks.clear()

    def trigger(self, v5_type: str, payload) -> None:
        wanted = f"on_{V4.to_snake(v5_type)}"
        for fn in list(self._callbacks):
            if getattr(fn, "__name__", "") == wanted:
                fn(payload)


class V4EventClient(_V4Socket):
    """伪装成 obsws-python 的 EventClient：开一条连接专收事件。"""

    def __init__(self, host: str, port: int, password: str = "", timeout: float = 3.0,
                 subs: int = 0) -> None:
        super().__init__(host, port, password, timeout)
        self.callback = V4Callback()
        self._stop = threading.Event()
        # 事件连接要长期阻塞地 recv，不能带着请求用的短超时
        self.ws.settimeout(None)
        self.worker = threading.Thread(target=self._listen, daemon=True)
        self.worker.start()

    def _listen(self) -> None:
        """持续收事件。v4 的帧全是平铺 JSON，靠 `update-type` 认出事件。"""
        while not self._stop.is_set():
            try:
                raw = self.ws.recv()
            except (WebSocketConnectionClosedException, OSError):
                logger.debug("v4 事件连接已关闭，监听线程退出")
                return
            except Exception:  # noqa: BLE001
                logger.debug("v4 事件连接异常，监听线程退出", exc_info=True)
                return
            if not raw:
                continue
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            update_type = frame.get("update-type")
            if not update_type:
                # 这条连接偶尔也会收到请求响应，忽略
                continue
            try:
                normalized = V4.normalize_event(str(update_type), frame)
            except Exception:  # noqa: BLE001
                logger.exception("翻译 v4 事件 %s 时出错", update_type)
                continue
            if normalized is None:
                continue
            v5_type, payload = normalized
            try:
                self.callback.trigger(v5_type, V4.as_object(payload))
            except Exception:  # noqa: BLE001
                logger.exception("派发 v4 事件 %s 时出错", update_type)

    def unsubscribe(self) -> None:
        self.disconnect()

    def disconnect(self) -> None:
        self._stop.set()
        self._closed = True
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 v4 事件连接时出错", exc_info=True)


__all__ = [
    "PROBE_WAIT_S",
    "V4Callback",
    "V4EventClient",
    "V4ReqClient",
    "V4Version",
    "detect_protocol",
    "expected_auth",
]
