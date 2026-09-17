from __future__ import annotations

import asyncio
import base64
import datetime
import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from . import channel_data as cdata
from . import mcp_protocol
from .tools import TencentChannelFunctionTool
from .tools.schema import (
    boolean_param,
    integer_param,
    object_parameters,
    string_param,
)

PLUGIN_NAME = "astrbot_plugin_tencent_channel_community"
PLUGIN_VERSION = "v0.4.0"
DEFAULT_MCP_ENDPOINT = "https://graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp"
DEFAULT_AUTH_BASE_URL = (
    "https://connect.qq.com/http2rpc/gotrpc/noauth/"
    "trpc.group_pro.open_developer_console.OpenDeveloperConsoleV2Trpc"
)
DEFAULT_DEVICE_CODE_REQUEST_URL = f"{DEFAULT_AUTH_BASE_URL}/RequestDeviceCode"
DEFAULT_DEVICE_TOKEN_POLL_URL = f"{DEFAULT_AUTH_BASE_URL}/PollDeviceToken"
MCP_PROTOCOL_VERSION = "2024-11-05"
# 网关对凭证位置的要求与方法相关（实测）：
#   initialize / tools/list / notifications/initialized → 只认 URL query 上的 token，
#       附加 Authorization 头会返回 8011 "api info not exist"；
#   tools/call → 必须附加 Authorization 头，否则 oidb 层报 151 "登录态验证失败"。
MCP_MAX_ATTEMPTS = 3
MCP_RETRY_BACKOFF_SECONDS = 1.5
# 失败分类（哪些错误值得重试）集中在 mcp_protocol，便于与 _post_json 的文案对齐并单测。
REQUEST_DEVICE_CODE_OIDB = {"uint32_command": "0x995b", "uint32_service_type": "1"}
POLL_DEVICE_TOKEN_OIDB = {"uint32_command": "0x995d", "uint32_service_type": "1"}
TXCM_LLM_TOOL_NAMES = (
    "txcm_status",
    "txcm_list_tools",
    "txcm_get_tool_schema",
    "txcm_list_guilds",
    "txcm_guild_channels",
    "txcm_search_feeds",
    "txcm_latest_feeds",
    "txcm_read_feed",
    "txcm_ask_channel",
    "txcm_call_tool",
    "txcm_skill_guide",
    "txcm_list_cli_commands",
    "txcm_get_cli_mapping",
    "txcm_call_cli_command",
    "txcm_endpoint_guide",
)

# 配置键 -> (_conf_schema.json 中的分组名, 分组内的键名)。
# 新增配置键必须同时补进 _conf_schema.json 和这里，否则 WebUI 里改了也不生效。
CONFIG_PATHS = {
    "qq_ai_connect_token": ("account_settings", "qq_ai_connect_token"),
    "mcp_endpoint": ("connection_settings", "mcp_endpoint"),
    "request_timeout_seconds": ("connection_settings", "request_timeout_seconds"),
    "proxy": ("connection_settings", "proxy"),
    "min_request_interval_ms": ("connection_settings", "min_request_interval_ms"),
    "cache_ttl_seconds": ("connection_settings", "cache_ttl_seconds"),
    "enable_write_tools": ("tool_settings", "enable_write_tools"),
    "enable_high_risk_tools": ("tool_settings", "enable_high_risk_tools"),
    "cache_tool_schema": ("tool_settings", "cache_tool_schema"),
    "device_code_request_url": ("login_settings", "device_code_request_url"),
    "device_token_poll_url": ("login_settings", "device_token_poll_url"),
    "login_timeout_seconds": ("login_settings", "login_timeout_seconds"),
    "login_poll_interval_seconds": ("login_settings", "login_poll_interval_seconds"),
    "login_request_payload_json": ("login_settings", "login_request_payload_json"),
    "login_poll_payload_json": ("login_settings", "login_poll_payload_json"),
}

# 兜底默认值：只在配置文件中缺少该键时生效（正常情况由 _conf_schema.json 提供默认值）。
CONFIG_DEFAULTS = {
    "qq_ai_connect_token": "",
    "mcp_endpoint": DEFAULT_MCP_ENDPOINT,
    "request_timeout_seconds": 30,
    "proxy": "",
    "enable_write_tools": False,
    "enable_high_risk_tools": False,
    "cache_tool_schema": True,
    "min_request_interval_ms": 400,
    "cache_ttl_seconds": 300,
    "device_code_request_url": DEFAULT_DEVICE_CODE_REQUEST_URL,
    "device_token_poll_url": DEFAULT_DEVICE_TOKEN_POLL_URL,
    "login_timeout_seconds": 420,
    "login_poll_interval_seconds": 3,
    "login_request_payload_json": "{}",
    "login_poll_payload_json": "{}",
}

