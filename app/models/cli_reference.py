"""tencent-channel-cli 与腾讯频道 MCP 网关的参考数据。

两个纯数据表，不含逻辑：

- ``CLI_COMMANDS``：官方 CLI 的 ``domain.action`` 命令与 MCP tool 的对应关系。
  ``tool`` 为空表示该命令是 CLI 本地流程（多步交互 / 本地状态机），插件侧不模拟，
  只在 ``note`` 里给出原子工具的等价组合方式。
- ``ENDPOINT_GUIDE``：``/txcm endpoints`` 展示的接口参考，含各端点的请求头/请求体示例。
  实测的鉴权约束（Token 放 query、只有 tools/call 带 Authorization）写在描述里，
  与 ``mcp_client`` 的实现保持一致。
"""

from __future__ import annotations

from typing import Any

from ..core.constants import (
    DEFAULT_DEVICE_CODE_REQUEST_URL,
    DEFAULT_DEVICE_TOKEN_POLL_URL,
    DEFAULT_MCP_ENDPOINT,
    POLL_DEVICE_TOKEN_OIDB,
    REQUEST_DEVICE_CODE_OIDB,
    SKILL_UPDATE_CHECK_URL,
)

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
        "risk": "high-risk-write",
        "group": "write",
        "description": "发表或删除评论",
    },
    "feed.do-reply": {
        "tool": "do_reply",
        "risk": "high-risk-write",
        "group": "write",
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
        "note": "bytesCookie 首次传空串即可（schema 标 required 但空串可用），分页回传网关返回的 cookie。",
        "group": "query",
        "risk": "read",
        "description": "查看我的腾讯频道列表",
    },
    "manage.get-user-info": {
        "note": "上游实测常返回『请求失败，请稍后重试』，疑似需特定上下文；失败改用 guild_member_search 或 get_guild_member_list。",
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
        "risk": "high-risk-write",
        "group": "write",
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
