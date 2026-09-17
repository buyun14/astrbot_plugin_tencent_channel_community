"""MCP 网关交互层：HTTP 请求、JSON-RPC 握手与重试、结果解析、限速与缓存。

拆成 mixin 而不是独立客户端对象，是为了让 ``self._post_json`` / ``self._mcp_request``
等方法仍然挂在插件类上 —— 已有回归测试直接替换 ``_post_json`` 打桩，这样的拆分
行为零变化。混入方（插件类）需要提供：

- ``self.config`` 以及 ``_cfg()``（配置读取，定义在插件类里）
- ``self._session`` / ``self._server_info`` / ``self._tool_cache``
- ``self._throttle_lock`` / ``self._last_call_at`` / ``self._data_cache``

失败分类（哪些错误值得重试）与凭据脱敏都放在 mcp_protocol，便于与 ``_post_json``
抛出的中文文案对齐并单测。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import aiohttp
from astrbot.api import logger

from . import mcp_protocol
from .constants import (
    DEFAULT_MCP_ENDPOINT,
    HIGH_RISK_TOOLS,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SKILL_UPDATE_CHECK_URL,
    SKILL_VERSION,
    WRITE_TOOLS,
)
from .errors import TencentChannelError

MCP_PROTOCOL_VERSION = "2024-11-05"
# 网关对凭证位置的要求与方法相关（实测）：
#   initialize / tools/list / notifications/initialized → 只认 URL query 上的 token，
#       附加 Authorization 头会返回 8011 "api info not exist"；
#   tools/call → 必须附加 Authorization 头，否则 oidb 层报 151 "登录态验证失败"。
MCP_MAX_ATTEMPTS = 3
MCP_RETRY_BACKOFF_SECONDS = 1.5

_RATE_LIMIT_MARKERS = (
    "请求频率过高",
    "频率限制",
    "频率受限",
    "接口调用已超过申请的频率上限",
    "too many requests",
    "rate limit",
    "rate-limited",
)


def is_rate_limit_payload(parsed: Any) -> bool:
    """判断 MCP tool 错误是否为 retCode 153 限流，尽量减少误判。"""
    if isinstance(parsed, dict):
        if str(parsed.get("code") or "") == "153":
            return True
        message = parsed.get("message")
        if isinstance(message, dict):
            text = json.dumps(message, ensure_ascii=False)
        else:
            text = str(message or parsed or "")
    else:
        text = str(parsed or "")
    lower = text.lower()
    return any(marker in lower for marker in _RATE_LIMIT_MARKERS)


class McpClientMixin:
    """腾讯频道 MCP 网关的请求、握手、重试与数据缓存。"""

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(trust_env=True)
        return self._session

    def _timeout(self) -> aiohttp.ClientTimeout:
        try:
            seconds = int(self._cfg("request_timeout_seconds", 30))
        except (TypeError, ValueError):
            seconds = 30
        return aiohttp.ClientTimeout(total=max(5, min(seconds, 180)))

    def _token(self) -> str:
        token = str(self._cfg("qq_ai_connect_token", "") or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token

    async def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        session = await self._get_session()
        proxy = str(self._cfg("proxy", "") or "").strip() or None
        token = self._token()
        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=proxy,
                timeout=self._timeout(),
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    if response.status in {401, 403}:
                        message = "腾讯频道鉴权失败，请检查 QQ AI Connect Token 或重新 /txcm login。"
                    elif response.status == 429:
                        message = "腾讯频道接口触发频率限制，请稍后再试。"
                    else:
                        message = f"腾讯频道端点返回 HTTP {response.status}。"
                    raise TencentChannelError(
                        mcp_protocol.redact_token(
                            f"{message} 响应片段：{text[:300]}", token
                        ),
                    )
        except TencentChannelError:
            raise
        except TimeoutError as exc:
            raise TencentChannelError("请求腾讯频道端点超时。") from exc
        except aiohttp.ClientError as exc:
            # aiohttp 异常文案可能带完整 URL（内含 token），必须先脱敏再往外抛
            raise TencentChannelError(
                mcp_protocol.redact_token(f"请求腾讯频道端点失败: {exc}", token)
            ) from exc
        except Exception as exc:  # 非 HTTP 异常同样脱敏后抛出，避免 token 落盘
            raise TencentChannelError(
                mcp_protocol.redact_token(
                    f"请求腾讯频道端点异常: {type(exc).__name__}: {exc}", token
                )
            ) from exc

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TencentChannelError("腾讯频道端点返回了非 JSON 响应。") from exc
        return data

    def _cache_ttl(self) -> float:
        """数据集缓存有效期（秒），<=0 表示不缓存。"""
        try:
            return max(0.0, float(self._cfg("cache_ttl_seconds", 300) or 0))
        except (TypeError, ValueError):
            return 300.0

    def _cache_get(self, key: str) -> Any:
        """读缓存；过期或**空结果**都视为未命中。

        空结果（`[]` / `{}`）不参与缓存，否则一次偶发的空响应会把频道列表
        或版块列表固定成"没有数据"整整一个 TTL（默认 300 秒），
        导致 5 个语义化工具全部不可用。
        """
        if self._cache_ttl() <= 0:
            return None
        entry = self._data_cache.get(key)
        if not entry:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self._cache_ttl():
            self._data_cache.pop(key, None)
            return None
        if not value:
            return None
        return value

    def _cache_set(self, key: str, value: Any) -> None:
        """写缓存（TTL 为 0 时跳过）。"""
        if self._cache_ttl() > 0:
            self._data_cache[key] = (time.monotonic(), value)

    async def _throttle(self) -> None:
        """串行化并发调用并保证最小请求间隔。

        网关有明显频率限制（超过后返回"接口调用已超过申请的频率上限"），
        并发调用很容易踩中，这里加一道全局限速闸门。
        """
        try:
            interval = max(
                0.0, float(self._cfg("min_request_interval_ms", 400) or 0) / 1000.0
            )
        except (TypeError, ValueError):
            interval = 0.4
        async with self._throttle_lock:
            if interval > 0:
                wait = self._last_call_at + interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_call_at = time.monotonic()

    def _tool_payload(self, parsed: dict[str, Any]) -> Any:
        """取 MCP 返回的业务数据：优先结构化内容，退回解析出的 message。"""
        structured = parsed.get("structured")
        if structured not in (None, {}, []):
            return structured
        return parsed.get("message")

    def _mcp_url(self) -> str:
        """拼接 MCP 端点 URL，并把 Token 作为 query 参数带上。

        所有方法的鉴权都依赖 URL query 上的 token；Authorization 头是 tools/call
        的额外要求（见 _mcp_headers）。仅带 Authorization 头会返回
        8011 "api info not exist"。

        Returns:
            已附加 token 查询参数的端点 URL。
        """
        endpoint = str(self._cfg("mcp_endpoint") or DEFAULT_MCP_ENDPOINT)
        return mcp_protocol.build_mcp_url(endpoint, self._token())

    def _mcp_headers(self, *, with_authorization: bool = False) -> dict[str, str]:
        """构造 MCP 请求头。

        实测网关对 Authorization 头的要求与方法相关：
        - ``tools/call`` 必须带，否则在 oidb 层报 151 "登录态验证失败"；
        - ``initialize`` / ``tools/list`` / ``notifications/initialized`` 带上反而会返回
          8011 "api info not exist"，只能靠 URL query 上的 token 鉴权。

        Args:
            with_authorization: 是否附加 Authorization 头（仅 tools/call 需要）。

        Returns:
            请求头字典。
        """
        return mcp_protocol.build_mcp_headers(
            self._token(), with_authorization=with_authorization
        )

    async def _notify_initialized(self) -> None:
        """补发 MCP 规范的 notifications/initialized 通知。

        initialize 之后缺少这一步会让后续 tools/list / tools/call 变得不稳定。
        该通知是单向的，失败不阻断主流程。
        """
        payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        try:
            await self._post_json(self._mcp_url(), payload, self._mcp_headers())
        except TencentChannelError as exc:
            logger.debug(
                f"[{PLUGIN_NAME}] notifications/initialized 发送失败（忽略）：{exc}"
            )

    async def _mcp_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        token_required: bool = False,
        with_authorization: bool = False,
        expect_tool_result: bool = False,
    ) -> dict[str, Any]:
        """向 MCP 端点发起一次 JSON-RPC 调用，并对瞬时故障自动重试。

        Args:
            method: JSON-RPC 方法名。
            params: 方法参数。
            token_required: 是否强制要求已配置 Token。
            with_authorization: 是否附加 Authorization 头（仅 tools/call 需要）。
            expect_tool_result: 为 True 时不把 result.isError 当成异常抛出，
                交由调用方 _extract_tool_result 生成友好错误。

        Returns:
            解析后的 JSON-RPC 响应。

        Raises:
            TencentChannelError: 请求失败、网关持续返回瞬时错误或响应格式异常。
        """
        token = self._token()
        if token_required and not token:
            raise TencentChannelError(
                "未配置 QQ AI Connect Token。请使用 /txcm token 写入。"
            )

        payload = {
            "jsonrpc": "2.0",
            "id": f"astrbot-{int(time.time() * 1000)}",
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        url = self._mcp_url()
        headers = self._mcp_headers(with_authorization=with_authorization)
        last_detail = ""
        for attempt in range(1, MCP_MAX_ATTEMPTS + 1):
            if attempt > 1:
                await asyncio.sleep(MCP_RETRY_BACKOFF_SECONDS * (attempt - 1))
            try:
                data = await self._post_json(url, payload, headers)
            except TencentChannelError as exc:
                last_detail = mcp_protocol.redact_token(str(exc), self._token())
                if attempt < MCP_MAX_ATTEMPTS and mcp_protocol.is_transient_mcp_failure(
                    last_detail
                ):
                    logger.warning(
                        f"[{PLUGIN_NAME}] MCP {method} 瞬时失败（第 {attempt} 次），"
                        f"将重试：{last_detail[:200]}"
                    )
                    continue
                raise

            if "error" in data:
                error = data.get("error") or {}
                message = (
                    error.get("message") if isinstance(error, dict) else str(error)
                )
                last_detail = mcp_protocol.redact_token(
                    f"MCP {method} 失败: {message}", self._token()
                )
                if attempt < MCP_MAX_ATTEMPTS and mcp_protocol.is_transient_mcp_failure(
                    f"{message} {json.dumps(error, ensure_ascii=False)}"
                ):
                    logger.warning(
                        f"[{PLUGIN_NAME}] MCP {method} 瞬时失败（第 {attempt} 次），"
                        f"将重试：{message}"
                    )
                    continue

                if "token" in str(message).lower() or "auth" in str(message).lower():
                    hint = "请检查 Token，或使用 /txcm login 重新授权。"
                else:
                    hint = "请检查请求参数和 MCP 接口配置。"
                raise TencentChannelError(f"{last_detail}。{hint}", data)

            # 网关会把鉴权/接口类错误包成 result.isError 返回（例如 8011 藏在
            # _meta.AdditionalFields 里），这类同样需要重试后判定。
            result = data.get("result")
            if isinstance(result, dict) and result.get("isError"):
                meta = (result.get("_meta") or {}).get("AdditionalFields") or {}
                detail = f"{meta.get('retCode')} {meta.get('errMsg')}"
                last_detail = mcp_protocol.redact_token(
                    f"MCP {method} 返回错误: {detail}", self._token()
                )
                if attempt < MCP_MAX_ATTEMPTS and mcp_protocol.is_transient_mcp_failure(
                    detail
                ):
                    logger.warning(
                        f"[{PLUGIN_NAME}] MCP {method} 瞬时失败（第 {attempt} 次），"
                        f"将重试：{detail[:200]}"
                    )
                    continue
                if not expect_tool_result:
                    raise TencentChannelError(
                        f"{last_detail}。重试 {attempt} 次后仍失败，请稍后再试。", data
                    )
            return data

        raise TencentChannelError(
            f"{last_detail}。网关连续 {MCP_MAX_ATTEMPTS} 次返回瞬时故障，请稍后重试。"
        )

    async def _initialize_mcp(self) -> dict[str, Any]:
        if self._server_info:
            return self._server_info

        response = await self._mcp_request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
            },
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise TencentChannelError("MCP initialize 返回格式异常。", response)
        self._server_info = result
        await self._notify_initialized()
        return result

    async def _list_mcp_tools(self, *, force: bool = False) -> list[dict[str, Any]]:
        if (
            self._tool_cache is not None
            and not force
            and self._cfg("cache_tool_schema")
        ):
            return self._tool_cache

        await self._initialize_mcp()
        tools: list[dict[str, Any]] = []
        for attempt in range(1, MCP_MAX_ATTEMPTS + 1):
            if attempt > 1:
                await asyncio.sleep(MCP_RETRY_BACKOFF_SECONDS * (attempt - 1))
            response = await self._mcp_request("tools/list")
            raw_tools = response.get("result", {}).get("tools", [])
            if not isinstance(raw_tools, list):
                raise TencentChannelError("MCP tools/list 返回格式异常。", response)
            tools = [tool for tool in raw_tools if isinstance(tool, dict)]
            if tools:
                break
            # 网关偶发返回空列表（同为瞬时故障），重试一次即可恢复。
            logger.warning(
                f"[{PLUGIN_NAME}] MCP tools/list 返回空列表（第 {attempt} 次）。"
            )
        self._tool_cache = tools
        return self._tool_cache

    def _extract_tool_result(self, response: dict[str, Any]) -> dict[str, Any]:
        result = response.get("result")
        if not isinstance(result, dict):
            raise TencentChannelError("MCP tools/call 返回格式异常。", response)

        parsed: dict[str, Any] = {
            "is_error": bool(result.get("isError")),
            "code": None,
            "message": None,
            "structured": None,
            "content": [],
            "raw": result,
        }
        # 网关若返回结构化内容就优先用它，避免依赖 "code(0):/message(返回信息):" 文本协议
        structured = result.get("structuredContent")
        if structured not in (None, {}, []):
            parsed["structured"] = structured
        for item in result.get("content", []):
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = str(item.get("text") or "")
            parsed["content"].append(text)
            lower = text.lower()
            if lower.startswith("code(") and ":" in text:
                code_text = text.split(":", 1)[1].strip()
                parsed["code"] = int(code_text) if code_text.isdigit() else code_text
            elif lower.startswith("message(") and ":" in text:
                raw_message = text.split(":", 1)[1].strip()
                parsed["message"] = mcp_protocol.parse_json_text(
                    raw_message, fallback=raw_message
                )

        if parsed["is_error"]:
            raise TencentChannelError(self._friendly_mcp_tool_error(parsed), parsed)
        return parsed

    def _friendly_mcp_tool_error(self, parsed: dict[str, Any]) -> str:
        """把 MCP tool 错误转换为可操作提示。

        Args:
            parsed: _extract_tool_result 解析出的错误内容。

        Returns:
            面向管理员和 LLM 的错误说明。
        """
        code = str(parsed.get("code") or "")
        message = parsed.get("message")
        if isinstance(message, dict):
            text = json.dumps(message, ensure_ascii=False)
        else:
            text = str(message or "无详细信息")

        if "api info not exist" in text.lower() or code == "130001":
            return (
                "网关上不存在该工具/接口（retCode 130001 api info not exist），"
                "通常是工具名拼写错误。请用 txcm_list_tools 确认工具名，"
                "再用 txcm_get_tool_schema 查看入参后重试。"
            )
        if code == "8011" or "未登录" in text or "token" in text.lower():
            return "腾讯频道鉴权失败，请使用 /txcm login 重新授权，或用 /txcm token 写入有效 Token。"
        if is_rate_limit_payload(parsed):
            return "腾讯频道接口触发频率限制，请等待约 70 秒后重试。"
        if code == "20047" or "需要加入" in text or "加入后" in text:
            return "该频道需要先加入才能浏览，请用 search_guild_content 找到并加入频道后重试。"
        if code == "130000" or "搜索失败" in text:
            return "搜索失败，该频道可能未加入。search-guild-feeds 只能搜索已加入频道，请先加入后重试。"
        if code == "20006" or "加入频道后可互动" in text:
            return "该频道未开放访客互动，需先加入频道才能评论或回复，请用 join_guild 加入后重试。"
        if code == "100707" or "未回复" in text:
            return "私信发送受限，对方未回复前只能发送 1 条消息，请等待对方回复后再发。"
        if "required" in text.lower() or "参数" in text or "validation" in text.lower():
            return (
                f"腾讯频道参数校验失败：{text}。请先用 /txcm schema 查看工具 schema。"
            )
        return f"腾讯频道 MCP 工具返回错误：{text}"

    async def call_mcp_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        bypass_risk_gate: bool = False,
    ) -> dict[str, Any]:
        """调用一个 MCP tool（插件内所有上游能力的唯一出口）。

        Args:
            tool_name: MCP 工具名，支持下划线或连字符。
            arguments: 传给 tools/call 的 arguments。
            bypass_risk_gate: 跳过写操作/高风险操作闸门。仅供插件内部的只读调用使用，
                新增调用不要在写操作上打开它。

        Returns:
            _extract_tool_result 解析后的结果。

        Raises:
            TencentChannelError: 工具名/参数非法、被风险闸门拦截或上游返回错误。
        """
        normalized = mcp_protocol.normalize_tool_name(tool_name)
        if not normalized:
            raise TencentChannelError("工具名不能为空。")
        if not isinstance(arguments, dict):
            raise TencentChannelError("arguments 必须是 JSON object。")

        await self._throttle()

        if not bypass_risk_gate:
            if normalized in HIGH_RISK_TOOLS and not self._cfg(
                "enable_high_risk_tools"
            ):
                raise TencentChannelError(
                    f"{normalized} 属于高风险操作，请在插件配置中启用高风险工具后再调用。"
                )
            if normalized in WRITE_TOOLS and not self._cfg("enable_write_tools"):
                raise TencentChannelError(
                    f"{normalized} 属于写操作，请在插件配置中启用写操作工具后再调用。"
                )

        try:
            response = await self._mcp_request(
                "tools/call",
                {"name": normalized, "arguments": arguments},
                token_required=True,
                with_authorization=True,
                expect_tool_result=True,
            )
            return self._extract_tool_result(response)
        except TencentChannelError as exc:
            if not self._is_rate_limit_error(exc):
                raise
            logger.warning(
                f"[{PLUGIN_NAME}] {normalized} 触发频率限制，70 秒后自动重试一次"
            )
            await asyncio.sleep(70)
            response = await self._mcp_request(
                "tools/call",
                {"name": normalized, "arguments": arguments},
                token_required=True,
                with_authorization=True,
                expect_tool_result=True,
            )
            return self._extract_tool_result(response)

    def _is_rate_limit_error(self, exc: TencentChannelError) -> bool:
        """判断异常是否为限流错误，复用 is_rate_limit_payload 保持一致。"""
        return is_rate_limit_payload(
            getattr(exc, "data", None)
        ) or is_rate_limit_payload(str(exc))

    async def _check_skill_update(self) -> dict[str, Any]:
        """HEAD 请求检测官方 Skill 是否有新版本。"""
        result: dict[str, Any] = {
            "latest_version": None,
            "current_version": SKILL_VERSION,
            "update_available": False,
        }
        session = await self._get_session()
        proxy = str(self._cfg("proxy", "") or "").strip() or None
        try:
            async with session.head(
                SKILL_UPDATE_CHECK_URL,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True,
            ) as response:
                latest = response.headers.get("x-cos-meta-tcc-version")
                if latest:
                    result["latest_version"] = latest
                    result["update_available"] = latest != SKILL_VERSION
                else:
                    result["error"] = "响应缺少 x-cos-meta-tcc-version 头"
                    result["error_hint"] = (
                        "官方版本检测失败（响应缺少版本信息），如需详情请查看日志。"
                    )
        except Exception as exc:  # 兜底写入结果字段，不向外抛出
            result["error"] = str(exc)
            result["error_hint"] = (
                f"官方版本检测失败：{type(exc).__name__}，如需详情请查看日志。"
            )
        return result
