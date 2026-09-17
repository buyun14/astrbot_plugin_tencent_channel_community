# 腾讯频道社区管理工具 (astrbot_plugin_tencent_channel_community)

通过 `tencent-channel-cli` 接口管理 QQ 频道。支持扫码授权、频道/成员/帖子操作、CLI 命令映射查询，以及供 LLM 自动调用的工具集。

## 环境要求

| 依赖 | 版本要求 | 说明 |
|------|----------|------|
| Python | >= 3.10 | |
| AstrBot | >= v4.16 | 指令 + LLM Tool |
| aiohttp | >= 3.9.0 | HTTP 客户端 |

**平台支持**: 全平台（无限制）

## 功能

- `/txcm` 指令集：登录、状态检查、工具列表、CLI 映射、接口参考、MCP 调用
- LLM Tool：查询 schema、列出频道、调用 MCP tool、查询 CLI 映射
- 扫码授权：`/txcm login` 创建授权链接和二维码，并自动轮询写回 Token
- 权限默认值：`/txcm` 指令和本插件 LLM Tools 默认管理员可用，可在 AstrBot WebUI 调整
- Gemini 兼容：复杂 MCP 参数通过 `arguments_json` 字符串传入

## 安装

### 两种方式

1. 在 AstrBot 插件市场搜索 `腾讯频道社区管理工具` 安装。
2. 在插件管理页面选择从链接安装，输入：

```text
https://github.com/piexian/astrbot_plugin_tencent_channel_community
```

## 配置

### 账号设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `qq_ai_connect_token` | string | 否 | QQ AI Connect Token，可通过 `/txcm login` 或 `/txcm token` 写入 |

### 连接设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `mcp_endpoint` | string | 是 | `tencent-channel-cli` 使用的 MCP 接口 |
| `request_timeout_seconds` | int | 否 | 请求超时时间 |
| `proxy` | string | 否 | HTTP 代理地址 |

### 工具设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `enable_write_tools` | bool | 否 | 启用发帖、评论、修改频道等写操作 |
| `enable_high_risk_tools` | bool | 否 | 启用删帖、踢人、退频道等高风险操作 |
| `cache_tool_schema` | bool | 否 | 缓存 `tools/list` 返回的工具 schema |

### 登录设置

| 配置项 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `device_code_request_url` | string | 是 | 设备码申请接口 |
| `device_token_poll_url` | string | 是 | 授权结果轮询接口 |
| `login_timeout_seconds` | int | 否 | 授权等待超时 |
| `login_poll_interval_seconds` | int | 否 | 自动轮询间隔 |
| `login_request_payload_json` | text | 否 | 设备码申请额外 JSON |
| `login_poll_payload_json` | text | 否 | 授权轮询额外 JSON |

## 使用

### 指令

```text
/txcm help
/txcm status
/txcm login
/txcm token <QQ_AI_CONNECT_TOKEN>
/txcm tools [关键词]
/txcm cli [关键词]
/txcm map feed.publish-feed
/txcm endpoints [topic]
/txcm schema get_my_join_guild_info
/txcm list
/txcm call <mcp_tool> <JSON>
/txcm ccall <domain.action> <JSON>
/txcm guide [topic]
```

### LLM Tool

| 工具 | 说明 |
|------|------|
| `txcm_status` | 检查 MCP 状态与官方 Skill 版本更新 |
| `txcm_list_tools` | 列出 MCP tools |
| `txcm_get_tool_schema` | 查看 MCP tool schema |
| `txcm_list_guilds` | 列出当前账号频道 |
| `txcm_call_tool` | 调用 MCP tool |
| `txcm_list_cli_commands` | 列出 CLI 命令映射 |
| `txcm_get_cli_mapping` | 查看单条 CLI 映射 |
| `txcm_call_cli_command` | 按 CLI 命令名调用 MCP tool |
| `txcm_endpoint_guide` | 查看接口参考 |
| `txcm_skill_guide` | 查看内置使用规则 |