HIGH_RISK_TOOLS = {
    "del_feed",
    "delete_channel",
    "kick_guild_member",
    "leave_guild",
    "modify_member_shut_up",
    "do_comment",
    "do_reply",
    "deal_notice",
}

WRITE_TOOLS = {
    "alter_feed",
    "apply_media_upload",
    "batch_essence",
    "change_role_member",
    "create_channel",
    "create_guild",
    "create_guild_role_group",
    "deal_notice",
    "del_feed",
    "delete_channel",
    "do_comment",
    "do_feed_prefer",
    "do_like",
    "do_reply",
    "join_guild",
    "kick_guild_member",
    "leave_guild",
    "modify_channel",
    "modify_guild_number",
    "modify_guild_role_group",
    "modify_member_shut_up",
    "move_feed",
    "publish_feed",
    "push_essence_feed",
    "push_group_normal_dm_msg",
    "push_qq_msg",
    "report_user_guild_read_digest",
    "top_feed_action",
    "update_guild_info",
    "update_join_guild_setting",
    "upload_guild_avatar",
    "upload_guild_avatar_pre",
}

DEFAULT_GUILD_LIST_ARGUMENTS = {
    "bytesCookie": "",
    "filter": {
        "filter": {
            "uint32MemberNum": 1,
            "uint32GuildName": 1,
            "uint32Profile": 1,
            "uint32FaceSeq": 1,
            "uint32GuildNumber": 1,
            "uint32CreateTime": 1,
        },
        "userFilter": {"uint32Role": 1},
    },
}

