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
├── main.py
├── tools/
│   ├── tencent_channel_tools.py
│   ├── schema.py
│   └── result.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

## 支持

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [腾讯频道 Skill 仓库](https://github.com/tencent-connect/tencent-channel-community)