`txcm_call_tool` 和 `txcm_call_cli_command` 使用 `arguments_json` 传入 MCP 参数：

```json
{
  "tool_name": "get_guild_info",
  "arguments_json": "{\"reqGuildInfos\":[{\"guildId\":\"123\"}]}"
}
```

工具失败时返回：

```json
{
  "ok": false,
  "error": "错误原因",
  "hint": "处理建议"
}
```

## 接口鉴权说明（v0.3.1 修正）

上游网关 `graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp` 对 Token 的携带位置要求**按方法区分**，实测结论如下：

| 方法 | URL query `?token=` | `Authorization: Bearer` 头 |
|------|--------------------|---------------------------|
| `initialize` | 必需 | **不要带**，带了返回 8011 `api info not exist` |
| `tools/list` | 必需 | **不要带**，带了返回 8011 `api info not exist` |
| `notifications/initialized` | 必需 | 不要带 |
| `tools/call` | 需要 | **必需**，不带会返回 151 `[oidb]登录态验证失败` |

因此 v0.3.0 只带 `Authorization` 头的做法会导致 initialize 直接失败，表现为「明明已扫码授权，却一直提示鉴权失败、请重新登录」。v0.3.1 改为：

1. 统一把 Token 放进 URL query（`_mcp_url`）；
2. 仅 `tools/call` 额外附加 `Authorization` 头（`_mcp_headers(with_authorization=True)`）；
3. `initialize` 之后补发 MCP 规范的 `notifications/initialized` 通知；
4. 对 oidb 登录态抖动、HTTP 429/5xx、超时等瞬时故障自动重试（默认 3 次，指数退避）；
5. 区分错误语义：`api info not exist`（130001）表示**网关没有这个工具/接口**（通常工具名拼错），不重试并直接提示核对工具名；`tools/list` 返回空列表时也会重试。

## 语义化工具（v0.4.0）

上游 55 个 MCP 工具都是 oidb 原语（位掩码 filter、base64 中文字段、全称枚举、
字段名不一致），直接透传给模型等于让它逐个踩坑。v0.4.0 在插件内包了一层
**面向模型的语义化只读工具**：

| 工具 | 作用 | 替模型吃掉的坑 |
|------|------|----------------|
| `txcm_list_guilds` | 我加入的频道列表 | base64 解码频道名/频道号、字段归一化 |
| `txcm_guild_channels` | 频道下的版块列表 | base64 解码版块名、解析嵌套结构 |
| `txcm_search_feeds` | 关键词搜帖子 | 自动补 `searchType.type=0`、字段归一、相关度排序 |
| `txcm_latest_feeds` | 主页帖子流（热门/最新） | 自动兜住 `getType=2` 返回空的情况 |
| `txcm_read_feed` | 帖子详情 + 评论 | 评论 `vecComment` + base64 protobuf 解码 |
| `txcm_ask_channel` | **组合动作**：搜索 → 读评论 → 排序 | 一步拿到带出处（作者+时间）的回答素材 |

`txcm_call_tool` / `txcm_call_cli_command` 仍然保留，作为访问全部 55 个原语的逃生舱。

配套改进：

- **全局限速**：`min_request_interval_ms`（默认 400ms）串行化并发调用，避免触发网关频率限制；
- **结果缓存**：`cache_ttl_seconds`（默认 300s）缓存频道列表 / 版块列表这类低频数据；
- **结构化优先**：MCP 返回若带 `structuredContent` 优先使用，不再只依赖
  `code(0): / message(返回信息):` 文本协议；
- **数据归一化**：`channel_data.py` 独立成纯函数模块（无 astrbot 依赖），可直接单测。

## 上游数据/接口约束（实测记录）

这些是踩出来的、无法从文档得知的行为，已固化进代码：

