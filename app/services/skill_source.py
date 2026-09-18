"""官方 tencent-channel-community Skill 的下载、缓存与主题检索。

官方包由腾讯连接 COS 分发（connect.qq.com 302 跳转），版本以响应头
x-cos-meta-tcc-version 为准。缓存布局：

    <cache_dir>/manifest.json
    <cache_dir>/<version>/SKILL.md
    <cache_dir>/<version>/references/*.md

任何下载/解包失败都保留旧缓存，返回结果里带 error 字段供调用方降级。
"""

from __future__ import annotations

import io
import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

import aiohttp

try:  # 运行时走 AstrBot 日志；单测环境无 astrbot 时退回标准 logging
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

from ..core.constants import SKILL_UPDATE_CHECK_URL

CHECK_TTL_SECONDS = 24 * 3600
READ_CHAR_LIMIT = 6000

# 官方包内文件 → 检索主题名。SKILL.md 是总览，references/ 是分域参考。
OFFICIAL_TOPICS: dict[str, str] = {
    "overview": "SKILL.md",
    "feed": "references/feed-reference.md",
    "manage-guild": "references/manage-guild.md",
    "manage-member": "references/manage-member.md",
    "notification": "references/notification-reference.md",
}


def manifest_path(cache_dir: Path) -> Path:
    return cache_dir / "manifest.json"


def load_manifest(cache_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(manifest_path(cache_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("version") else None


def official_topics(cache_dir: Path) -> dict[str, dict[str, str]]:
    """列出缓存中可用的官方主题及其来源标注。"""
    manifest = load_manifest(cache_dir)
    if not manifest:
        return {}
    version_dir = cache_dir / str(manifest["version"])
    source = f"official {manifest['version']}"
    return {
        topic: {"file": rel, "source": source}
        for topic, rel in OFFICIAL_TOPICS.items()
        if (version_dir / rel).is_file()
    }


def official_files(cache_dir: Path) -> dict[str, str]:
    """读取全部可用官方主题的完整内容，供本地化改写。"""
    manifest = load_manifest(cache_dir)
    if not manifest:
        return {}
    version_dir = cache_dir / str(manifest["version"])
    files: dict[str, str] = {}
    for rel in OFFICIAL_TOPICS.values():
        path = version_dir / rel
        if not path.is_file():
            continue
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return files


def read_official_topic(
    cache_dir: Path, topic: str, limit: int = READ_CHAR_LIMIT
) -> str:
    """读取官方主题正文；过长截断并标注全文长度。"""
    manifest = load_manifest(cache_dir)
    if not manifest:
        return ""
    rel = OFFICIAL_TOPICS.get(str(topic or "").strip())
    if not rel:
        return ""
    try:
        text = (cache_dir / str(manifest["version"]) / rel).read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(text) > limit:
        text = (
            text[:limit]
            + f"\n…(已截断，全文 {len(text)} 字符；可按官方 references 拆分的更细主题再检索)"
        )
    return f"[来源: official {manifest.get('version')}] {text}"


def extract_skill_zip(
    data: bytes, cache_dir: Path, version: str, cli_version: str = ""
) -> dict[str, Any]:
    """把官方 zip 解包进 <cache_dir>/<version>/，清理旧版本目录并写 manifest。"""
    version_dir = cache_dir / version
    members: list[tuple[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                continue  # 跳过路径异常成员，官方包不受影响
            members.append((name, member))
        roots = {name.split("/")[0] for name, _ in members if "/" in name}
        # 官方包带顶层目录 tencent-channel-community/，解包时剥离公共前缀
        strip_root = len(roots) == 1 and all("/" in name for name, _ in members)
        for name, member in members:
            rel = name.split("/", 1)[1] if strip_root else name
            if not rel:
                continue
            target = version_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(member))
    if not (version_dir / "SKILL.md").is_file():
        raise zipfile.BadZipFile("官方包缺少 SKILL.md，疑似内容变更")
    cache_dir.mkdir(parents=True, exist_ok=True)
    for child in cache_dir.iterdir():
        if child.is_dir() and child.name != version:
            shutil.rmtree(child, ignore_errors=True)
    manifest = {
        "version": version,
        "cli_version": cli_version,
        "fetched_at": int(time.time()),
    }
    manifest_path(cache_dir).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


async def refresh_skill_cache(
    cache_dir: Path,
    session: aiohttp.ClientSession,
    proxy: str | None = None,
    *,
    force: bool = False,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """按 TTL/版本刷新官方 Skill 缓存。

    force=True 跳过 TTL 直接检测；失败时保留旧缓存并在结果里带 error。
    """
    result: dict[str, Any] = {
        "latest_version": None,
        "cached_version": None,
        "updated": False,
    }
    manifest = load_manifest(cache_dir)
    if manifest:
        result["cached_version"] = manifest.get("version")
    stale = (
        not manifest
        or not manifest.get("fetched_at")
        or time.time() - float(manifest["fetched_at"]) > CHECK_TTL_SECONDS
    )
    if not force and not stale:
        result["latest_version"] = result["cached_version"]
        result["skipped"] = "缓存仍在 TTL 内"
        return result
    try:
        async with session.head(
            SKILL_UPDATE_CHECK_URL,
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=True,
        ) as response:
            latest = response.headers.get("x-cos-meta-tcc-version") or ""
            cli_version = response.headers.get("x-cos-meta-tcc-cli-version") or ""
        if not latest:
            result["error"] = "响应缺少 x-cos-meta-tcc-version 头"
            return result
        result["latest_version"] = latest
        logger.debug(
            f"[txcm] 官方 Skill 版本检测：latest={latest} "
            f"cached={manifest.get('version') if manifest else None} force={force}"
        )
        if manifest and not force and latest == manifest.get("version"):
            manifest["fetched_at"] = int(time.time())
            manifest_path(cache_dir).write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            result["skipped"] = "已是最新"
            return result
        async with session.get(
            SKILL_UPDATE_CHECK_URL,
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            allow_redirects=True,
        ) as response:
            data = await response.read()
        if response.status != 200 or not data:
            result["error"] = f"下载失败 HTTP {response.status}"
            return result
        manifest = extract_skill_zip(data, cache_dir, latest, cli_version)
        logger.debug(f"[txcm] 官方 Skill 已下载解包：v{latest} bytes={len(data)}")
        result["cached_version"] = manifest["version"]
        result["updated"] = True
    except Exception as exc:  # 离线/坏包兜底：保留旧缓存，错误交给调用方降级
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result
