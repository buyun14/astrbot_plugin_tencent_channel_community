"""设备码扫码授权流程（``/txcm login``）。

走的是官方 CLI 同源的腾讯连接设备授权接口：申请设备码 → 展示二维码/授权链接 →
后台轮询授权结果 → 拿到 Token 后写回插件配置。

做成 mixin 的理由同 mcp_client：让 ``self._post_json`` 等依赖仍然来自插件类自身，
拆分不改变行为。混入方需要提供 ``self._cfg()`` / ``self._set_cfg()`` /
``self._save_config()`` / ``self._post_json()``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from typing import Any

from astrbot.api.event import AstrMessageEvent

from . import mcp_protocol
from .constants import POLL_DEVICE_TOKEN_OIDB, REQUEST_DEVICE_CODE_OIDB
from .errors import TencentChannelError


class DeviceLoginMixin:
    """腾讯频道设备码授权。"""

    async def _request_device_code(self) -> dict[str, Any]:
        url = str(self._cfg("device_code_request_url", "") or "").strip()
        if not url:
            raise TencentChannelError(
                "未配置设备码申请端点，请在插件配置中填写或使用 /txcm token 写入 Token。"
            )

        payload = mcp_protocol.parse_json_text(
            str(self._cfg("login_request_payload_json", "{}") or "{}"),
            fallback={},
        )
        if not isinstance(payload, dict):
            raise TencentChannelError("login_request_payload_json 必须是 JSON object。")
        device_id = str(payload.get("device_id") or "").strip()
        if device_id:
            try:
                uuid.UUID(device_id)
            except ValueError as exc:
                raise TencentChannelError("device_id 必须是合法 UUID。") from exc
        else:
            device_id = str(uuid.uuid4())
            payload["device_id"] = device_id

        data = await self._post_json(
            url,
            payload,
            {
                "Content-Type": "application/json",
                "X-Oidb": json.dumps(REQUEST_DEVICE_CODE_OIDB, separators=(",", ":")),
            },
        )
        result = self._unwrap_auth_gateway_response(data)
        if isinstance(result, dict):
            result.setdefault("device_id", device_id)
        return result

    async def _poll_device_token(
        self, device_code: str, device_id: str
    ) -> dict[str, Any]:
        url = str(self._cfg("device_token_poll_url", "") or "").strip()
        if not url:
            raise TencentChannelError("未配置设备码轮询端点。")
        payload = mcp_protocol.parse_json_text(
            str(self._cfg("login_poll_payload_json", "{}") or "{}"),
            fallback={},
        )
        if not isinstance(payload, dict):
            raise TencentChannelError("login_poll_payload_json 必须是 JSON object。")
        payload["device_code"] = device_code
        payload["device_id"] = device_id
        data = await self._post_json(
            url,
            payload,
            {
                "Content-Type": "application/json",
                "X-Oidb": json.dumps(POLL_DEVICE_TOKEN_OIDB, separators=(",", ":")),
            },
        )
        return self._unwrap_auth_gateway_response(data)

    def _unwrap_auth_gateway_response(self, data: Any) -> dict[str, Any]:
        """解包腾讯连接设备授权网关响应。

        Args:
            data: 上游返回的 JSON 对象。

        Returns:
            解包后的业务数据。

        Raises:
            TencentChannelError: 上游返回业务错误或结构异常。
        """
        if not isinstance(data, dict):
            raise TencentChannelError("腾讯频道登录端点返回格式异常。", data)

        retcode = data.get("retcode")
        if retcode not in (None, 0, "0"):
            message = data.get("message") or data.get("msg") or data.get("tipMsg")
            error = data.get("error")
            if not message and isinstance(error, dict):
                message = error.get("message")
            raise TencentChannelError(
                f"腾讯频道登录网关错误：retcode={retcode}，{message or '无详细信息'}",
                data,
            )

        payload = data.get("data", data)
        if isinstance(payload, str):
            payload = mcp_protocol.parse_json_text(payload, fallback={"value": payload})
        if not isinstance(payload, dict):
            raise TencentChannelError("腾讯频道登录端点 data 格式异常。", data)

        code = payload.get("code")
        if code not in (None, 0, "0"):
            message = payload.get("message") or payload.get("msg") or "无详细信息"
            raise TencentChannelError(
                f"腾讯频道登录业务错误：code={code}，{message}",
                data,
            )

        inner = payload.get("data", payload)
        if isinstance(inner, str):
            inner = mcp_protocol.parse_json_text(inner, fallback={"value": inner})
        if not isinstance(inner, dict):
            raise TencentChannelError("腾讯频道登录业务 data 格式异常。", data)
        return inner

    def _extract_login_token(self, data: dict[str, Any]) -> str:
        for key in (
            "qq_ai_connect_token",
            "QQ_AI_CONNECT_TOKEN",
            "access_token",
            "token",
            "session_key",
            "sessionKey",
        ):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        credentials = data.get("credentials")
        if isinstance(credentials, dict):
            return self._extract_login_token(credentials)
        return ""

    async def _login_poll_loop(
        self,
        event: AstrMessageEvent,
        device_code: str,
        device_id: str,
        interval: int,
        expires_in: int,
    ) -> None:
        deadline = time.time() + max(60, expires_in)
        while time.time() < deadline:
            await asyncio.sleep(max(1, interval))
            try:
                data = await self._poll_device_token(device_code, device_id)
            except TencentChannelError as exc:
                await event.send(event.plain_result(f"腾讯频道登录轮询失败：{exc}"))
                return

            status = str(data.get("status") or "").lower()
            next_interval = data.get("interval")
            if next_interval:
                with contextlib.suppress(TypeError, ValueError):
                    interval = int(next_interval)
            token = self._extract_login_token(data)
            if token:
                self._set_cfg("qq_ai_connect_token", token)
                self._save_config()
                await event.send(
                    event.plain_result("腾讯频道登录成功，Token 已写入插件配置。")
                )
                return
            if status in {"authorized", "success"}:
                await event.send(
                    event.plain_result("腾讯频道已授权，但轮询响应中没有 Token。")
                )
                return
            if status in {"1", "pending", "pending_authorization"}:
                continue
            if status in {"expired", "denied", "cancelled", "failed"}:
                await event.send(event.plain_result(f"腾讯频道登录失败：{status}"))
                return

        await event.send(
            event.plain_result("腾讯频道登录二维码已过期，请重新执行 /txcm login。")
        )
