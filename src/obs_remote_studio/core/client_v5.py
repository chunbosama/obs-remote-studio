"""v5 请求通道：给 obsws-python 补上**响应与请求的配对校验**。

为什么必须有这一层
------------------
`obsws_python.ObsClient.req()` 是"发一条、收一帧"，**从不校验响应里的
`requestId`**（源码 `obsws_python/baseclient.py`）。只要有一条请求超时，
它那条迟到的响应就会留在 socket 里，被**下一次**请求当成自己的响应收下 ——
从此这条连接上的每一条响应都错位一格，而且**不会自己恢复**。

错位一格对单发请求通常只是"这次操作看着没生效"，对**每秒都在发**的缩略图
请求却是灾难：客户端为"预览"和"输出"各要一张图，回来的两张各差一格，
于是输出窗显示的是预览场景、预览窗显示的是输出场景，并且会一直错下去。
现场报的正是这个现象：「单点场景后输出窗切到了预览场景、预览窗无变化」。

这里只做一件事：每条请求带一个唯一 `requestId`，收帧时**跳过所有对不上的帧**
（迟到的旧响应），只认自己那一条。于是"响应错位"在源头就不成立了，
上层"按请求顺序关联"的老代码（缩略图、逐源音频、逐项可见性）重新变得可靠。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Callable

from obsws_python import ReqClient
from obsws_python.baseclient import ObsClient
from obsws_python.error import OBSSDKTimeoutError
from websocket import WebSocketTimeoutException

logger = logging.getLogger(__name__)

# obs-websocket 的帧类型（只关心这两种）
OP_REQUEST = 6
OP_RESPONSE = 7

# 服务端没给 timeout 时的兜底预算（秒）。正常连接一定会带上 worker 的 3s。
DEFAULT_TIMEOUT = 10.0


class VerifiedObsClient(ObsClient):
    """把 `ObsClient.req()` 换成"发一条、只认自己那条响应"的版本。

    对外的契约与父类完全一致：参数一样、返回的 `d` 字典一样、抛的异常一样，
    所以 `ReqClient.send()` 那一整套现成逻辑（dataclass 转换、错误码包装）
    一行都不用改。
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        #: 累计丢弃的迟到响应条数（诊断窗口 / 测试用）
        self.stale_frames: int = 0
        #: 丢弃迟到响应时的回调，参数是那条被丢掉的响应帧
        self.on_stale: Callable[[dict], None] | None = None

    # ---------------------------------------------------------------- 请求
    def req(self, req_type, req_data=None):
        request_id = uuid.uuid4().hex
        payload: dict = {"op": OP_REQUEST, "d": {"requestType": req_type, "requestId": request_id}}
        if req_data:
            payload["d"]["requestData"] = req_data

        budget = self._budget()
        deadline = time.monotonic() + budget
        try:
            self.ws.send(json.dumps(payload))
        except WebSocketTimeoutException as exc:
            raise OBSSDKTimeoutError("Timeout while trying to send the request") from exc

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OBSSDKTimeoutError(
                    f"Timeout while waiting for response to {req_type}（{budget:.1f}s）"
                )
            # json 解析失败（空帧 / 半帧）与连接关闭都原样抛出，
            # 由 worker 按"连接级故障"处理，跟父类的行为一致。
            frame = json.loads(self._recv(remaining))
            if frame.get("op") != OP_RESPONSE:
                # 请求通道订阅位是 0，正常不会有事件帧。真收到了就跳过，
                # 不能让一条事件把响应流顶掉一格。
                continue
            data = frame.get("d") or {}
            if data.get("requestId") != request_id:
                self._note_stale(data)
                continue
            return data

    # ---------------------------------------------------------------- 内部
    def _budget(self) -> float:
        return float(self.timeout or DEFAULT_TIMEOUT)

    def _recv(self, remaining: float) -> str:
        """按剩余预算收一帧。

        websocket-client 的 `recv()` 用的是 socket 自己的超时，
        所以每次收之前把剩余预算写回去 —— 否则超时后剩下的时间会
        白白耗在一条已死的连接上。
        """
        try:
            self.ws.settimeout(remaining)
        except (AttributeError, OSError):
            # socket 已关闭 / 底层实现不支持 settimeout：交给 recv 自己报错
            pass
        try:
            return self.ws.recv()
        except WebSocketTimeoutException as exc:
            raise OBSSDKTimeoutError("Timeout while waiting for response") from exc

    def _note_stale(self, data: dict) -> None:
        """记一条"被丢弃的迟到响应"。

        这条路径**必须说出来**：它正是"画面搞反"的成因。诊断窗口里如果只看到
        "请求发了、响应对不上/没回"，排障的人只会以为 OBS 没回。
        """
        self.stale_frames += 1
        logger.warning(
            "丢弃迟到的响应：requestType=%s requestId=%s（它属于上一条已失败的请求，"
            "若被当成下一条请求的响应，整条连接的响应就会集体错位一格）",
            data.get("requestType", "?"),
            data.get("requestId", "?"),
        )
        if self.on_stale is None:
            return
        try:
            self.on_stale(data)
        except Exception:  # noqa: BLE001
            logger.debug("on_stale 回调出错", exc_info=True)


class VerifiedReqClient(ReqClient):
    """`ReqClient` 的加固版：底层换成 `VerifiedObsClient`，其余行为一模一样。

    之所以自己拼一遍 `__init__`：`obsws_python.reqs.ReqClient.__init__`
    里把 `ObsClient` 写死了，没法从外面替换。这里只替换那一行，
    `send()` / `disconnect()` / 便捷方法全部沿用父类。
    """

    def __init__(self, **kwargs) -> None:
        self.logger = logger.getChild(type(self).__name__)
        self.base_client = VerifiedObsClient(**kwargs)
        try:
            success = self.base_client.authenticate()
        except Exception as exc:  # noqa: BLE001 - 与父类一致：认证失败原样抛给上层分类
            self.logger.error("%s: %s", type(exc).__name__, exc)
            raise
        self.logger.info(
            "Successfully identified with the server using RPC version:%s",
            (success or {}).get("negotiatedRpcVersion"),
        )

    @property
    def stale_frames(self) -> int:
        """累计丢弃的迟到响应条数。"""
        return int(getattr(self.base_client, "stale_frames", 0))

    @property
    def on_stale(self) -> Callable[[dict], None] | None:
        return getattr(self.base_client, "on_stale", None)

    @on_stale.setter
    def on_stale(self, callback: Callable[[dict], None] | None) -> None:
        self.base_client.on_stale = callback


__all__ = ["VerifiedObsClient", "VerifiedReqClient"]
