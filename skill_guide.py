"""插件踩坑附录：内置的腾讯频道使用规则文本（``/txcm guide``、``txcm_skill_read`` 的数据源）。

按主题拆段：topic 命中则只返回该段，未命中（或为空）时按 ``_DEFAULT_TOPIC_ORDER``
拼成一份完整说明。这些规则来自对上游 CLI / 官方 Skill 的实测结论，
改动前请先确认真实行为，不要凭文档推测。
"""

from __future__ import annotations

SECTIONS: dict[str, str] = {
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

# 未指定 topic 时的拼接顺序：先讲怎么登录/有什么风险，再讲具体命令族。
_DEFAULT_TOPIC_ORDER = (
    "login",
    "risk",
    "error_codes",
    "cli",
    "endpoint",
    "media",
    "guild",
    "member",
    "feed",
    "feed_type",
    "markdown",
    "inline",
    "pagination",
    "alter_feed",
    "del_reply",
    "join_guild",
    "dm",
    "share_url",
    "notification",
    "shortcut",
)


def skill_guide_text(topic: str = "") -> str:
    """按主题返回内置使用规则文本。

    Args:
        topic: 主题名（``SECTIONS`` 的键），为空或不认识时返回完整说明。

    Returns:
        规则文本。
    """
    key = str(topic or "").strip().lower()
    if key in SECTIONS:
        return SECTIONS[key]
    return "\n".join(SECTIONS[name] for name in _DEFAULT_TOPIC_ORDER)
