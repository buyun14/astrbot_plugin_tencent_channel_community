"""把官方 Skill 内容本地化为插件自带的 AstrBot 技能。

AstrBot 会索引插件目录下的 ``skills/<name>/SKILL.md`` 并原生提供给模型，
因此插件只负责：下载官方 zip（skill_source）→ 调用模型做本地化改写 →
写入 ``<插件目录>/skills/tencent-channel-community/``。

本地化的职责是修正官方文档与插件现实的偏差（CLI 命令名 ≠ MCP 工具名、
风险分级不一致、平台差异），任何失败都保留上一版本地化结果。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

SKILL_DIR_NAME = "tencent-channel-community"
SKILL_VERSION_KEY = "version"

# 允许写出的相对路径白名单，其余一律丢弃
ALLOWED_OUTPUT_PATHS = frozenset(
    {
        "SKILL.md",
        "references/feed-reference.md",
        "references/manage-guild.md",
        "references/manage-member.md",
        "references/notification-reference.md",
    }
)

SYSTEM_PROMPT = (
    "你是腾讯频道 Skill 文档的本地化助手。把官方 Skill（面向 tencent-channel-cli "
    "二进制与 OpenClaw 平台）改写为 AstrBot 插件版本：所有操作改由插件的 LLM 工具完成，"
    "不提安装/执行 CLI 二进制、PowerShell 或 OpenClaw。只输出一个 JSON object："
    '键为文件相对路径（"SKILL.md" 与 references/*.md），值为该文件完整 Markdown 内容，'
    "不要输出 JSON 以外的任何文字或代码围栏。SKILL.md 必须保留 YAML frontmatter，"
    "name 固定为 tencent-channel-community，description 一句话概括，version 填用户给定的版本。"
)

# 本地化必须落实的修正（来自对真实网关与工具清单的核对）
CORRECTION_RULES = """
1. 风险分级：modify_member_shut_up 与 del_feed / delete_channel / kick_guild_member /
   do_comment(type=0/2 删除) / do_reply(type=0/2 删除) / change_role_member(remove-admin) /
   leave_guild / deal_notice 同级，都是高风险操作，必须统一标注为高风险并提示先确认。
2. 分享链接只有一个 MCP 工具 get_share_url（帖子与频道共用，参数区分）；
   文档若出现 get_feed_share_url / get_guild_share_url 一律改为 get_share_url。
3. 工具名一律用 MCP 工具名（如 get_share_url、get_feed_comments），
   CLI 命令名（如 feed.get-share-url）只能作为映射表里的对照列。
4. 平台差异：AstrBot 插件没有通知订阅（notices-on / --ref 自动填充不可用），
   通知只能用 get_notice_list / get_interact_notice 等原子工具组合；删除文档中
   要求处理 setup_hint 或开启订阅的指引。
5. 写操作与高风险操作受插件配置 enable_write_tools / enable_high_risk_tools
   限制且默认关闭：指引必须提示先让管理员开启，否则调用直接被拒。
6. 翻页与参数坑保持原样但注明：get_feed_comments 的 channelSign 必须驼峰且
   pageSize 以 schema 为准（当前网关标最大 50；早期实测 30/50 曾被拒，拿不准先用小页）；get_search_guild_feed 的 searchType.type 必须为 0；
   get_guild_feeds 主键是 id 且 getType=2 常返回空。
7. 字段名以 MCP schema 为准（feedId/guildIds/keyWord 等驼峰），
   官方文档与 CLI 的 snake_case 不是 MCP 参数名；vector_search 是网关幽灵工具
   （tools/list 可见但调用 130001），文档与示例一律不要引用它。
"""


def build_localize_prompt(
    files: dict[str, str],
    official_version: str,
    cli_mapping_rows: list[str],
) -> str:
    """拼装本地化请求：修正规则 + 工具对照表 + 官方原文。"""
    mapping = "\n".join(f"- {row}" for row in cli_mapping_rows)
    parts = [
        f"官方版本：{official_version}。本地化后的 SKILL.md frontmatter version 必须是它。",
        "需要落实的修正规则：",
        CORRECTION_RULES,
        "插件实际可用的 CLI 命令 → MCP 工具对照（以此为准）：",
        mapping,
        "官方 Skill 原文（按文件分组）：",
    ]
    for name, content in files.items():
        parts.append(f"===== {name} =====\n{content}")
    return "\n\n".join(parts)


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_localized_payload(text: str) -> dict[str, str]:
    """从模型输出提取文件字典；非法输出抛 ValueError。"""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()
    match = _JSON_OBJECT_RE.search(raw)
    if not match:
        raise ValueError("模型输出中未找到 JSON object")
    data = json.loads(match.group(0))
    if not isinstance(data, dict) or not data.get("SKILL.md"):
        raise ValueError("模型输出缺少 SKILL.md")
    payload = {
        path: content
        for path, content in data.items()
        if path in ALLOWED_OUTPUT_PATHS and isinstance(content, str) and content.strip()
    }
    if "SKILL.md" not in payload:
        raise ValueError("SKILL.md 内容为空")
    return payload


def write_localized_skill(plugin_dir: Path, payload: dict[str, str]) -> Path:
    """把本地化结果写入插件自带技能目录（先写临时目录再整体替换）。"""
    skill_dir = plugin_dir / "skills" / SKILL_DIR_NAME
    tmp_dir = plugin_dir / "skills" / f".{SKILL_DIR_NAME}.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    for rel, content in payload.items():
        target = tmp_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    tmp_dir.rename(skill_dir)
    return skill_dir


_VERSION_RE = re.compile(r"^version:\s*(\S+)\s*$", re.MULTILINE)


def localized_skill_version(plugin_dir: Path) -> str | None:
    """读取当前已本地化技能的版本号；未生成为 None。"""
    skill_md = plugin_dir / "skills" / SKILL_DIR_NAME / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None
