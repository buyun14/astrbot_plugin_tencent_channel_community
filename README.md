# 腾讯频道社区管理工具 (astrbot_plugin_tencent_channel_community)

通过腾讯频道官方 MCP 接口管理 QQ 频道：扫码授权、频道/成员/帖子/评论操作，内置 LLM 工具与官方 Skill 检索。

## 环境要求

- AstrBot >= v4.16，Python >= 3.10，aiohttp >= 3.9.0

## 安装

- AstrBot 插件市场搜索「腾讯频道社区管理工具」；或从链接安装：
  `https://github.com/piexian/astrbot_plugin_tencent_channel_community`

## 快速开始

```text
/txcm login            # 扫码授权，自动写回 Token
/txcm status           # 连接、凭据与官方 Skill 版本检查
```

## 指令

```text
/txcm help                                  # 帮助
/txcm status                                # 状态
/txcm login | /txcm token <token>           # 授权
/txcm tools [关键词]                         # MCP 工具列表
/txcm schema <tool>                         # 工具 schema
/txcm call <tool> <JSON>                    # 调用 MCP 工具
/txcm cli [关键词] | /txcm map <cli命令>     # CLI 命令映射
/txcm ccall <domain.action> <JSON>          # 按 CLI 命令调用
/txcm endpoints [topic]                     # 接口参考
/txcm guide [topic]                         # 官方 Skill 与踩坑附录
/txcm skill_update                          # 拉取官方 Skill 并本地化为内置技能
/txcm list                                  # 已注册 LLM 工具
```

## LLM 工具

| 工具 | 说明 |
|------|------|
| `txcm_status` | 状态与官方 Skill 版本检查 |
| `txcm_list_tools` / `txcm_get_tool_schema` | 列出工具 / 查看 schema |
| `txcm_call_tool` | 调用 MCP 原始工具（`arguments_json` 传参） |
| `txcm_list_guilds` | 已加入频道列表（自动解码频道名） |
| `txcm_guild_channels` | 频道版块列表 |
| `txcm_search_feeds` / `txcm_latest_feeds` | 搜索帖子 / 主页帖子流 |
| `txcm_read_feed` | 帖子详情 + 评论（正文/IP 属地/表情/卡片/@提及分离） |
| `txcm_ask_channel` | 搜索→读评论→相关度排序的组合问答 |
| `txcm_list_cli_commands` / `txcm_get_cli_mapping` | CLI 命令映射 |
| `txcm_call_cli_command` | 按 CLI 命令名调用 MCP 工具 |
| `txcm_endpoint_guide` | 接口端点参考 |

## 配置

全部在 AstrBot WebUI 插件配置中维护，默认值见 `_conf_schema.json`，要点：

- `qq_ai_connect_token`：为空时先 `/txcm login` 或 `/txcm token` 写入
- `enable_write_tools` / `enable_high_risk_tools`：写操作与高风险操作默认关闭
- `mcp_endpoint` / `proxy` / 超时与登录轮询参数：一般保持默认

## 说明

- 插件内置本地化版官方 Skill（`skills/tencent-channel-community/`，AstrBot 原生索引
  并随系统提示词下发）；`/txcm skill_update` 拉取官方最新版并调用模型重新本地化
  （修正工具名/风险分级/平台差异），模型供应商经 `skill_localize_provider` 配置，
  留空用主 LLM；下载或本地化失败保留现有技能，不影响使用。
- 上游 55 个 MCP 工具为 oidb 原语；语义化工具替模型处理 base64/protobuf、
  位掩码 filter、分页等细节，`txcm_call_tool` 保留为访问全部原语的逃生舱。
- 鉴权与踩坑细节随 `/txcm guide` 内置附录下发（8011/151/130001 等）。
- 版本历史见 [CHANGELOG.md](CHANGELOG.md)。

## 测试

```bash
pip install pytest
python -m pytest tests/ -q
```

## 项目结构

```text
astrbot_plugin_tencent_channel_community/
├── main.py                 # 插件类：LLM 工具注册、/txcm 指令、语义化与检索工具
├── metadata.yaml           # AstrBot 插件元数据（加载器约定，留根）
├── _conf_schema.json       # WebUI 配置 schema（约定留根）
├── requirements.txt        # 依赖声明（约定留根）
├── assets/
│   └── qq_face_map.json    # 官方表情 id → 名字表（来源见文件内注释）
├── skills/                 # 内置本地化技能（AstrBot 原生索引，约定留根）
│   └── tencent-channel-community/
├── app/                    # FastAPI 风格分层包
│   ├── core/               # constants.py  errors.py
│   ├── models/             # cli_reference.py  skill_guide.py（知识数据表）
│   ├── services/           # mcp_client.py  device_login.py  skill_source.py  skill_localize.py
│   └── utils/              # channel_data.py  mcp_protocol.py（纯函数，可单测）
├── tools/                  # LLM 工具定义（tencent_channel_tools.py  schema.py）
├── tests/                  # pytest：纯函数 + 接线回归（conftest.py 在此）
└── CHANGELOG.md
```

插件类通过 mixin 组合能力：`mcp_client` / `device_login` 只提供方法，依赖 `main.py`
插件类的 `_cfg()` / `_set_cfg()` / `_save_config()` 与实例属性（会话、缓存、限速状态）。

## 支持

- [AstrBot 插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [腾讯频道 Skill 仓库](https://github.com/tencent-connect/tencent-channel-community)
