"""skill_source 纯函数测试：主题映射、zip 解包安全、manifest 与截断。"""

from __future__ import annotations

import importlib
import io
import json
import pathlib
import sys
import time
import zipfile
from pathlib import Path

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

skill_source = importlib.import_module(f"{PLUGIN_DIR.name}.skill_source")


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def build_cache(tmp_path: Path, version: str = "1.1.5") -> Path:
    cache_dir = tmp_path / "skill"
    data = make_zip(
        {
            "SKILL.md": "# tencent-channel-community",
            "references/feed-reference.md": "feed 用法",
            "references/manage-member.md": "member 用法",
        }
    )
    skill_source.extract_skill_zip(data, cache_dir, version, "1.0.6")
    return cache_dir


def test_extract_writes_manifest_and_topics(tmp_path):
    cache_dir = build_cache(tmp_path)
    manifest = skill_source.load_manifest(cache_dir)
    assert manifest is not None
    assert manifest["version"] == "1.1.5"
    assert manifest["cli_version"] == "1.0.6"
    topics = skill_source.official_topics(cache_dir)
    # 只列实际解包成功的主题
    assert set(topics) == {"overview", "feed", "manage-member"}
    assert all(meta["source"].startswith("official ") for meta in topics.values())


def test_extract_skips_path_escape_and_requires_skill_md(tmp_path):
    data = make_zip({"../evil.txt": "x", "references/feed-reference.md": "y"})
    cache_dir = tmp_path / "skill"
    try:
        skill_source.extract_skill_zip(data, cache_dir, "9.9.9")
    except skill_source.zipfile.BadZipFile:
        pass  # 缺 SKILL.md 必须拒绝
    else:
        raise AssertionError("缺少 SKILL.md 的包应被拒绝")
    assert not (tmp_path / "evil.txt").exists()


def test_read_topic_and_truncation(tmp_path):
    cache_dir = build_cache(tmp_path)
    text = skill_source.read_official_topic(cache_dir, "feed")
    assert text.startswith("[来源: official 1.1.5]")
    assert "feed 用法" in text
    short = skill_source.read_official_topic(cache_dir, "feed", limit=5)
    assert "已截断" in short
    assert skill_source.read_official_topic(cache_dir, "nope") == ""
    assert skill_source.read_official_topic(tmp_path / "empty", "feed") == ""


def test_extract_strips_official_root_prefix(tmp_path):
    data = make_zip(
        {
            "tencent-channel-community/SKILL.md": "# tcc",
            "tencent-channel-community/references/feed-reference.md": "feed",
        }
    )
    cache_dir = skill_source.extract_skill_zip(data, tmp_path / "skill", "1.1.5")
    topics = skill_source.official_topics(tmp_path / "skill")
    assert set(topics) == {"overview", "feed"}


def test_refresh_reuses_fresh_cache(tmp_path):
    cache_dir = build_cache(tmp_path)
    manifest = json.loads(skill_source.manifest_path(cache_dir).read_text("utf-8"))
    manifest["fetched_at"] = time.time()
    skill_source.manifest_path(cache_dir).write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    class FakeSession:
        def head(self, *args, **kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("TTL 内不应发起网络请求")

    import asyncio

    result = asyncio.run(skill_source.refresh_skill_cache(cache_dir, FakeSession()))
    assert result["latest_version"] == "1.1.5"
    assert "TTL" in result.get("skipped", "")
    assert "error" not in result
