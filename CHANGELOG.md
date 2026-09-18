# Changelog

## v0.4.0 (2026-09-18)

自 v0.3.0 起合入上游 PR #2、#3（dev 分支，压缩为一个版本发布）。

### 新增

- 语义化只读工具层：`txcm_guild_channels` / `txcm_search_feeds` / `txcm_latest_feeds` / `txcm_read_feed` / `txcm_ask_channel`；`txcm_list_guilds` 自动解码频道名
- `channel_data.py` / `mcp_protocol.py`：base64 与 protobuf 文本解析、字段归一化、凭据脱敏
- 全局限速 `min_request_interval_ms`（默认 400ms）与数据缓存 `cache_ttl_seconds`（默认 300s）
- CI：GitHub Actions（ruff==0.16.8 check / format --check + pytest）与 `ruff.toml` 规则集
- 插件内置本地化版官方 Skill（`skills/tencent-channel-community/`，AstrBot 原生技能体系自动索引）；
  `/txcm skill_update` 拉取官方最新版（版本取 `x-cos-meta-tcc-version` 头）并调用模型重新本地化
  （落实工具名/风险分级/平台差异修正），模型供应商经 `skill_localize_provider` 配置，留空用主 LLM；
  下载或本地化失败保留现有技能，不影响使用

### 修复

- MCP 鉴权按方法区分：Token 走 URL query，仅 `tools/call` 附加 `Authorization`（网关实测：query+头→成功；query 无头→oidb 151；带头的 initialize/tools/list→8011 api info not exist）。修复 v0.3.0 的 `/txcm status`、`/txcm tools`、`/txcm schema`、`txcm_list_tools` 全部 8011 故障
- `initialize` 后补发 `notifications/initialized`；瞬时故障（429/5xx/超时/oidb 抖动）自动重试；未知工具名不重试
- `/txcm cli` 无论关键词只输出一条命令的 `break` 缩进错误
- 评论正文与帖子级 IP 属地分离（protobuf 严格按结构解析），表情/卡片/@提及内联保留并输出 `faces` / `cards` / `mentions` 字段
- 清理 4.0 弃用 API（`sp.get/put` → `await sp.global_get/global_put`）与死代码；`qq_ai_connect_token` 标记 `secret: true`

### 变更

- `main.py`（2692 行）按职责拆分为 `mcp_client` / `device_login` / `constants` / `cli_reference` / `skill_guide` / `errors`，行为等价（AST 逐函数比对）
- 官方用法经内置本地化技能下发，不再注册自定义读取工具；`/txcm guide` 聚焦插件踩坑附录；
  `txcm_call_tool` / `txcm_call_cli_command` 描述注入鉴权要点
- 配置项与指令用法保持兼容

## v0.3.0 (2026-08-18)

- 补全官方 Skill 知识缺口与运行时增强；收紧限流检测复用、补充 Skill 版本检测错误提示（PR #1）
