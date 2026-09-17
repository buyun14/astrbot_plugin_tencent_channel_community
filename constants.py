"""插件级常量：配置键映射、工具清单、协议与端点默认值。

这些值被 main.py、mcp_client.py、cli_reference.py 共同引用，集中放一处是为了避免
"同一条信息写两遍、改了一处漏了一处"的漂移（端点 URL 就踩过这个坑）。
"""

from __future__ import annotations

PLUGIN_NAME = "astrbot_plugin_tencent_channel_community"
PLUGIN_VERSION = "v0.4.0"

DEFAULT_MCP_ENDPOINT = "https://graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp"
DEFAULT_AUTH_BASE_URL = (
    "https://connect.qq.com/http2rpc/gotrpc/noauth/"
    "trpc.group_pro.open_developer_console.OpenDeveloperConsoleV2Trpc"
)
DEFAULT_DEVICE_CODE_REQUEST_URL = f"{DEFAULT_AUTH_BASE_URL}/RequestDeviceCode"
DEFAULT_DEVICE_TOKEN_POLL_URL = f"{DEFAULT_AUTH_BASE_URL}/PollDeviceToken"

# 官方 Skill/CLI 更新检测端点（ENDPOINT_GUIDE 与 _check_skill_update 共用同一份）。
SKILL_UPDATE_CHECK_URL = "https://connect.qq.com/skills/tencent-channel-community.zip"

# 官方 tencent-channel-community Skill 版本，来源为 SKILL.md frontmatter version。
# 升级官方 Skill 后需手动同步此处，否则 /txcm status 的版本比对结果会失真。
SKILL_VERSION = "1.1.5"

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

# 高风险操作：默认关闭，需要显式开启 enable_high_risk_tools（这些集合里的工具同时也在写操作集合中）。
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
