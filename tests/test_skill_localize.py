"""skill_localize 纯函数测试：提示词拼装、输出解析、写盘与版本读取。"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

skill_localize = importlib.import_module(
    f"{PLUGIN_DIR.name}.app.services.skill_localize"
)


SKILL_MD = (
    "---\nname: tencent-channel-community\ndescription: x\nversion: 1.1.5\n---\n# tcc"
)
FEED_MD = "# feed 用法"


def test_parse_accepts_json_and_filters_paths():
    text = json.dumps(
        {
            "SKILL.md": SKILL_MD,
            "references/feed-reference.md": FEED_MD,
            "evil/../path": "x",
            "references/unknown.md": "y",
        }
    )
    payload = skill_localize.parse_localized_payload(text)
    assert set(payload) == {"SKILL.md", "references/feed-reference.md"}


def test_parse_strips_code_fence_and_rejects_missing_skill_md():
    fenced = "```json\n" + json.dumps({"SKILL.md": SKILL_MD}) + "\n```"
    assert "SKILL.md" in skill_localize.parse_localized_payload(fenced)
    try:
        skill_localize.parse_localized_payload(json.dumps({"other": "x"}))
    except ValueError:
        pass
    else:
        raise AssertionError("缺少 SKILL.md 应拒绝")


def test_write_localized_skill_replaces_atomically(tmp_path):
    plugin_dir = tmp_path
    first = skill_localize.write_localized_skill(
        plugin_dir,
        {"SKILL.md": SKILL_MD, "references/feed-reference.md": FEED_MD},
    )
    assert (first / "SKILL.md").is_file()
    # 第二次写入收窄文件集合，旧文件不应残留
    skill_localize.write_localized_skill(plugin_dir, {"SKILL.md": SKILL_MD})
    assert not (first / "references" / "feed-reference.md").exists()
    assert skill_localize.localized_skill_version(plugin_dir) == "1.1.5"
    assert skill_localize.localized_skill_version(tmp_path / "none") is None


def test_build_prompt_contains_rules_mapping_and_official_text():
    prompt = skill_localize.build_localize_prompt(
        {"SKILL.md": "官方原文"}, "1.1.5", ["feed.publish-feed -> publish_feed"]
    )
    assert "get_share_url" in prompt  # P1 修正规则
    assert "modify_member_shut_up" in prompt  # P1 风险分级规则
    assert "feed.publish-feed -> publish_feed" in prompt
    assert "官方原文" in prompt
    assert "1.1.5" in prompt