CLI_COMMANDS: dict[str, dict[str, Any]] = {
    "feed.get-guild-feeds": {
        "tool": "get_guild_feeds",
        "group": "read",
        "risk": "read",
        "description": "获取腾讯频道主页帖子",
    },
    "feed.get-channel-timeline-feeds": {
        "tool": "get_channel_timeline_feeds",
        "group": "read",
        "risk": "read",
        "description": "获取版块帖子列表",
    },
    "feed.get-feed-detail": {
        "tool": "get_feed_detail",
        "group": "read",
        "risk": "read",
        "description": "查看帖子详情",
    },
    "feed.get-feed-comments": {
        "tool": "get_feed_comments",
        "group": "read",
        "risk": "read",
        "description": "查看帖子评论",
    },
    "feed.search-guild-feeds": {
        "tool": "get_search_guild_feed",
        "group": "read",
        "risk": "read",
        "description": "搜索频道内帖子",
    },
    "feed.get-feed-share-url": {
        "tool": "get_share_url",
        "group": "read",
        "risk": "read",
        "description": "获取帖子分享短链",
        "note": "CLI 会本地编码 businessParam；插件只暴露对应 MCP tool。",
    },
    "feed.get-notices": {
        "tool": "get_interact_notice",
        "group": "read",
        "risk": "read",
        "description": "查看互动消息",
    },
    "feed.get-next-page-replies": {
        "tool": "get_next_page_replies",
        "group": "read",
        "risk": "read",
        "description": "查看更多评论回复",
    },
    "feed.publish-feed": {
        "tool": "publish_feed",
        "group": "write",
        "risk": "write",
        "description": "发表帖子",
    },
    "feed.del-feed": {
        "tool": "del_feed",
        "group": "write",
        "risk": "high-risk-write",
        "description": "删除帖子",
    },
    "feed.do-comment": {
        "tool": "do_comment",
        "group": "write",
        "risk": "write",
        "description": "发表或删除评论",
    },
    "feed.do-reply": {
        "tool": "do_reply",
        "group": "write",
        "risk": "write",
        "description": "发表或删除回复",
    },
    "feed.do-like": {
        "tool": "do_like",
        "group": "write",
        "risk": "write",
        "description": "评论或回复点赞",
    },
    "feed.do-feed-prefer": {
        "tool": "do_feed_prefer",
        "group": "write",
        "risk": "write",
        "description": "帖子点赞或取消",
    },
    "feed.alter-feed": {
        "tool": "alter_feed",
        "group": "write",
        "risk": "write",
        "description": "编辑帖子",
    },
    "feed.top-feed": {
        "tool": "top_feed_action",
        "group": "write",
        "risk": "write",
        "description": "帖子置顶或取消置顶",
    },
    "feed.set-feed-essence": {
        "tool": "batch_essence",
        "group": "write",
        "risk": "write",
        "description": "设置或取消精华",
    },
    "feed.push-essence-feed": {
        "tool": "push_essence_feed",
        "group": "write",
        "risk": "write",
        "description": "推送精华帖通知",
    },
    "feed.move-feed": {
        "tool": "move_feed",
        "group": "write",
        "risk": "write",
        "description": "移动帖子到其他版块",
    },
    "feed.quick-publish": {
        "tool": "",
        "group": "shortcut",
        "risk": "write",
        "description": "选择频道和版块后一键发帖",
        "note": "CLI 本地多步流程；插件侧请用列表工具选择目标后调用 publish_feed。",
    },
    "feed.search-and-comment": {
        "tool": "",
        "group": "shortcut",
        "risk": "write",
        "description": "搜索帖子并评论",
        "note": "CLI 本地多步流程；插件侧请组合 get_search_guild_feed 与 do_comment。",
    },
    "feed.delete-and-mute": {
        "tool": "",
        "group": "shortcut",
        "risk": "high-risk-write",
        "description": "搜帖删帖并禁言",
        "note": "CLI 本地高风险流程；插件侧请显式确认后组合 del_feed 与 modify_member_shut_up。",
    },
    "feed.latest-feeds-detail": {
        "tool": "",
        "group": "shortcut",
        "risk": "read",
        "description": "获取最新帖子详情",
        "note": "CLI 本地多步流程；插件侧请组合 get_guild_feeds 与 get_feed_detail。",
    },
    "feed.hot-feeds-detail": {
        "tool": "",
        "group": "shortcut",
        "risk": "read",
        "description": "获取热门帖子详情",
        "note": "CLI 本地多步流程；插件侧请组合 get_guild_feeds 与 get_feed_detail。",
    },
    "manage.get-guild-info": {
        "tool": "get_guild_info",
        "group": "query",
        "risk": "read",
        "description": "查看腾讯频道资料",
    },
    "manage.get-my-join-guild-info": {
        "tool": "get_my_join_guild_info",
        "group": "query",
        "risk": "read",
        "description": "查看我的腾讯频道列表",
    },
    "manage.get-user-info": {
        "tool": "get_user_info",
        "group": "query",
        "risk": "read",
        "description": "查看用户资料",
    },
    "manage.get-guild-member-list": {
        "tool": "get_guild_member_list",
        "group": "query",
        "risk": "read",
        "description": "查看成员列表",
    },
    "manage.guild-member-search": {
        "tool": "guild_member_search",
        "group": "query",
        "risk": "read",
        "description": "按昵称搜索成员",
    },
    "manage.get-guild-channel-list": {
        "tool": "get_guild_channel_list",
        "group": "query",
        "risk": "read",
        "description": "查看版块列表",
    },
    "manage.search-guild-content": {
        "tool": "search_guild_content",
        "group": "query",
        "risk": "read",
        "description": "搜索腾讯频道、帖子或作者",
    },
    "manage.get-join-guild-setting": {
        "tool": "get_join_guild_setting",
        "group": "query",
        "risk": "read",
        "description": "查看腾讯频道加入设置",
    },
    "manage.get-guild-share-url": {
        "tool": "get_share_url",
        "group": "query",
        "risk": "read",
        "description": "获取腾讯频道分享短链",
    },
    "manage.get-share-info": {
        "tool": "get_share_info",
        "group": "query",
        "risk": "read",
        "description": "解析 pd.qq.com 分享链接",
    },
    "manage.kick-guild-member": {
        "tool": "kick_guild_member",
        "group": "write",
        "risk": "high-risk-write",
        "description": "踢出成员",
    },
    "manage.modify-member-shut-up": {
        "tool": "modify_member_shut_up",
        "group": "write",
        "risk": "write",
        "description": "禁言或解禁成员",
    },
    "manage.update-guild-info": {
        "tool": "update_guild_info",
        "group": "write",
        "risk": "write",
        "description": "修改腾讯频道名称或简介",
    },
    "manage.modify-guild-number": {
        "tool": "modify_guild_number",
        "group": "write",
        "risk": "write",
        "description": "修改腾讯频道号",
    },
    "manage.create-guild-role-group": {
        "tool": "create_guild_role_group",
        "group": "write",
        "risk": "write",
        "description": "创建身份组",
    },
    "manage.modify-guild-role-group": {
        "tool": "modify_guild_role_group",
        "group": "write",
        "risk": "write",
        "description": "修改身份组",
    },
    "manage.add-role-members": {
        "tool": "change_role_member",
        "group": "write",
        "risk": "write",
        "description": "向身份组添加成员",
    },
    "manage.remove-role-members": {
        "tool": "change_role_member",
        "group": "write",
        "risk": "high-risk-write",
        "description": "从身份组移除成员",
    },
    "manage.join-guild": {
        "tool": "join_guild",
        "group": "write",
        "risk": "write",
        "description": "加入腾讯频道",
    },
    "manage.create-channel": {
        "tool": "create_channel",
        "group": "write",
        "risk": "write",
        "description": "创建子版块",
    },
    "manage.delete-channel": {
        "tool": "delete_channel",
        "group": "write",
        "risk": "high-risk-write",
        "description": "删除版块",
    },
    "manage.modify-channel": {
        "tool": "modify_channel",
        "group": "write",
        "risk": "write",
        "description": "修改版块名称",
    },
    "manage.upload-guild-avatar": {
        "tool": "upload_guild_avatar",
        "group": "write",
        "risk": "write",
        "description": "修改腾讯频道头像",
    },
    "manage.create-theme-private-guild": {
        "tool": "create_guild",
        "group": "write",
        "risk": "write",
        "description": "创建公开或私密频道",
    },
    "manage.add-admin": {
        "tool": "change_role_member",
        "group": "write",
        "risk": "write",
        "description": "设置超级管理员",
        "note": "CLI 使用 change_role_member 并写死超级管理员 roleId=2。",
    },
    "manage.remove-admin": {
        "tool": "change_role_member",
        "group": "write",
        "risk": "high-risk-write",
        "description": "移除超级管理员",
        "note": "CLI 使用 change_role_member 并写死超级管理员 roleId=2。",
    },
    "manage.push-group-dm-msg": {
        "tool": "push_group_normal_dm_msg",
        "group": "write",
        "risk": "write",
        "description": "发送频道私信",
    },
    "manage.update-join-guild-setting": {
        "tool": "update_join_guild_setting",
        "group": "write",
        "risk": "write",
        "description": "修改腾讯频道加入设置",
    },
    "manage.leave-guild": {
        "tool": "leave_guild",
        "group": "write",
        "risk": "high-risk-write",
        "description": "退出腾讯频道",
    },
    "manage.notices-on": {
        "tool": "",
        "group": "write",
        "risk": "write",
        "description": "开启频道消息通知",
        "note": "CLI 本地订阅/OpenClaw 推送流程；AstrBot 插件不启动 CLI daemon。",
    },
    "manage.notices-off": {
        "tool": "",
        "group": "write",
        "risk": "write",
        "description": "关闭频道消息通知",
        "note": "CLI 本地订阅/OpenClaw 推送流程；AstrBot 插件不启动 CLI daemon。",
    },
    "manage.notices-status": {
        "tool": "",
        "group": "query",
        "risk": "read",
        "description": "查看频道消息通知状态",
        "note": "CLI 读取本地 ~/.qqcli/subscription 状态。",
    },
    "manage.check-notices": {
        "tool": "",
        "group": "query",
        "risk": "read",
        "description": "增量检查频道通知",
        "note": "CLI 本地流程会组合 query_user_guild_digest、get_interact_notice、get_notice_list 和 query_normal_dm_list。",
    },
    "manage.subscribe-notices": {
        "tool": "",
        "group": "write",
        "risk": "write",
        "description": "开启频道消息通知",
        "note": "notices-on 的兼容别名。",
    },
    "manage.unsubscribe-notices": {
        "tool": "",
        "group": "write",
        "risk": "write",
        "description": "关闭频道消息通知",
        "note": "notices-off 的兼容别名。",
    },
    "manage.check-new-notices": {
        "tool": "",
        "group": "query",
        "risk": "read",
        "description": "检查新的频道通知",
        "note": "check-notices 的兼容别名。",
    },
    "manage.get-recent-notices": {
        "tool": "",
        "group": "query",
        "risk": "read",
        "description": "获取最近的通知记录",
        "note": "CLI 读取本地通知记录。",
    },
    "manage.deal-notice": {
        "tool": "deal_notice",
        "group": "write",
        "risk": "write",
        "description": "处理系统通知",
    },
    "manage.notify-daemon": {
        "tool": "",
        "group": "write",
        "risk": "write",
        "description": "启动后台通知检查服务",
        "note": "CLI 本地 daemon；AstrBot 插件不启动外部进程。",
    },
    "manage.search-and-join": {
        "tool": "",
        "group": "shortcut",
        "risk": "write",
        "description": "搜索频道并加入",
        "note": "CLI 本地多步流程；插件侧请组合 search_guild_content 与 join_guild。",
    },
}

