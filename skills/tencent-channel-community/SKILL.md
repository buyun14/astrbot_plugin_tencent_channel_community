---
name: tencent-channel-community
description: 通过插件 LLM 工具管理 QQ 频道：频道/成员/帖子/评论/点赞/私信/分享链接，含风险分级与参数坑。涉及腾讯频道操作时优先使用。
version: 1.1.5
---

# 腾讯频道社区管理（AstrBot 插件本地化版）

本技能随插件内置；插件会从 connect.qq.com 拉取官方原文并调用模型重新本地化。
所有操作用插件的 LLM 工具直接完成，**不需要安装 tencent-channel-cli**。
文档里出现的 CLI 命令（如 `feed.publish-feed`）仅作对照，实际执行用
`txcm_call_cli_command`（自动映射到 MCP 工具）或 `txcm_call_tool` 直调 MCP 工具。

## 工具选择

- 看有什么工具：`txcm_list_tools`；看参数：`txcm_get_tool_schema`
- 日常入口：`txcm_list_guilds`（我的频道，已解码频道名）→ `txcm_guild_channels`（版块）
- 帖子：`txcm_search_feeds`（关键词，优先）/ `txcm_latest_feeds`（主页流）/
  `txcm_read_feed`（详情+评论）/ `txcm_ask_channel`（组合问答）
- 原语逃生舱：`txcm_call_tool`；CLI 对照：`txcm_list_cli_commands` / `txcm_get_cli_mapping`

## 硬规则

1. **高风险操作**（默认禁用，需管理员开启 `enable_high_risk_tools`，执行前先向用户确认）：
   `del_feed`（删帖）、`delete_channel`（删版块）、`kick_guild_member`（踢人）、
   `modify_member_shut_up`（禁言）、`do_comment` / `do_reply` 的 `type=0/2`（删评论/回复）、
   `change_role_member`（移除管理员）、`leave_guild`（退频道）、`deal_notice`（处理通知）。
2. **写操作**（发帖 `publish_feed`、评论 `do_comment(type=1)`、回复 `do_reply(type=1)`、
   点赞 `do_like`、加频道 `join_guild` 等）默认关闭，需管理员开启 `enable_write_tools`，
   否则调用直接被拒——先提示开启再执行。
3. **通知**：插件无订阅能力（`notices-on` / `--ref` 自动填充不可用），不要尝试开启订阅；
   只用 `get_notice_list` / `get_interact_notice` 按需拉取。
4. **分享链接只有一个工具 `get_share_url`**（帖子与频道共用，参数区分）；
   `get_feed_share_url` / `get_guild_share_url` 不存在，调用会报 8011。
5. **参数字段名一律以 `txcm_get_tool_schema` 返回为准**（驼峰，如 `feedId`/`guildIds`/
   `keyWord`——注意 W 大写）；官方文档与 CLI 的 snake_case 不是 MCP 参数名。
   参数不确定先查 schema，不要编造参数值。`vector_search` 在网关上不可用，不要调用。
6. 错误处理：`8011`/`130001` api info not exist = 工具名或接口不存在（检查拼写）；
   `151` = 登录态失效（提示 `/txcm login`）；`153`/频率限制 = 等约 70 秒再试；
   `20047`/`130000`/`20006` = 需先加入频道。
7. **参数坑**：`get_feed_comments` 的 `channelSign` 必须驼峰 `guildId`/`channelId` 且
   `pageSize` 以 schema 为准（当前网关标默认 20、最大 50；早期实测 30/50 曾被拒，
   拿不准先用小页）；`get_search_guild_feed` 的 `searchType.type` 必须为 `0`（2 返回空）；
   `get_guild_feeds` 帖子主键是 `id` 且 `getType=2`（最新）常返回空；中文字段多为 base64，
   语义化工具已自动解码。

## 常用流程

| 目标 | 路径 |
|------|------|
| 我的频道 | `txcm_list_guilds` |
| 主页帖子 | `get_guild_feeds`（`getType=1` 热门）或 `txcm_latest_feeds` |
| 搜帖子 | `txcm_search_feeds` 或 `search_guild_content` |
| 帖子+评论 | `get_feed_detail` + `get_feed_comments`（或 `txcm_read_feed`） |
| 发帖/评论/点赞 | `publish_feed` / `do_comment` / `do_like`（写操作需先开启） |
| 成员 | `guild_member_search` / `get_guild_member_list` / `change_role_member`（高风险） |
| 私信 | `query_normal_dm_list` / `push_group_normal_dm_msg` |
| 分享 | `get_share_url`（生成）/ `get_share_info`（解析） |

## CLI 命令对照（节选）

| CLI 命令 | MCP 工具 |
|----------|----------|
| `feed.get-guild-feeds` | `get_guild_feeds` |
| `feed.get-feed-comments` | `get_feed_comments` |
| `feed.search-guild-feeds` | `get_search_guild_feed` / `search_guild_content` |
| `feed.publish-feed` | `publish_feed` |
| `feed.do-comment` | `do_comment` |
| `feed.get-share-url` | `get_share_url` |
| `manage.get-my-join-guild-info` | `get_my_join_guild_info` |
| `manage.get-guild-channel-list` | `get_guild_channel_list` |
| `manage.modify-member-shut-up` | `modify_member_shut_up`（高风险） |
| `manage.kick-guild-member` | `kick_guild_member`（高风险） |

完整对照：`/txcm map <domain.action>` 或 `txcm_list_cli_commands`。

## 参考

`references/` 下按域拆分的详细参考（feed / manage-guild / manage-member / notification），
原始语法面向 CLI，按本文件对照表换算成 MCP 工具名使用。