| 位置 | 约束 |
|------|------|
| `get_search_guild_feed` | `searchType.type` 必须为 `0`（`2` 返回空）；命中总数在 `unionResult.feedTotal`（**字符串**）；频道分享链接在 `aiSearchInfo.guildUrl` |
| `get_guild_feeds` | 帖子主键字段是 `id` 而不是 `feedId`；版块 id 埋在 `share.channelShareInfo.channelSign.channelId`；`getType=2`（最新）实测常返回空 |
| `get_feed_comments` | 评论数组字段名是 `vecComment`，作者在 `postUser`；`channelSign` **必须带**且必须是**驼峰** `guildId`/`channelId`（蛇形报 8010，缺失直接"请求失败"）；**`pageSize` 必须 ≤ 20**（30/50 被拒） |
| 中文字段 | `bytesGuildName` / 评论正文是 base64（正文是 protobuf），需解码；上游偶有**截断在字符中间**的情况，解码结果尾部可能带 1 个残字 |

## 测试

```bash
pip install pytest
python -m pytest tests/ -q
```

`tests/` 下三个文件：

- `test_channel_data.py` / `test_mcp_protocol.py` —— 纯函数测试，fixtures 全部来自真实接口返回，
  不需要 AstrBot 运行时；
- `test_mcp_wiring.py` —— MCP 层回归测试，用假 transport 断言 **Token 摆放位置**
  （query 给 initialize/notifications，`Authorization` 只给 tools/call）、
  `notifications/initialized` 被补发、瞬时故障会重试而 `api info not exist` 不重试、
  空结果不进缓存、schema 配置键可读、工具白名单与分发分支一致。
  该文件在缺少 `astrbot` 时自动跳过。

## CLI 对齐范围

- `feed` / `manage` 原子命令映射到 MCP tool，可用 `/txcm map <domain.action>` 查询。
- CLI 快捷命令会拆成原子 MCP tool 组合，不维护 CLI resume 状态机。
- 图片/视频上传涉及动态上传地址和分片协议，当前保留 MCP 原子工具和接口参考。
- CLI 的 `--ref` 通知编号机制依赖本地通知存储，插件侧需手动传入 feed_id/comment_id 等参数。

## Skill Guide Topics

`/txcm guide [topic]` 和 `txcm_skill_guide` 工具支持以下 topic：

| Topic | 说明 |
|-------|------|
| `login` | 登录与 Token 管理 |
| `risk` | 高风险工具与写操作权限 |
| `error_codes` | retCode 错误码表（8011/153/20047/130000/20006/100707） |
| `cli` | CLI 命令与 MCP tool 映射 |
| `endpoint` | 接口端点规则 |
| `guild` | 频道管理 |
| `member` | 成员操作、tiny_id、禁言 |
| `feed` | 帖子操作 |
| `feed_type` | 短贴/长贴类型、字数与媒体限制 |
| `markdown` | Markdown 发帖规则 |
| `inline` | 内联链接与 @语法 |
| `pagination` | 翻页字段名差异 |
| `alter_feed` | 编辑帖子媒体替换 |
| `del_reply` | 删除回复必填字段 |
| `join_guild` | 加入频道 7 种验证类型 |
| `dm` | 频道私信双模式 |
| `share_url` | 分享链接规则 |
| `notification` | 通知操作与插件限制 |
| `shortcut` | 快捷命令的原子组合方式 |
## 项目结构

```text
astrbot_plugin_tencent_channel_community/
├── main.py                 # 插件主体：工具注册、MCP 客户端、指令
├── channel_data.py         # 纯函数：解码 / 归一化 / 抽取 / 相关度（可单测）
├── mcp_protocol.py         # 纯函数：URL/请求头构造、凭据脱敏、失败分类（可单测）
├── tools/
│   ├── tencent_channel_tools.py
│   ├── schema.py
│   └── result.py
├── tests/
│   ├── test_channel_data.py
│   ├── test_mcp_protocol.py
│   └── test_mcp_wiring.py
├── conftest.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

## 支持

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [腾讯频道 Skill 仓库](https://github.com/tencent-connect/tencent-channel-community)