# 官方 Skill/CLI 更新检测端点（ENDPOINT_GUIDE 与 _check_skill_update 共用同一份）。
SKILL_UPDATE_CHECK_URL = "https://connect.qq.com/skills/tencent-channel-community.zip"

ENDPOINT_GUIDE: dict[str, dict[str, Any]] = {
    "login_request_device_code": {
        "method": "POST",
        "url": DEFAULT_DEVICE_CODE_REQUEST_URL,
        "headers": {"X-Oidb": REQUEST_DEVICE_CODE_OIDB},
        "body": {"device_id": "<uuid>"},
        "description": "申请扫码/授权链接设备码。",
    },
    "login_poll_device_token": {
        "method": "POST",
        "url": DEFAULT_DEVICE_TOKEN_POLL_URL,
        "headers": {"X-Oidb": POLL_DEVICE_TOKEN_OIDB},
        "body": {"device_id": "<uuid>", "device_code": "<device_code>"},
        "description": "轮询设备授权结果，成功时返回 Token。",
    },
    "mcp_json_rpc": {
        "method": "POST",
        "url": DEFAULT_MCP_ENDPOINT,
        "headers": {
            "Authorization": "Bearer <token>",
            "Content-Type": "application/json",
            "X-Forwarded-Method": "POST",
        },
        "query": {"token": "<token>"},
        "body": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "<tool_name>", "arguments": {}},
        },
        "description": (
            "所有 feed/manage 原子业务能力共用的 MCP JSON-RPC 端点。"
            "注意：Token 必须放在 URL query（?token=）；仅 tools/call 需要额外附加 "
            "Authorization 头（oidb 登录态），initialize / tools/list 附加该头会报 8011。"
            "initialize 后还需补发 notifications/initialized 通知。"
        ),
    },
    "media_sliceupload": {
        "method": "POST",
        "url": "http://<upload_host>:<upload_port>/sliceupload",
        "description": (
            "发帖/改帖上传图片或视频时由 apply_media_upload 返回动态上传地址，"
            "请求体是 CLI 内部编码的分片上传二进制协议。"
        ),
    },
    "skill_update_check": {
        "method": "HEAD",
        "url": SKILL_UPDATE_CHECK_URL,
        "description": "官方 Skill/CLI 更新检测端点，读取 x-cos-meta-tcc-version 等响应头。",
    },
}

