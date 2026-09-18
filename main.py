from __future__ import annotations

import asyncio
import base64
import datetime
import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .app.core.constants import (
    CONFIG_DEFAULTS,
    CONFIG_PATHS,
    DEFAULT_GUILD_LIST_ARGUMENTS,
    PLUGIN_NAME,
    TXCM_LLM_TOOL_NAMES,
    UNAVAILABLE_TOOLS,
)
from .app.core.errors import TencentChannelError
from .app.models.cli_reference import CLI_COMMANDS, ENDPOINT_GUIDE
from .app.models.skill_guide import skill_guide_text
from .app.services import skill_localize, skill_source
from .app.services.device_login import DeviceLoginMixin
from .app.services.mcp_client import McpClientMixin
from .app.utils import channel_data as cdata
from .app.utils import mcp_protocol
from .tools import TencentChannelFunctionTool
from .tools.schema import (
    boolean_param,
    integer_param,
    object_parameters,
    string_param,
)


def _json_dumps(data: Any, limit: int = 4000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... 已截断 ..."


def _format_timestamp(value: Any) -> str:
    """把秒级时间戳格式化成东八区可读时间（空值/非法值返回空串）。"""
    seconds = cdata.as_int(value)
    if seconds <= 0:
        return ""
    try:
        moment = datetime.datetime.fromtimestamp(
            seconds, tz=datetime.timezone(datetime.timedelta(hours=8))
        )
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


# 插件元数据以 metadata.yaml 为准（优先级高于已废弃的 @register 装饰器），
# 这里不再重复声明名称/描述/版本，避免两处漂移。
class TencentChannelCommunityPlugin(McpClientMixin, DeviceLoginMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._session: aiohttp.ClientSession | None = None
        self._server_info: dict[str, Any] | None = None
        self._tool_cache: list[dict[str, Any]] | None = None
        self._login_task: asyncio.Task | None = None
        self._throttle_lock = asyncio.Lock()
        self._last_call_at = 0.0
        self._data_cache: dict[str, tuple[float, Any]] = {}

    async def initialize(self) -> None:
        self.context.add_llm_tools(
            TencentChannelFunctionTool(
                name="txcm_status",
                description="检查 tencent-channel-cli 接口、Token 配置和基础连通性。",
                parameters=object_parameters({}),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_list_tools",
                description="列出腾讯频道 MCP 可用工具，可按关键词过滤。",
                parameters=object_parameters(
                    {"query": string_param("可选。按工具名或描述过滤。")}
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_get_tool_schema",
                description="获取某个腾讯频道 MCP 工具的 JSON Schema。",
                parameters=object_parameters(
                    {"tool_name": string_param("工具名，支持下划线或连字符。")},
                    required=["tool_name"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_list_guilds",
                description="获取当前账号已加入的腾讯频道列表（频道名/频道号已自动解码）。",
                parameters=object_parameters({}),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_guild_channels",
                description=(
                    "列出某个频道的版块（子频道）列表，返回 channel_id 与版块名。"
                    "guild 可传频道 id、频道号或名称片段。"
                ),
                parameters=object_parameters(
                    {
                        "guild": string_param(
                            "频道 id / 频道号 / 名称片段，例如「启翔湖畔」。"
                        )
                    },
                    required=["guild"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_search_feeds",
                description=(
                    "按关键词搜索频道内的帖子，返回标题/正文/作者/时间/评论数/版块。"
                    "搜索比逐页翻更准，优先用它；结果已按相关度排序。"
                ),
                parameters=object_parameters(
                    {
                        "guild": string_param("频道 id / 频道号 / 名称片段。"),
                        "keyword": string_param(
                            "搜索关键词，例如「安全防卫学 上课地点」。"
                        ),
                        "limit": integer_param("返回条数，默认 10，最大 30。"),
                    },
                    required=["guild", "keyword"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_latest_feeds",
                description="拉取频道主页帖子流（热门或最新），用于看近期动态。",
                parameters=object_parameters(
                    {
                        "guild": string_param("频道 id / 频道号 / 名称片段。"),
                        "count": integer_param("返回条数，默认 10，最大 30。"),
                        "order": string_param("hot=热门（默认），new=最新。"),
                    },
                    required=["guild"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_read_feed",
                description=(
                    "读某条帖子的详情和评论（评论正文会自动解码）。"
                    "评论默认自动回填；特殊情况下可显式提供 guild / channel_id。"
                ),
                parameters=object_parameters(
                    {
                        "feed_id": string_param("帖子 id（feed_id）。"),
                        "guild": string_param(
                            "可选。频道 id / 频道号 / 名称片段，评论接口需要。"
                        ),
                        "channel_id": string_param("可选。子频道 id，评论接口需要。"),
                        "with_comments": boolean_param("是否抓取评论，默认 true。"),
                    },
                    required=["feed_id"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_do_comment",
                description=(
                    "给指定帖子发表评论（type=1）。自动获取原帖 StFeed 并组装请求，"
                    "content 传纯文本即可（插件自动 base64）；poster_tinyid 可选，"
                    "未填则省略评论人信息。删除评论（type=0/2）属高风险，不在本工具用途内。"
                ),
                parameters=object_parameters(
                    {
                        "feed_id": string_param("目标帖子 id。"),
                        "content": string_param("评论正文，纯文本。"),
                        "poster_tinyid": string_param(
                            "可选。评论人 tinyid（>10 位数字），一般可不填。"
                        ),
                    },
                    required=["feed_id", "content"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_ask_channel",
                description=(
                    "在频道里问一个问题：自动搜索相关帖子 → 读评论 → 按相关度排序，"
                    "返回带出处（作者+时间）的回答素材。适合「某课在哪上」这类事实性问题。"
                ),
                parameters=object_parameters(
                    {
                        "guild": string_param("频道 id / 频道号 / 名称片段。"),
                        "question": string_param(
                            "要问的问题，例如「大学生安全防卫学上课地点」。"
                        ),
                        "limit": integer_param("最多深挖几条帖子，默认 5，最大 8。"),
                    },
                    required=["guild", "question"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_call_tool",
                description=(
                    "调用腾讯频道 MCP 原始工具。arguments_json 必须是 JSON object 字符串。"
                    "鉴权由插件自动处理：8011/130001=接口或工具不存在，151=登录态失效需 /txcm login。"
                    "写操作和高风险操作受插件配置开关限制。"
                ),
                parameters=object_parameters(
                    {
                        "tool_name": string_param("MCP 工具名，支持下划线或连字符。"),
                        "arguments_json": string_param(
                            '传给 MCP tools/call 的 arguments JSON object 字符串，例如 {"guildId":"123"}。'
                        ),
                    },
                    required=["tool_name", "arguments_json"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_list_cli_commands",
                description="列出 tencent-channel-cli 官方命令与当前插件 MCP 对齐关系。",
                parameters=object_parameters(
                    {"query": string_param("可选。按命令名、MCP 工具名或说明过滤。")}
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_get_cli_mapping",
                description="查询某个 tencent-channel-cli 命令对应的 MCP tool 或本地流程说明。",
                parameters=object_parameters(
                    {
                        "command": string_param(
                            "CLI 命令，例如 feed.publish-feed 或 manage get-guild-info。"
                        )
                    },
                    required=["command"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_call_cli_command",
                description=(
                    "按 CLI 命令名定位 MCP tool 并调用。arguments_json 必须是对应 MCP tool schema 的 "
                    "JSON object 字符串，写操作和高风险操作受插件配置开关限制。"
                    "鉴权由插件自动处理：8011/130001=接口或工具不存在，151=登录态失效需 /txcm login。"
                ),
                parameters=object_parameters(
                    {
                        "command": string_param("CLI 命令，例如 feed.publish-feed。"),
                        "arguments_json": string_param(
                            '对应 MCP tool 的 arguments JSON object 字符串，例如 {"guildId":"123"}。'
                        ),
                    },
                    required=["command", "arguments_json"],
                ),
                plugin=self,
            ),
            TencentChannelFunctionTool(
                name="txcm_endpoint_guide",
                description="查看腾讯频道 CLI/Skill 使用到的接口参考。",
                parameters=object_parameters(
                    {
                        "topic": string_param(
                            "可选。login_request_device_code/login_poll_device_token/mcp_json_rpc/media_sliceupload/skill_update_check。"
                        )
                    }
                ),
                plugin=self,
            ),
        )
        await self._ensure_default_tool_permissions()
        logger.info(f"[{PLUGIN_NAME}] Tencent Channel LLM tools registered")

    async def _ensure_default_tool_permissions(self) -> None:
        """把本插件 LLM Tools 的默认权限交给 AstrBot 权限配置。

        AstrBot 只在 ``tool_permissions._default`` 里没有该工具时才回退到"非内置工具
        默认 member（不限制）"，所以这里显式写入 admin，避免本插件的写操作级工具
        被普通成员直接调用。已存在的配置不会被覆盖。
        """
        try:
            perms_store = await sp.global_get("tool_permissions", {})
            if not isinstance(perms_store, dict):
                perms_store = {}
            defaults = perms_store.get("_default", {})
            if not isinstance(defaults, dict):
                defaults = {}

            changed = False
            for tool_name in TXCM_LLM_TOOL_NAMES:
                if tool_name not in defaults:
                    defaults[tool_name] = "admin"
                    changed = True
            if changed:
                perms_store["_default"] = defaults
                await sp.global_put("tool_permissions", perms_store)
        except Exception as exc:  # 兜底告警，不阻断插件加载
            logger.warning(
                f"[{PLUGIN_NAME}] failed to set default tool permissions: {exc}"
            )

    async def terminate(self) -> None:
        if self._login_task and not self._login_task.done():
            self._login_task.cancel()
        if self._session:
            await self._session.close()
            self._session = None

    def _cfg(self, key: str, default: Any = None) -> Any:
        path = CONFIG_PATHS.get(key)
        fallback = CONFIG_DEFAULTS.get(key, default)
        if path:
            section = self.config.get(path[0], {})
            if isinstance(section, dict) and path[1] in section:
                return section[path[1]]
        return self.config.get(key, fallback)

    def _set_cfg(self, key: str, value: Any) -> None:
        path = CONFIG_PATHS.get(key)
        if path:
            section = self.config.get(path[0])
            if not isinstance(section, dict):
                section = {}
                self.config[path[0]] = section
            section[path[1]] = value
            return
        self.config[key] = value

    def _save_config(self) -> None:
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            save_config()

    def _extract_guilds(self, data: Any) -> list[dict[str, Any]]:
        """从任意响应里抽出原始频道条目（结构解析见 channel_data.extract_guilds）。"""
        return cdata.extract_guilds(data)

    async def _list_guilds_payload(self) -> dict[str, Any]:
        result = await self.call_mcp_tool(
            "get_my_join_guild_info",
            DEFAULT_GUILD_LIST_ARGUMENTS,
            bypass_risk_gate=True,
        )
        return {
            "parsed": result,
            "guilds": self._extract_guilds(result.get("message")),
        }

    def _format_guilds(self, guilds: list[dict[str, Any]]) -> str:
        if not guilds:
            return "未解析到频道列表。Token 可能有效，但上游返回结构与预期不同。"

        lines = [f"已加入频道：{len(guilds)} 个"]
        for index, guild in enumerate(guilds[:20], start=1):
            name = (
                guild.get("guildName")
                or guild.get("strGuildName")
                or guild.get("name")
                or "未命名频道"
            )
            number = guild.get("guildNumber") or guild.get("strGuildNumber") or ""
            member_num = guild.get("memberNum") or guild.get("uint32MemberNum") or ""
            suffix = []
            if number:
                suffix.append(f"频道号 {number}")
            if member_num:
                suffix.append(f"{member_num} 人")
            lines.append(
                f"{index}. {name}" + (f"（{'，'.join(suffix)}）" if suffix else "")
            )
        if len(guilds) > 20:
            lines.append(f"... 还有 {len(guilds) - 20} 个未显示")
        return "\n".join(lines)

    async def _status_payload(self) -> dict[str, Any]:
        server = await self._initialize_mcp()
        tools = await self._list_mcp_tools()
        payload: dict[str, Any] = {
            "endpoint": self._cfg("mcp_endpoint"),
            "token_configured": bool(self._token()),
            "server": server.get("serverInfo", server),
            "tool_count": len(tools),
            "cli_command_count": len(CLI_COMMANDS),
            "write_tools_enabled": bool(self._cfg("enable_write_tools")),
            "high_risk_tools_enabled": bool(self._cfg("enable_high_risk_tools")),
        }
        if self._token():
            try:
                guilds_payload = await self._list_guilds_payload()
                payload["credential_probe"] = "ok"
                payload["guild_count"] = len(guilds_payload["guilds"])
            except TencentChannelError as exc:
                payload["credential_probe"] = f"failed: {exc}"
        payload["skill_update"] = await self._check_skill_update()
        payload["skill_update"]["localized_version"] = (
            skill_localize.localized_skill_version(Path(__file__).resolve().parent)
        )
        skill_cache = self._skill_cache_dir()
        if skill_cache is not None:
            manifest = skill_source.load_manifest(skill_cache)
            payload["skill_update"]["official_cached_version"] = (
                manifest.get("version") if manifest else None
            )
        return payload

    def _resolve_cli_command_key(self, command: str) -> str:
        """把用户输入的 CLI 命令归一化为 domain.action。

        Args:
            command: CLI 命令名，支持 feed.publish-feed、feed publish-feed 或单独 action。

        Returns:
            CLI_COMMANDS 中的标准 key。

        Raises:
            TencentChannelError: 命令不存在或单独 action 匹配到多个域。
        """
        raw = str(command or "").strip().lower()
        if raw.startswith("tencent-channel-cli "):
            raw = raw[len("tencent-channel-cli ") :].strip()
        raw = raw.replace("/", ".")
        parts = [part for part in raw.split() if part]
        if len(parts) >= 2 and parts[0] in {"feed", "manage"}:
            raw = f"{parts[0]}.{parts[1]}"
        raw = raw.replace(" ", ".")

        if "." in raw:
            domain, action = raw.split(".", 1)
            key = f"{domain}.{action.replace('_', '-')}"
            if key in CLI_COMMANDS:
                return key
        else:
            action = raw.replace("_", "-")
            matches = [key for key in CLI_COMMANDS if key.endswith(f".{action}")]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise TencentChannelError(
                    f"命令 {command} 同时匹配多个域，请写成 feed.{action} 或 manage.{action}。"
                )

        raise TencentChannelError(f"未找到 CLI 命令映射: {command}")

    def _cli_mapping_payload(self, command: str) -> dict[str, Any]:
        key = self._resolve_cli_command_key(command)
        item = dict(CLI_COMMANDS[key])
        item["command"] = key
        item["supported_by_plugin"] = bool(item.get("tool"))
        if item.get("tool"):
            item["call_note"] = (
                "可用 /txcm schema 查看 MCP schema，再用 /txcm ccall 或 txcm_call_cli_command 调用。"
            )
        else:
            item["call_note"] = (
                "这是 CLI 本地流程或快捷命令，插件提供组合工具但不模拟 CLI 交互状态机。"
            )
        return item

    def _endpoint_payload(self, topic: str = "") -> dict[str, Any]:
        topic = str(topic or "").strip().lower()
        if topic:
            key = topic.replace("-", "_")
            if key not in ENDPOINT_GUIDE:
                raise TencentChannelError(f"未找到接口参考: {topic}")
            return {key: ENDPOINT_GUIDE[key]}
        return ENDPOINT_GUIDE

    # ------------------------------------------------------------------ #
    # 语义化只读工具：把 oidb 原语的坑（base64、位掩码、字段名不一致）
    # 全部收在插件内部，对外只给模型干净的字段。
    # ------------------------------------------------------------------ #
    async def _guilds_normalized(
        self, *, use_cache: bool = True
    ) -> list[dict[str, Any]]:
        """归一化后的"我加入的频道"（解码频道名、带 TTL 缓存）。"""
        if use_cache:
            cached = self._cache_get("guilds")
            if cached is not None:
                return cached
        payload = await self._list_guilds_payload()
        guilds = [
            normalized
            for normalized in (cdata.normalize_guild(raw) for raw in payload["guilds"])
            if normalized.get("guild_id")
        ]
        if guilds:
            self._cache_set("guilds", guilds)
        return guilds

    async def _resolve_guild(self, reference: str) -> dict[str, Any]:
        """把频道 id / 频道号 / 名称片段解析成归一化频道信息。"""
        key = str(reference or "").strip()
        guilds = await self._guilds_normalized()
        logger.debug(f"[txcm] 解析频道引用：缓存频道数={len(guilds)}")
        if not guilds:
            raise TencentChannelError(
                "上游返回成功但未解析到频道：该账号可能尚未加入任何频道。"
                "若确认已加入，请 /txcm login 重新授权后重试；"
                "也可用 txcm_call_tool 调 get_my_join_guild_info 查看原始返回。"
            )
        if key:
            for guild in guilds:
                if key in (guild.get("guild_id"), guild.get("guild_number")):
                    return guild
            matched = [g for g in guilds if key in str(g.get("name") or "")]
            if len(matched) == 1:
                return matched[0]
            if len(matched) > 1:
                names = "、".join(str(g["name"]) for g in matched[:5])
                raise TencentChannelError(
                    f"「{key}」匹配到多个频道：{names}。请改用频道 id 或更完整的名称。"
                )
        available = "、".join(
            str(g.get("name") or g.get("guild_id")) for g in guilds[:10]
        )
        raise TencentChannelError(f"没找到频道「{key}」。当前账号已加入：{available}")

    async def _channel_map(self, guild_id: str) -> dict[str, str]:
        """子频道 id -> 版块名（带 TTL 缓存）。"""
        cache_key = f"channels:{guild_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        result = await self.call_mcp_tool(
            "get_guild_channel_list", {"guildIds": [guild_id]}
        )
        raw_channels = cdata.extract_channels(self._tool_payload(result))
        mapping = {
            channel["channel_id"]: channel["name"]
            for channel in (cdata.normalize_channel(raw) for raw in raw_channels)
            if channel.get("channel_id")
        }
        if mapping:
            self._cache_set(cache_key, mapping)
        return mapping

    async def _safe_channel_map(self, guild_id: str) -> dict[str, str]:
        """取版块名映射；失败时退化为空映射，不阻断主流程。

        降级会打 warning 日志：否则模型和用户都分不清"没有版块名"还是"版块接口坏了"。
        """
        try:
            return await self._channel_map(guild_id)
        except TencentChannelError as exc:
            logger.warning(
                f"[{PLUGIN_NAME}] 获取版块列表失败，本次结果不带版块名：{exc}"
            )
            return {}

    def _feed_view(
        self, raw: dict[str, Any], channels: dict[str, str]
    ) -> dict[str, Any]:
        """把原始帖子转成给模型看的精简结构（字段归一 + 时间可读）。"""
        feed = cdata.normalize_feed(raw)
        channel_id = feed["channel_id"]
        return {
            "feed_id": feed["feed_id"],
            "guild_id": feed.get("guild_id", ""),
            "channel_id": channel_id,
            # 版块名取不到时退回 channel_id，至少让模型知道帖子属于哪个版块
            "channel": channels.get(channel_id) or (channel_id if channel_id else ""),
            "title": feed["title"],
            "content": feed["content"][:280],
            "author": feed["author"],
            "time": _format_timestamp(feed["create_time"]),
            "create_time": feed["create_time"],
            "comment_count": feed["comment_count"],
            "image_count": feed["image_count"],
        }

    def _comment_view(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把原始评论转成精简结构（正文 / IP 属地 / 实体分开给出）。

        正文里的实体是带标注的内联形式（``[表情:汪汪]`` / ``[卡片:标题]`` / ``[@昵称]``），
        原始 id / url 放在结构化字段里；空列表不放进结果，避免每次调用都带三个空数组。
        """
        comment = cdata.normalize_comment(raw)
        view: dict[str, Any] = {
            "author": comment["author"],
            "time": _format_timestamp(comment["create_time"]),
            "content": comment["content"],
            "location": comment["location"],
        }
        for key in ("faces", "cards", "mentions"):
            if comment.get(key):
                view[key] = comment[key]
        return view

    async def _search_feeds(self, guild_id: str, query: str) -> dict[str, Any]:
        """调用搜索接口并返回业务数据（searchType.type 必须为 0）。"""
        result = await self.call_mcp_tool(
            "get_search_guild_feed",
            {
                "guildId": guild_id,
                "query": query,
                "searchType": {"type": 0, "feedType": 1},
                "cookie": "",
            },
        )
        return self._tool_payload(result) or {}

    @staticmethod
    def _search_total(payload: Any) -> int | None:
        """搜索命中总数（实测在 unionResult.feedTotal，且是字符串）。"""
        if not isinstance(payload, dict):
            return None
        for node in (payload, payload.get("unionResult")):
            if isinstance(node, dict):
                total = node.get("feedTotal")
                if total not in (None, ""):
                    return cdata.as_int(total)
        return None

    @staticmethod
    def _search_guild_url(payload: Any) -> str:
        """频道分享链接（实测在 aiSearchInfo.guildUrl）。"""
        if not isinstance(payload, dict):
            return ""
        info = payload.get("aiSearchInfo")
        if isinstance(info, dict):
            return str(info.get("guildUrl") or "")
        return ""

    async def _fetch_guild_feeds(
        self, guild_id: str, get_type: int, count: int
    ) -> dict[str, Any]:
        """调用帖子流接口并返回业务数据。"""
        result = await self.call_mcp_tool(
            "get_guild_feeds",
            {"guildId": guild_id, "getType": get_type, "count": count},
        )
        return self._tool_payload(result) or {}

    async def _feed_comments(
        self, feed_id: str, *, guild_id: str = "", channel_id: str = ""
    ) -> list[dict[str, Any]]:
        """取某帖评论。

        实测约束（2026-09 验证）：
        - ``channelSign`` 必须带且为驼峰 ``guildId``/``channelId``，缺了会报"请求失败"；
        - ``pageSize`` 默认 20；上限以 schema 为准（历史实测 30/50 曾被拒）。
        - 评论数组字段名是 ``vecComment``，正文是 base64 protobuf（由 channel_data 解码）。
        """
        arguments: dict[str, Any] = {"feedId": feed_id, "pageSize": 20}
        if guild_id and channel_id:
            arguments["channelSign"] = {"guildId": guild_id, "channelId": channel_id}
        result = await self.call_mcp_tool("get_feed_comments", arguments)
        raw_comments = cdata.extract_comments(self._tool_payload(result))
        return [self._comment_view(raw) for raw in raw_comments]

    async def tool_guild_channels(self, guild: str) -> str:
        target = await self._resolve_guild(guild)
        mapping = await self._channel_map(target["guild_id"])
        return _json_dumps(
            {
                "ok": True,
                "guild": target,
                "channel_count": len(mapping),
                "channels": [
                    {"channel_id": channel_id, "name": name}
                    for channel_id, name in mapping.items()
                ],
            },
            12000,
        )

    async def tool_search_feeds(self, guild: str, keyword: str, limit: int = 10) -> str:
        target = await self._resolve_guild(guild)
        query = str(keyword or "").strip()
        if not query:
            raise TencentChannelError("keyword 不能为空。")
        count = max(1, min(cdata.as_int(limit, 10) or 10, 30))
        payload = await self._search_feeds(target["guild_id"], query)
        channels = await self._safe_channel_map(target["guild_id"])
        feeds = [self._feed_view(raw, channels) for raw in cdata.extract_feeds(payload)]
        ranked = [
            row["item"]
            for row in cdata.rank_by_relevance(
                feeds, query, text_keys=("title", "content")
            )
        ]
        return _json_dumps(
            {
                "ok": True,
                "guild": target,
                "guild_url": self._search_guild_url(payload),
                "query": query,
                "total_matched": self._search_total(payload),
                "returned": len(ranked[:count]),
                "feeds": ranked[:count],
            },
            12000,
        )

    async def tool_latest_feeds(
        self, guild: str, count: int = 10, order: str = "hot"
    ) -> str:
        target = await self._resolve_guild(guild)
        size = max(1, min(cdata.as_int(count, 10) or 10, 30))
        wanted = str(order or "hot").strip().lower()
        get_type = 2 if wanted in ("new", "latest", "最新") else 1
        channels = await self._safe_channel_map(target["guild_id"])
        payload = await self._fetch_guild_feeds(target["guild_id"], get_type, size)
        feeds = [self._feed_view(raw, channels) for raw in cdata.extract_feeds(payload)]
        note = ""
        if not feeds and get_type == 2:
            # 实测 getType=2（最新）经常返回空列表，退回热门流并说明，避免模型拿到空结果
            payload = await self._fetch_guild_feeds(target["guild_id"], 1, size)
            feeds = [
                self._feed_view(raw, channels) for raw in cdata.extract_feeds(payload)
            ]
            note = "最新流返回为空，已退回热门流（网关 getType=2 实测常空）。"
        return _json_dumps(
            {
                "ok": True,
                "guild": target,
                "order": "new" if get_type == 2 else "hot",
                "returned": len(feeds),
                "feeds": feeds,
                "note": note,
            },
            12000,
        )

    async def tool_read_feed(
        self,
        feed_id: str,
        guild: str = "",
        channel_id: str = "",
        with_comments: bool = True,
    ) -> str:
        fid = str(feed_id or "").strip()
        if not fid:
            raise TencentChannelError("feed_id 不能为空。")

        payload: dict[str, Any] = {
            "ok": True,
            "feed_id": fid,
            "feed": None,
            "comments": [],
            "note": "",
        }
        detail = await self.call_mcp_tool("get_feed_detail", {"feedId": fid})
        detail_payload = self._tool_payload(detail)
        raw_feeds = cdata.extract_feeds(detail_payload) if detail_payload else []
        if raw_feeds:
            payload["feed"] = self._feed_view(raw_feeds[0], {})
        elif detail_payload:
            payload["feed"] = {"raw": detail_payload}

        if with_comments:
            guild_id = str(guild or "").strip()
            if guild_id and not guild_id.isdigit():
                try:
                    guild_id = (await self._resolve_guild(guild_id))["guild_id"]
                except TencentChannelError as exc:
                    payload["note"] = f"频道解析失败：{exc}；"
                    guild_id = ""
            channel_id = str(channel_id or "").strip()
            if not guild_id:
                guild_id = str((payload["feed"] or {}).get("guild_id") or "")
            if not channel_id:
                channel_id = str((payload["feed"] or {}).get("channel_id") or "")
            try:
                payload["comments"] = await self._feed_comments(
                    fid, guild_id=guild_id, channel_id=channel_id
                )
            except TencentChannelError as exc:
                payload["note"] += (
                    f"评论获取失败：{exc}。可先用 txcm_search_feeds 或 txcm_latest_feeds "
                    "拿到 channel_id 后重试（评论接口需要 channelSign）。"
                )
        return _json_dumps(payload, 16000)

    async def tool_do_comment(
        self,
        feed_id: str,
        content: str,
        poster_tinyid: str = "",
        comment_type: int = 1,
    ) -> str:
        """发表评论：自动取原帖 StFeed 透传，content 自动 base64。"""
        fid = str(feed_id or "").strip()
        text = str(content or "").strip()
        if not fid:
            raise TencentChannelError("feed_id 不能为空。")
        if not text:
            raise TencentChannelError("content 不能为空。")

        detail = await self.call_mcp_tool("get_feed_detail", {"feedId": fid})
        raw_feeds = cdata.extract_feeds(self._tool_payload(detail))
        if not raw_feeds:
            raise TencentChannelError("未取到原帖数据，无法安全发表评论；请稍后重试。")

        comment: dict[str, Any] = {
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii")
        }
        tinyid = str(poster_tinyid or "").strip()
        if tinyid:
            comment["postUser"] = {"id": tinyid}

        result = await self.call_mcp_tool(
            "do_comment",
            {
                "commentType": int(comment_type),
                "feed": raw_feeds[0],
                "comment": comment,
            },
        )
        return _json_dumps(result, 8000)

    async def tool_ask_channel(self, guild: str, question: str, limit: int = 5) -> str:
        """组合动作：搜帖子 → 读评论 → 按相关度排序，带出处返回回答素材。"""
        target = await self._resolve_guild(guild)
        text = str(question or "").strip()
        if not text:
            raise TencentChannelError("question 不能为空。")
        size = max(1, min(cdata.as_int(limit, 5) or 5, 8))

        payload = await self._search_feeds(target["guild_id"], text)
        channels = await self._safe_channel_map(target["guild_id"])
        posts = [self._feed_view(raw, channels) for raw in cdata.extract_feeds(payload)]
        ranked = [
            row["item"]
            for row in cdata.rank_by_relevance(
                posts, text, text_keys=("title", "content")
            )
        ][:size]

        snippets: list[dict[str, Any]] = []
        for post in ranked:
            entry = dict(post)
            entry["comments"] = []
            try:
                comments = await self._feed_comments(
                    post["feed_id"],
                    guild_id=target["guild_id"],
                    channel_id=post["channel_id"],
                )
            except TencentChannelError as exc:
                entry["comments_error"] = str(exc)
            else:
                entry["comments"] = [
                    row["item"]
                    for row in cdata.rank_by_relevance(
                        comments, text, text_keys=("content",)
                    )[:5]
                ]
            snippets.append(entry)

        return _json_dumps(
            {
                "ok": True,
                "guild": target,
                "guild_url": self._search_guild_url(payload),
                "question": text,
                "total_matched": self._search_total(payload),
                "scanned_posts": len(snippets),
                "snippets": snippets,
                "note": (
                    "答案通常出现在 comments 里；正文/评论由上游返回，可能被截断，"
                    "结论请以 latest 为准并注明出处（作者+时间）。"
                ),
            },
            16000,
        )

    async def tool_status(self) -> str:
        return _json_dumps(await self._status_payload())

    async def tool_list_tools(self, query: str = "") -> str:
        tools = await self._list_mcp_tools()
        keyword = str(query or "").strip().lower()
        rows = []
        for tool in tools:
            name = str(tool.get("name") or "")
            if name in UNAVAILABLE_TOOLS:
                continue
            desc = str(tool.get("description") or "")
            if keyword and keyword not in name.lower() and keyword not in desc.lower():
                continue
            rows.append({"name": name, "description": desc})
        return _json_dumps(
            {
                "tools": rows,
                "unavailable": sorted(UNAVAILABLE_TOOLS),
                "note": "unavailable 中的工具在网关上不可用（调用 130001），已被插件禁用。",
            }
        )

    async def tool_get_tool_schema(self, tool_name: str) -> str:
        normalized = mcp_protocol.normalize_tool_name(tool_name)
        tools = await self._list_mcp_tools()
        for tool in tools:
            if (
                mcp_protocol.normalize_tool_name(str(tool.get("name") or ""))
                == normalized
            ):
                return _json_dumps(tool)
        raise TencentChannelError(f"未找到 MCP 工具: {tool_name}")

    async def tool_list_guilds(self) -> str:
        """列出已加入的频道（频道名/频道号自动 base64 解码）。"""
        guilds = await self._guilds_normalized()
        if guilds:
            return _json_dumps({"ok": True, "count": len(guilds), "guilds": guilds})
        payload = await self._list_guilds_payload()
        return _json_dumps(
            {
                "ok": False,
                "count": 0,
                "guilds": [],
                "raw_guild_count": len(payload["guilds"]),
                "note": "列表为空：该账号可能尚未加入任何频道；若确认已加入，请 /txcm login 重新授权后重试，或用 txcm_call_tool 调 get_my_join_guild_info 查看原始返回。",
            }
        )

    async def tool_call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        arguments_json: str = "",
    ) -> str:
        if arguments is None:
            arguments = mcp_protocol.parse_json_text(
                str(arguments_json or "{}"), fallback=None
            )
        if not isinstance(arguments, dict):
            raise TencentChannelError(
                'arguments_json 必须是 JSON object 字符串，例如 {"guildId":"123"}。'
            )
        result = await self.call_mcp_tool(tool_name, arguments)
        return _json_dumps(result)

    def _skill_cache_dir(self) -> Path | None:
        """官方 Skill 缓存目录；定位失败时返回 None，仅保留内置踩坑附录。"""
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME)) / "skill"
        except Exception:
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            return Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "skill"
        except Exception:
            return None

    async def _ensure_skill_source(self, *, force: bool = False) -> dict[str, Any]:
        cache_dir = self._skill_cache_dir()
        if cache_dir is None:
            return {"error": "无法定位插件数据目录，官方 Skill 缓存不可用"}
        session = await self._get_session()
        proxy = str(self._cfg("proxy", "") or "").strip() or None
        refresh = await skill_source.refresh_skill_cache(
            cache_dir, session, proxy, force=force
        )
        logger.debug(f"[txcm] 官方 Skill 缓存刷新结果：{refresh}")

    async def _localize_official_skill(self, *, force: bool = False) -> dict[str, Any]:
        """下载官方 Skill 并调用模型本地化；任何失败保留现有技能。"""
        refresh = await self._ensure_skill_source(force=force)
        if refresh.get("error"):
            return {**refresh, "stage": "download"}
        official_version = str(
            refresh.get("latest_version") or refresh.get("cached_version") or ""
        )
        if not official_version:
            return {"error": "无法确定官方 Skill 版本", "stage": "download"}
        plugin_dir = Path(__file__).resolve().parent
        current = skill_localize.localized_skill_version(plugin_dir)
        if not force and current == official_version:
            return {
                "skipped": "本地化技能已是该版本",
                "localized_version": current,
                "official_version": official_version,
            }
        cache_dir = self._skill_cache_dir()
        files = skill_source.official_files(cache_dir) if cache_dir else {}
        if not files.get("SKILL.md"):
            return {"error": "官方缓存缺少 SKILL.md", "stage": "download"}
        provider_id = str(self._cfg("skill_localize_provider", "") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        else:
            provider = await self.context.get_using_provider_async()
        if provider is None:
            return {
                "error": "未配置可用的 LLM 供应商",
                "stage": "localize",
                "official_version": official_version,
            }
        mapping_rows = [
            f"{command} -> {item.get('tool')}"
            for command, item in CLI_COMMANDS.items()
            if item.get("tool")
        ]
        prompt = skill_localize.build_localize_prompt(
            files, official_version, mapping_rows
        )
        logger.debug(
            f"[txcm] Skill 本地化：provider={'自定义' if provider_id else '主 LLM'} "
            f"official=v{official_version} prompt_chars={len(prompt)} "
            f"official_files={sorted(files)}"
        )
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=skill_localize.SYSTEM_PROMPT,
        )
        payload = skill_localize.parse_localized_payload(
            str(response.completion_text or "")
        )
        skill_localize.write_localized_skill(plugin_dir, payload)
        logger.debug(f"[txcm] Skill 本地化写盘完成：{sorted(payload)}")
        return {
            "localized_version": official_version,
            "official_version": official_version,
            "files": sorted(payload),
        }

    async def tool_list_cli_commands(self, query: str = "") -> str:
        keyword = str(query or "").strip().lower()
        rows = []
        for command, item in CLI_COMMANDS.items():
            text = " ".join(
                [
                    command,
                    str(item.get("tool") or ""),
                    str(item.get("group") or ""),
                    str(item.get("risk") or ""),
                    str(item.get("description") or ""),
                    str(item.get("note") or ""),
                ]
            ).lower()
            if keyword and keyword not in text:
                continue
            rows.append(
                {
                    "command": command,
                    "tool": item.get("tool") or "",
                    "group": item.get("group"),
                    "risk": item.get("risk"),
                    "description": item.get("description"),
                    "note": item.get("note", ""),
                    "supported_by_plugin": bool(item.get("tool")),
                }
            )
        return _json_dumps(rows)

    async def tool_get_cli_mapping(self, command: str) -> str:
        return _json_dumps(self._cli_mapping_payload(command))

    async def tool_call_cli_command(
        self,
        command: str,
        arguments: dict[str, Any] | None = None,
        arguments_json: str = "",
    ) -> str:
        mapping = self._cli_mapping_payload(command)
        tool_name = str(mapping.get("tool") or "")
        if not tool_name:
            raise TencentChannelError(str(mapping["call_note"]))
        if arguments is None:
            arguments = mcp_protocol.parse_json_text(
                str(arguments_json or "{}"), fallback=None
            )
        if not isinstance(arguments, dict):
            raise TencentChannelError(
                'arguments_json 必须是 JSON object 字符串，例如 {"guildId":"123"}。'
            )
        result = await self.call_mcp_tool(tool_name, arguments)
        return _json_dumps({"mapping": mapping, "result": result})

    async def tool_endpoint_guide(self, topic: str = "") -> str:
        return _json_dumps(self._endpoint_payload(topic))

    def _help_text(self) -> str:
        return (
            "腾讯频道社区管理工具指令：\n"
            "/txcm status - 检查端点与 Token\n"
            "/txcm token <token> - 写入 QQ AI Connect Token\n"
            "/txcm login - 发起设备码登录并自动轮询\n"
            "/txcm tools [关键词] - 列出 MCP 工具\n"
            "/txcm cli [关键词] - 列出 CLI 命令与 MCP 映射\n"
            "/txcm map <domain.action> - 查看 CLI 命令映射\n"
            "/txcm endpoints [topic] - 查看接口参考\n"
            "/txcm schema <工具名> - 查看工具 schema\n"
            "/txcm list - 列出已加入频道\n"
            "/txcm call <工具名> <JSON> - 调用原始 MCP 工具\n"
            "/txcm ccall <domain.action> <JSON> - 按 CLI 命令名调用 MCP 工具\n"
            "/txcm guide [topic] - 查看官方 Skill 与踩坑附录"
        )

    @filter.command_group("txcm")
    def txcm(self):
        """腾讯频道社区管理工具指令组。"""

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("help")
    async def txcm_help(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """显示腾讯频道插件帮助。"""
        yield event.plain_result(self._help_text())

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("status")
    async def txcm_status(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """检查腾讯频道 MCP 状态。"""
        try:
            payload = await self._status_payload()
        except TencentChannelError as exc:
            yield event.plain_result(f"腾讯频道状态检查失败：{exc}")
            return

        lines = [
            "腾讯频道 MCP 状态",
            f"端点：{payload['endpoint']}",
            f"Token：{'已配置' if payload['token_configured'] else '未配置'}",
            f"工具数：{payload['tool_count']}",
            f"CLI 映射：{payload['cli_command_count']} 条",
            f"凭证探测：{payload.get('credential_probe', '未执行')}",
            f"写操作：{'开启' if payload['write_tools_enabled'] else '关闭'}",
            f"高风险操作：{'开启' if payload['high_risk_tools_enabled'] else '关闭'}",
        ]
        if "guild_count" in payload:
            lines.append(f"已解析频道数：{payload['guild_count']}")
        skill = payload.get("skill_update")
        if isinstance(skill, dict):
            current = skill.get("current_version", "?")
            latest = skill.get("latest_version")
            if latest:
                tag = "（有新版）" if skill.get("update_available") else "（最新）"
                lines.append(f"Skill 版本：当前 {current}，官方 {latest} {tag}")
            else:
                hint = skill.get("error_hint") or skill.get("error") or "未知原因"
                lines.append(f"Skill 版本：当前 {current}，官方版本检测失败（{hint}）")
        yield event.plain_result("\n".join(lines))

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("token")
    async def txcm_token(
        self,
        event: AstrMessageEvent,
        token: GreedyStr,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """写入 QQ AI Connect Token。"""
        value = str(token or "").strip()
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        if not value:
            yield event.plain_result("用法：/txcm token <QQ_AI_CONNECT_TOKEN>")
            return

        self._set_cfg("qq_ai_connect_token", value)
        self._save_config()
        try:
            await self._list_guilds_payload()
            yield event.plain_result("Token 已写入配置，凭证探测成功。")
        except TencentChannelError as exc:
            yield event.plain_result(f"Token 已写入配置，但凭证探测失败：{exc}")

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("login")
    async def txcm_login(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """发起设备码登录并后台自动轮询。"""
        if self._login_task and not self._login_task.done():
            yield event.plain_result("已有腾讯频道登录轮询任务正在运行。")
            return

        try:
            data = await self._request_device_code()
        except TencentChannelError as exc:
            yield event.plain_result(str(exc))
            return

        device_code = str(data.get("device_code") or data.get("deviceCode") or "")
        if not device_code:
            yield event.plain_result("设备码申请成功，但响应中没有 device_code。")
            return
        device_id = str(data.get("device_id") or data.get("deviceId") or "")
        if not device_id:
            yield event.plain_result("设备码申请成功，但响应中没有 device_id。")
            return

        interval = int(
            data.get("interval") or self._cfg("login_poll_interval_seconds", 3)
        )
        expires_in = int(
            data.get("expires_in_s")
            or data.get("expires_in")
            or self._cfg("login_timeout_seconds", 420)
        )
        link = str(data.get("verification_uri") or data.get("verificationUri") or "")
        qr_code = str(data.get("qr_code") or data.get("qrcode") or "")

        lines = ["腾讯频道登录已创建，后台会自动轮询授权结果。"]
        if link:
            lines.append(f"授权链接：<{link}>")
        lines.append(f"有效期：{expires_in} 秒")

        if qr_code:
            try:
                base64.b64decode(qr_code, validate=False)
                yield event.chain_result([Comp.Image.fromBase64(qr_code)])
            except Exception:  # 兜底降级为文本提示，不中断登录输出
                logger.warning(f"[{PLUGIN_NAME}] login qr_code is not valid base64")
        yield event.plain_result("\n".join(lines))

        self._login_task = asyncio.create_task(
            self._login_poll_loop(event, device_code, device_id, interval, expires_in)
        )

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("tools")
    async def txcm_tools(
        self,
        event: AstrMessageEvent,
        query: GreedyStr = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """列出腾讯频道 MCP 工具。"""
        try:
            tools = await self._list_mcp_tools()
        except TencentChannelError as exc:
            yield event.plain_result(f"获取工具列表失败：{exc}")
            return

        keyword = str(query or "").strip().lower()
        lines = []
        for tool in tools:
            name = str(tool.get("name") or "")
            desc = str(tool.get("description") or "")
            if keyword and keyword not in name.lower() and keyword not in desc.lower():
                continue
            lines.append(f"- {name}: {desc[:80]}")
            if len(lines) >= 60:
                lines.append("... 已截断，请加关键词过滤")
                break
        yield event.plain_result("\n".join(lines) if lines else "未找到匹配工具。")

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("cli")
    async def txcm_cli(
        self,
        event: AstrMessageEvent,
        query: GreedyStr = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """列出 CLI 命令与 MCP 映射。"""
        keyword = str(query or "").strip().lower()
        lines = []
        for command, item in CLI_COMMANDS.items():
            text = " ".join(
                [
                    command,
                    str(item.get("tool") or ""),
                    str(item.get("group") or ""),
                    str(item.get("risk") or ""),
                    str(item.get("description") or ""),
                    str(item.get("note") or ""),
                ]
            ).lower()
            if keyword and keyword not in text:
                continue
            tool = item.get("tool") or "本地流程"
            lines.append(
                f"- {command} -> {tool} [{item.get('group')}/{item.get('risk')}] "
                f"{item.get('description')}"
            )
            if item.get("note"):
                lines.append(f"  {item['note']}")
            if len(lines) >= 90:
                lines.append("... 已截断，请加关键词过滤")
                break
        yield event.plain_result("\n".join(lines) if lines else "未找到匹配命令。")

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("map")
    async def txcm_map(
        self,
        event: AstrMessageEvent,
        command: GreedyStr = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看单条 CLI 命令映射。"""
        if not str(command or "").strip():
            yield event.plain_result("用法：/txcm map feed.publish-feed")
            return
        try:
            payload = self._cli_mapping_payload(str(command))
        except TencentChannelError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(_json_dumps(payload))

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("endpoints")
    async def txcm_endpoints(
        self,
        event: AstrMessageEvent,
        topic: GreedyStr = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看接口参考。"""
        try:
            payload = self._endpoint_payload(str(topic or ""))
        except TencentChannelError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(_json_dumps(payload))

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("schema")
    async def txcm_schema(
        self,
        event: AstrMessageEvent,
        tool_name: str,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看腾讯频道 MCP 工具 schema。"""
        try:
            text = await self.tool_get_tool_schema(tool_name)
        except TencentChannelError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(text)

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("list")
    async def txcm_list(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """列出当前账号已加入频道。"""
        try:
            payload = await self._list_guilds_payload()
        except TencentChannelError as exc:
            yield event.plain_result(f"获取频道列表失败：{exc}")
            return
        yield event.plain_result(self._format_guilds(payload["guilds"]))

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("call")
    async def txcm_call(
        self,
        event: AstrMessageEvent,
        tool_name: str,
        raw_json: GreedyStr = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """调用腾讯频道 MCP 原始工具。"""
        arguments = mcp_protocol.parse_json_text(str(raw_json or "{}"), fallback=None)
        if not isinstance(arguments, dict):
            yield event.plain_result(
                '参数必须是 JSON object，例如：/txcm call get_guild_info {"guildId":"..."}'
            )
            return

        try:
            result = await self.call_mcp_tool(tool_name, arguments)
        except TencentChannelError as exc:
            yield event.plain_result(f"调用失败：{exc}")
            return
        yield event.plain_result(_json_dumps(result))

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("ccall")
    async def txcm_ccall(
        self,
        event: AstrMessageEvent,
        command: str,
        raw_json: GreedyStr = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """按 CLI 命令名定位 MCP 工具并调用。"""
        arguments = mcp_protocol.parse_json_text(str(raw_json or "{}"), fallback=None)
        if not isinstance(arguments, dict):
            yield event.plain_result(
                '参数必须是 JSON object，例如：/txcm ccall feed.get-feed-detail {"feedId":"..."}'
            )
            return

        try:
            result = await self.tool_call_cli_command(command, arguments)
        except TencentChannelError as exc:
            yield event.plain_result(f"调用失败：{exc}")
            return
        yield event.plain_result(result)

    @txcm.custom_filter(filter.PermissionTypeFilter, filter.PermissionType.ADMIN)
    @txcm.command("guide")
    async def txcm_guide(
        self,
        event: AstrMessageEvent,
        topic: GreedyStr = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看踩坑附录使用规则。"""
        yield event.plain_result(skill_guide_text(str(topic or "")))

    @txcm.command("skill_update")
    async def txcm_skill_update(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        """拉取官方 Skill 并调用模型本地化为插件内置技能。"""
        result = await self._localize_official_skill(force=True)
        if result.get("error"):
            yield event.plain_result(
                f"Skill 本地化失败（{result.get('stage') or 'unknown'}）：{result['error']}"
                "；现有技能保持不变。"
            )
            return
        if result.get("skipped"):
            yield event.plain_result(
                f"官方 Skill {result.get('official_version')} 未变化，本地化技能仍为"
                f" {result.get('localized_version')}。"
            )
            return
        yield event.plain_result(
            f"Skill 本地化完成：v{result.get('localized_version')}"
            f"（{', '.join(result.get('files', []))}）"
        )