# 官方 tencent-channel-community Skill 版本，来源为 SKILL.md frontmatter version。
# 升级官方 Skill 后需手动同步此处，否则 /txcm status 的版本比对结果会失真。
SKILL_VERSION = "1.1.5"


class TencentChannelError(Exception):
    """腾讯频道接口错误。

    Args:
        message: 可展示给管理员的错误信息。
        data: 上游返回的原始数据。
    """

    def __init__(self, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.data = data


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


def _normalize_tool_name(name: str) -> str:
    return str(name or "").strip().replace("-", "_")


_RATE_LIMIT_MARKERS = (
    "请求频率过高",
    "频率限制",
    "频率受限",
    "接口调用已超过申请的频率上限",
    "too many requests",
    "rate limit",
    "rate-limited",
)


def _is_transient_mcp_failure(detail: str) -> bool:
    """判断 MCP 调用失败是否属于可重试的瞬时故障（实现与标记见 mcp_protocol）。"""
    return mcp_protocol.is_transient_mcp_failure(detail)


def _is_rate_limit_payload(parsed: Any) -> bool:
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


def _parse_json_text(text: str, *, fallback: Any = None) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


# 插件元数据以 metadata.yaml 为准（优先级高于已废弃的 @register 装饰器），
# 这里不再重复声明名称/描述/版本，避免两处漂移。
class TencentChannelCommunityPlugin(Star):
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
                    "要看评论必须提供 channel_id（可用 txcm_search_feeds / txcm_latest_feeds 得到）。"
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
                name="txcm_skill_guide",
                description="读取内置腾讯频道 Skill 使用规则，帮助模型选择工具和控制风险。",
                parameters=object_parameters(
                    {
                        "topic": string_param(
                            "可选。guild/member/feed/notification/risk/login/cli/endpoint/media/shortcut。"
                        )
                    }
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
        except Exception as exc:
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
        except Exception as exc:  # noqa: BLE001 - 兜底脱敏，避免 token 随异常日志落盘
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
                if attempt < MCP_MAX_ATTEMPTS and _is_transient_mcp_failure(
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
                if attempt < MCP_MAX_ATTEMPTS and _is_transient_mcp_failure(
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
                if attempt < MCP_MAX_ATTEMPTS and _is_transient_mcp_failure(detail):
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
                parsed["message"] = _parse_json_text(raw_message, fallback=raw_message)

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
        if _is_rate_limit_payload(parsed):
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
        normalized = _normalize_tool_name(tool_name)
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
        """判断异常是否为限流错误，复用 _is_rate_limit_payload 保持一致。"""
        return _is_rate_limit_payload(
            getattr(exc, "data", None)
        ) or _is_rate_limit_payload(str(exc))

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
        return payload

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
        except Exception as exc:
            result["error"] = str(exc)
            result["error_hint"] = (
                f"官方版本检测失败：{type(exc).__name__}，如需详情请查看日志。"
            )
        return result

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

    def _skill_guide_text(self, topic: str = "") -> str:
        topic = str(topic or "").strip().lower()
        sections = {
            "risk": (
                "风险规则：del_feed、delete_channel、kick_guild_member、leave_guild、"
                "modify_member_shut_up、do_comment(type=0/2 删除)、do_reply(type=0/2 删除)、"
                "remove-admin(change_role_member 移除管理员)、deal_notice 等高风险工具默认禁用。"
                "写操作需要 enable_write_tools，高风险操作还需要 enable_high_risk_tools。"
                "管理员权限由 AstrBot 的指令和工具权限配置控制。"
            ),
            "login": (
                "登录规则：MCP 调用使用 QQ AI Connect Token，Token 统一放在 URL query"
                "（?token=）上鉴权；只有 tools/call 需要额外附加 HTTP Header "
                "Authorization: Bearer <token>（oidb 登录态），而 initialize / tools/list "
                "附加该头反而会报 8011 'api info not exist'。initialize 之后会补发 "
                "notifications/initialized 通知，并对频率限制、超时、连接失败、oidb 登录态抖动"
                "这类瞬时故障自动重试；但 api info not exist / 130001 属工具或接口不存在，不重试。/txcm token 可写入 Token；/txcm login 会直接请求腾讯连接设备"
                "授权端点，发送二维码/授权链接并自动轮询回写 Token。"
                "重试后仍报 8011 或'未登录'时，需重新 /txcm login 或 /txcm token。"
            ),
            "guild": (
                "频道管理：先用 txcm_list_guilds 获取当前账号频道；需要具体工具参数时，"
                "先调用 txcm_get_tool_schema，再用 txcm_call_tool 调用原始 MCP 工具。"
                "频道号（如 pd20589127）是展示标识，不能当作 guild_id 使用；"
                "获取 guild_id 可通过 get_share_info 解析分享链接或从 get_my_join_guild_info 提取。"
            ),
            "member": (
                "成员操作：@用户前必须先 guild_member_search 或 get_guild_member_list 查到 tiny_id，"
                "填入 at_users（id=tiny_id, nick=昵称），严禁用昵称或 QQ 号猜测（QQ 号≤10位，tiny_id>10位）。"
                "禁言 time_stamp 必须传绝对 Unix 时间戳（当前时间+时长秒数），0=立即解禁。"
                "禁言、踢人、移除管理员属于高风险操作。"
                "get-user-info 传参：{} 查自己全局资料，{guild_id} 查自己在频道内资料，"
                "{guild_id, tiny_id} 查他人在频道内资料。"
            ),
            "feed": (
                "帖子操作：浏览、详情、评论列表通常是读操作；发帖、改帖、删帖、评论、回复、"
                "置顶、精华、移动帖子属于写操作，其中删帖和评论/回复删除属于高风险操作。"
                "get-guild-feeds 必须传 get_type（1=热门 2=最新），翻页保持相同 get_type。"
                "search-guild-feeds 只能搜索已加入频道，未加入返回 retCode=130000。"
                "do-comment/do-reply 返回 retCode=20006 表示需先加入频道才能互动。"
            ),
            "notification": (
                "通知操作：处理加入申请、私信回复等需要先读取通知列表并保留通知上下文字段。"
                "deal_notice 属于高风险操作，默认禁用。"
                "注意：CLI 的 --ref 通知编号机制依赖 CLI 本地通知存储，插件侧无法复刻；"
                "插件调用 do_comment/do_reply/push_group_normal_dm_msg/deal_notice 时需手动传入 "
                "feed_id/comment_id/tiny_id 等参数，不支持编号自动填充。"
            ),
            "cli": (
                "CLI 对齐：/txcm cli 可列出 tencent-channel-cli 命令与 MCP tool 映射；"
                "/txcm map <domain.action> 查看单条映射；/txcm ccall <domain.action> <JSON> "
                "按 CLI 命令名定位 MCP tool。ccall 的 JSON 参数仍需使用 MCP schema。"
            ),
            "endpoint": (
                "端点规则：登录走 connect.qq.com 设备码接口；业务能力统一走 graph.qq.com MCP "
                "JSON-RPC tools/call；媒体上传会先 apply_media_upload 再访问动态 sliceupload 地址。"
            ),
            "media": (
                "媒体上传：CLI 会先 apply_media_upload 获取 uploadRsp.upload_addrs，再用内部二进制"
                "分片协议 POST 到 http://<host>:<port>/sliceupload，最后 apply_media_upload_status_sync。"
                "当前插件暴露这些 MCP 原子工具，但不复刻 sliceupload 二进制编码。"
            ),
            "shortcut": (
                "快捷命令：quick-publish、search-and-comment、delete-and-mute、search-and-join、"
                "latest-feeds-detail、hot-feeds-detail 是 CLI 本地多步交互流程（--resume-id 状态机）。"
                "插件侧不维护 resume 状态，需用原子工具组合执行："
                "quick-publish → get_my_join_guild_info + get_guild_channel_list + publish_feed；"
                "search-and-comment → get_search_guild_feed + do_comment；"
                "delete-and-mute → get_search_guild_feed + del_feed + modify_member_shut_up；"
                "search-and-join → search_guild_content + join_guild；"
                "latest/hot-feeds-detail → get_guild_feeds + get_feed_detail。"
            ),
            "pagination": (
                "翻页规则：翻页时严格用上次返回的字段名和值原样传回，不要跨命令复用翻页令牌。"
                "字段名差异：get-guild-feeds 用 feed_attach_info；"
                "get-channel-timeline-feeds 用 feed_attch_info（少个 a）；"
                "get-feed-comments/get-notices/get-next-page-replies 用 attach_info；"
                "search-guild-feeds 用 cookie（CLI flag 为 --next-page-cookie）。"
                "get-next-page-replies 首次 attach_info 从 get-feed-comments 的评论对象获取。"
            ),
            "markdown": (
                "Markdown 发帖规则：--content 是纯文本模式，后端不渲染 Markdown；"
                "内容含 Markdown 语法时必须用 --markdown-content（后端设置 is_markdown=true）。"
                "两者互斥不可同传。纯文本帖子不接受 --markdown-content，Markdown 帖子不接受 --content。"
                "alter-feed 编辑 Markdown 帖子时必须用 --markdown-content，否则报错。"
                "短贴（feed_type=1）的 --markdown-content 中禁止嵌入媒体语法（图片/视频），"
                "图片/视频必须通过 --image/--video 传入。"
            ),
            "inline": (
                "内联链接与@语法：发帖/评论/回复支持在 content 中内联写入。"
                "链接语法 [显示文字](https://url)；@语法 @[昵称](tinyid)，tinyid 通常>10位数字。"
                "Markdown 模式 @语法不同：[@昵称](mqqapi://markdown/mention?at_type=1&at_tinyid=<tinyid>)，"
                "Markdown 模式下传 --at-user 会被拦截。"
                "独立参数 --link url|显示文字、--at-user tinyid:昵称 可多次指定，追加在正文末尾。"
                "禁止在 content 中拼入裸 URL，裸 URL 在帖子里原样显示为纯文本不可点击。"
            ),
            "feed_type": (
                "帖子类型规则：feed_type=1 短贴（≤1000加权字，无标题，支持话题标签）；"
                "feed_type=2 长贴（>1000加权字，需标题，不支持话题标签）。"
                "加权字：中文/中文标点=1字，英文/数字/半角=0.5字。"
                "长贴传入话题参数会被 CLI 直接报错拦截。"
                "数量限制：短贴≤1000字/≤18图/≤1视频；长贴≤10000字/≤50图/≤5视频；评论回复≤1图。"
                "alter-feed 不接受 feed_type，帖子类型从原帖自动继承。"
            ),
            "alter_feed": (
                "编辑帖子规则：alter-feed 默认保留原帖所有图片/视频并追加新增内容。"
                "要替换时必须先清除：--clear-images 清除原图，--clear-videos 清除原视频，可连用 --image/--video。"
                "alter-feed 不接受 feed_type 参数。已有 CDN URL 时用 images 字段，每项字段名为 url 不是 picUrl。"
            ),
            "del_reply": (
                "删除回复必填字段：do-reply 删除（type=0/2）除 reply_id 外还需："
                "replier_id、feed_id、feed_author_id、feed_create_time、comment_id、"
                "comment_author_id、comment_create_time、guild_id、channel_id。"
                "do-comment 删除（type=0/2）和 do-reply 删除均为高风险，需 --yes。"
            ),
            "join_guild": (
                "加入频道规则：join-guild 内部自动预检加入设置。7种 JoinGuildType："
                "1=DIRECT 直接加入；2=ADMIN_AUDIT 需向用户收集 join_guild_comment 后再调用；"
                "4/5=QUESTION 需收集答案填入 join_guild_comment；"
                "6=MULTI_QUESTION 需 join_guild_answers(JSON数组)；7=QUIZ 需 join_guild_answers(JSON)。"
                "收到 need_verification 必须先展示问题给用户收集答案后才能再次调用，禁止编造答案。"
                "update-join-guild-setting：后3种高级类型（QUESTION/MULTI_QUESTION/QUIZ）需 stdin JSON 传 setting 对象。"
            ),
            "dm": (
                "频道私信规则：push-group-dm-msg 两种模式："
                "模式1（主动发私信）：先 guild_member_search 查 tiny_id，再传 peer_tiny_id + source_guild_id + text。"
                "source_guild_id 是发送者所在来源频道，不是目标用户所在频道。"
                "模式2（回复私信通知）：CLI 用 --ref 编号自动填充，插件侧需手动传 peer_tiny_id + source_guild_id。"
                "限制：对方未回复前只能发1条（retCode=100707）。严禁未获用户同意批量发送。"
            ),
            "share_url": (
                "分享链接规则：帖子列表不自动补取短链，需调 get_feed_share_url。"
                "get-feed-detail/publish-feed/alter-feed 自动补取帖子短链。"
                "帖子分享用 get_feed_share_url，频道分享用 get_guild_share_url。"
                "get_share_info 仅限 pd.qq.com 域名链接解析。"
                "输出 URL 时用 <链接> 包裹，不用 markdown 语法。"
            ),
            "error_codes": (
                "错误码表：8011=鉴权失败需重新登录；153=频率限制需等待约70秒后重试；"
                "20047=频道需先加入才能浏览；130000=搜索失败（未加入该频道）；"
                "20006=需加入频道后才能互动（评论/回复）；100707=私信限制（对方未回复前只能发1条）。"
                "鉴权失败时引导 /txcm login 或 /txcm token 重新写入凭证。"
            ),
        }
        if topic in sections:
            return sections[topic]
        return "\n".join(
            [
                sections["login"],
                sections["risk"],
                sections["error_codes"],
                sections["cli"],
                sections["endpoint"],
                sections["guild"],
                sections["member"],
                sections["feed"],
                sections["feed_type"],
                sections["markdown"],
                sections["inline"],
                sections["pagination"],
                sections["alter_feed"],
                sections["del_reply"],
                sections["join_guild"],
                sections["dm"],
                sections["share_url"],
                sections["notification"],
                sections["shortcut"],
            ]
        )

    # ------------------------------------------------------------------ #
    # 语义化只读工具（v0.4.0）：把 oidb 原语的坑（base64、位掩码、字段名不一致）
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
        if not guilds:
            raise TencentChannelError(
                "没取到频道列表。可能是 Token 失效（/txcm login 重新授权），"
                "也可能是上游返回结构变化导致解析不出频道"
                "（可用 txcm_call_tool 直接调用 get_my_join_guild_info 查看原始字段）。"
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
        """把原始评论转成精简结构（正文自动 protobuf 解码）。"""
        comment = cdata.normalize_comment(raw)
        return {
            "author": comment["author"],
            "time": _format_timestamp(comment["create_time"]),
            "content": comment["content"],
        }

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
        - ``pageSize`` 必须 <= 20，传 30/50 会被网关拒绝；
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
            try:
                payload["comments"] = await self._feed_comments(
                    fid, guild_id=guild_id, channel_id=str(channel_id or "").strip()
                )
            except TencentChannelError as exc:
                payload["note"] += (
                    f"评论获取失败：{exc}。可先用 txcm_search_feeds 或 txcm_latest_feeds "
                    "拿到 channel_id 后重试（评论接口需要 channelSign）。"
                )
        return _json_dumps(payload, 16000)

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
            desc = str(tool.get("description") or "")
            if keyword and keyword not in name.lower() and keyword not in desc.lower():
                continue
            rows.append({"name": name, "description": desc})
        return _json_dumps(rows)

    async def tool_get_tool_schema(self, tool_name: str) -> str:
        normalized = _normalize_tool_name(tool_name)
        tools = await self._list_mcp_tools()
        for tool in tools:
            if _normalize_tool_name(str(tool.get("name") or "")) == normalized:
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
                "note": "上游返回结构无法归一化，请用 txcm_call_tool 直接查看原始字段。",
            }
        )

    async def tool_call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        arguments_json: str = "",
    ) -> str:
        if arguments is None:
            arguments = _parse_json_text(str(arguments_json or "{}"), fallback=None)
        if not isinstance(arguments, dict):
            raise TencentChannelError(
                'arguments_json 必须是 JSON object 字符串，例如 {"guildId":"123"}。'
            )
        result = await self.call_mcp_tool(tool_name, arguments)
        return _json_dumps(result)

    async def tool_skill_guide(self, topic: str = "") -> str:
        return self._skill_guide_text(topic)

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
            arguments = _parse_json_text(str(arguments_json or "{}"), fallback=None)
        if not isinstance(arguments, dict):
            raise TencentChannelError(
                'arguments_json 必须是 JSON object 字符串，例如 {"guildId":"123"}。'
            )
        result = await self.call_mcp_tool(tool_name, arguments)
        return _json_dumps({"mapping": mapping, "result": result})

    async def tool_endpoint_guide(self, topic: str = "") -> str:
        return _json_dumps(self._endpoint_payload(topic))

    async def _request_device_code(self) -> dict[str, Any]:
        url = str(self._cfg("device_code_request_url", "") or "").strip()
        if not url:
            raise TencentChannelError(
                "未配置设备码申请端点，请在插件配置中填写或使用 /txcm token 写入 Token。"
            )

        payload = _parse_json_text(
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
        payload = _parse_json_text(
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
            payload = _parse_json_text(payload, fallback={"value": payload})
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
            inner = _parse_json_text(inner, fallback={"value": inner})
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
                try:
                    interval = int(next_interval)
                except (TypeError, ValueError):
                    pass
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
            "/txcm guide [topic] - 查看内置 Skill 指导"
        )

    @filter.command_group("txcm")
    def txcm(self):
        """腾讯频道社区管理工具指令组。"""
        pass

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
            except Exception:
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
        arguments = _parse_json_text(str(raw_json or "{}"), fallback=None)
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
        arguments = _parse_json_text(str(raw_json or "{}"), fallback=None)
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
        """查看内置 Skill 指导。"""
        yield event.plain_result(self._skill_guide_text(str(topic or "")))
