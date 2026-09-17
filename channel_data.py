"""腾讯频道返回数据的解码、归一化与抽取。

本模块只做纯数据处理，不依赖 astrbot / aiohttp，因此可以直接单元测试。

背景（实测结论）：
- 频道名 / 频道号等字段是 base64 编码的 UTF-8 明文（如 ``bytesGuildName``）；
- 帖子正文、评论正文是 base64 包裹的 protobuf，可读文本以 length-delimited
  字符串形式嵌在里面；
- 不同接口返回的帖子 id 字段名不一致（热门流是 ``id``，搜索是 ``feedId``），
  子频道 id 在热门流里埋在 ``share.channelShareInfo.channelSign.channelId``；
- 评论数组字段名是 ``vecComment``，评论作者在 ``postUser``。
"""

from __future__ import annotations

import base64
import re
from typing import Any

_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
# protobuf 的标签/长度字节常解成这些 ASCII 标点，收尾时会当噪声剪掉
_ASCII_NOISE = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
# protobuf 里可读字符串的近似形态：一段不含控制字符的可见字符（含中文/全角标点）
_READABLE_RUN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9\uff00-\uffef\u3000-\u303f]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TEXT_KEYS = ("contents", "content", "text", "title", "richContents", "feedContent")

GUILD_ID_KEYS = ("uint64GuildId", "guildId", "guild_id", "id")
GUILD_NESTED_KEYS = ("msgGuildInfo", "guildInfo")
USER_NESTED_KEYS = ("guildUserInfo", "userInfo", "poster", "postUser")


# --------------------------------------------------------------------------- #
# 基础解码
# --------------------------------------------------------------------------- #
def decode_bytes_text(value: Any) -> str:
    """解码 ``bytesXxx`` 形式的字段（base64 编码的 UTF-8 明文）。

    不是合法 base64 或解不出 UTF-8 时原样返回，保证对已经解码过的数据幂等。

    Args:
        value: 原始字段值。

    Returns:
        解码后的文本；无法识别时返回原值。
    """
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip()
    if len(text) < 8 or len(text) % 4 != 0 or not _BASE64_RE.fullmatch(text):
        return value
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return value
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return value


def decode_protobuf_text(value: Any) -> str:
    """从 base64 包裹的 protobuf 里抽出可读文本（评论正文）。

    先按 protobuf 结构真正解析（递归下钻嵌套消息），只接受可打印的文本字段；
    解析不出东西时退回"可见字符片段"启发式，保证不会比朴素做法更差。

    Args:
        value: 原始字段值，可能是 base64、也可能是普通文本。

    Returns:
        抽取出的可读文本。
    """
    if not isinstance(value, str) or not value:
        return ""
    text = value.strip()
    if len(text) < 8 or not _BASE64_RE.fullmatch(text):
        return value
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except (ValueError, TypeError):
        return value

    parts = _protobuf_texts(raw)
    if parts:
        return _join_parts(parts)
    return _heuristic_text(raw.decode("utf-8", errors="ignore"))


def _read_varint(data: bytes, index: int) -> tuple[int | None, int]:
    """读一个 protobuf varint，越界或超长时返回 ``(None, index)``。"""
    result = 0
    shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            return None, index
    return None, index


def _iter_length_delimited(data: bytes):
    """遍历 protobuf 里所有 length-delimited（wire type 2）字段的原始载荷。"""
    index = 0
    while index < len(data):
        tag, index = _read_varint(data, index)
        if tag is None:
            return
        wire = tag & 0x07
        if wire == 2:
            length, index = _read_varint(data, index)
            if length is None:
                return
            # 上游可能把正文截断，导致长度前缀超出剩余字节：按剩余字节取，别整个放弃
            end = min(index + length, len(data))
            yield data[index:end]
            index = end
        elif wire == 0:
            _, index = _read_varint(data, index)
        elif wire == 5:
            index += 4
        elif wire == 1:
            index += 8
        else:
            return


def _as_printable_text(chunk: bytes) -> str:
    """判断一段载荷是不是可读文本（含中文或纯数字），否则交由上层继续下钻。"""
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        text = chunk.decode("utf-8", errors="ignore")
        # 丢掉少量尾部残字节（例如被上游截断的正文）仍然可用，丢太多则视为二进制
        if len(text.encode("utf-8")) <= len(chunk) - 4:
            return ""
    # protobuf 标签/长度字节常落到 ASCII 标点上，剪掉收尾的这类噪声
    text = text.strip().lstrip(_ASCII_NOISE)
    if not text:
        return ""
    if any(not (ch.isprintable() or ch.isspace()) for ch in text):
        return ""
    if _CJK_RE.search(text):
        return text
    if text.isdigit() and len(text) >= 2:
        return text
    return ""


def _protobuf_texts(data: bytes, depth: int = 0) -> list[str]:
    """递归收集 protobuf 中的可读文本字段。"""
    if depth > 6:
        return []
    collected: list[str] = []
    for chunk in _iter_length_delimited(data):
        text = _as_printable_text(chunk)
        if text:
            collected.append(text)
        else:
            collected.extend(_protobuf_texts(chunk, depth + 1))
    return collected


def _join_parts(parts: list[str]) -> str:
    """拼接多个文本片段：中文接中文时直接相连，其余情况用空格分隔。"""
    result = ""
    for part in parts:
        cleaned = part.strip()
        if not cleaned:
            continue
        if result and not (_is_cjk(result[-1]) or _is_cjk(cleaned[0])):
            result += " "
        result += cleaned
    return result.strip()


def _heuristic_text(decoded: str) -> str:
    """朴素兜底：保留含中文的片段、纯数字片段和纯小写英文片段。"""
    kept: list[str] = []
    for run in _READABLE_RUN_RE.findall(decoded):
        if _CJK_RE.search(run):
            kept.append(run)
        elif run.isdigit() and len(run) >= 2:
            kept.append(run)
        elif run.isalpha() and run.islower() and len(run) >= 3:
            kept.append(run)
    if kept:
        return _join_parts(kept)
    return _READABLE_RUN_RE.sub(" ", decoded).strip()


def _is_cjk(char: str) -> bool:
    """判断字符是否为中日韩文字或全角标点。"""
    return bool(
        _CJK_RE.fullmatch(char)
        or ("\u3000" <= char <= "\u303f")
        or ("\uff00" <= char <= "\uffef")
    )


def first_text(node: Any) -> str:
    """从富文本结构里取纯文本（兼容 ``contents``/``textContent``/字符串）。

    Args:
        node: 任意富文本节点。

    Returns:
        拼出的纯文本。
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return decode_protobuf_text(node)
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, dict):
        if node.get("type") == 1 and isinstance(node.get("textContent"), dict):
            return str(node["textContent"].get("text") or "")
        for key in _TEXT_KEYS:
            if key in node:
                got = first_text(node[key])
                if got:
                    return got
        return ""
    if isinstance(node, list):
        returns = [first_text(item) for item in node]
        return " ".join(part for part in returns if part)
    return ""


def as_int(value: Any, default: int = 0) -> int:
    """宽松地把值转成 int（兼容字符串、浮点、None）。

    Args:
        value: 原始值。
        default: 转换失败时的默认值。

    Returns:
        转换结果。
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _lookup(node: Any, *keys: str) -> Any:
    """在字典（及若干常见嵌套层）里按顺序取第一个非空值。"""
    if not isinstance(node, dict):
        return None
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    for nested in GUILD_NESTED_KEYS + USER_NESTED_KEYS:
        child = node.get(nested)
        if isinstance(child, dict):
            for key in keys:
                value = child.get(key)
                if value not in (None, "", [], {}):
                    return value
    return None


# --------------------------------------------------------------------------- #
# 归一化
# --------------------------------------------------------------------------- #
def normalize_guild(raw: Any) -> dict[str, Any]:
    """把原始频道结构归一化成稳定字段。

    Args:
        raw: 原始频道字典。

    Returns:
        含 ``guild_id`` / ``name`` / ``guild_number`` / ``member_count`` /
        ``role`` / ``tiny_id`` 的字典。
    """
    if not isinstance(raw, dict):
        return {}
    info = raw.get("msgGuildInfo") if isinstance(raw.get("msgGuildInfo"), dict) else raw
    user = (
        raw.get("guildUserInfo") if isinstance(raw.get("guildUserInfo"), dict) else {}
    )
    name = _lookup(info, "bytesGuildName", "strGuildName", "guildName", "name")
    number = _lookup(info, "bytesGuildNumber", "strGuildNumber", "guildNumber")
    return {
        "guild_id": str(_lookup(raw, *GUILD_ID_KEYS) or ""),
        "name": decode_bytes_text(name) if isinstance(name, str) else str(name or ""),
        "guild_number": (
            decode_bytes_text(number) if isinstance(number, str) else str(number or "")
        ),
        "member_count": as_int(
            _lookup(info, "uint32MemberNum", "memberNum", "member_count")
        ),
        "role": as_int(
            _lookup(user, "uint32Role", "role") or _lookup(raw, "uint32Role", "role")
        ),
        "tiny_id": str(_lookup(user, "uint64Tinyid", "tinyId") or ""),
    }


def normalize_channel(raw: Any) -> dict[str, Any]:
    """把子频道（版块）结构归一化。

    Args:
        raw: 原始版块字典。

    Returns:
        含 ``channel_id`` / ``name`` 的字典。
    """
    if not isinstance(raw, dict):
        return {}
    name = _lookup(raw, "channelName", "bytesChannelName", "name", "channelSign")
    return {
        "channel_id": str(_lookup(raw, "channelId", "channel_id", "id") or ""),
        "name": decode_bytes_text(name) if isinstance(name, str) else str(name or ""),
    }


def _feed_channel_id(raw: dict[str, Any]) -> str:
    """从帖子结构里挖出子频道 id（不同接口埋点不同）。"""
    direct = _lookup(raw, "channelId", "channel_id")
    if direct:
        return str(direct)
    share = raw.get("share")
    if isinstance(share, dict):
        for key in ("channelShareInfo", "feedShareInfo"):
            node = share.get(key)
            if isinstance(node, dict):
                sign = node.get("channelSign")
                if isinstance(sign, dict) and sign.get("channelId"):
                    return str(sign["channelId"])
    return ""


def normalize_feed(raw: Any) -> dict[str, Any]:
    """把帖子结构归一化成稳定字段。

    兼容 ``id`` / ``feedId`` 两种主键命名，以及埋在 ``share`` 里的子频道 id。

    Args:
        raw: 原始帖子字典。

    Returns:
        含 ``feed_id`` / ``channel_id`` / ``title`` / ``content`` / ``author`` /
        ``author_id`` / ``create_time`` / ``comment_count`` / ``image_count`` 的字典。
    """
    if not isinstance(raw, dict):
        return {}
    poster = raw.get("poster") if isinstance(raw.get("poster"), dict) else {}
    title = first_text(_lookup(raw, "title"))
    content = first_text(_lookup(raw, "content", "contents", "feedContent"))
    return {
        "feed_id": str(_lookup(raw, "id", "feedId", "feed_id") or ""),
        "channel_id": _feed_channel_id(raw),
        "title": title.strip(),
        "content": content.strip(),
        "author": str(
            poster.get("nick") or _lookup(raw, "nickName", "nick", "author") or ""
        ),
        "author_id": str(poster.get("tinyId") or _lookup(raw, "tinyId") or ""),
        "create_time": as_int(_lookup(raw, "createTime", "create_time")),
        "comment_count": as_int(
            _lookup(raw, "commentCount", "comment_count", "totalCommentCount")
        ),
        "image_count": len(raw.get("images") or []),
    }


def normalize_comment(raw: Any) -> dict[str, Any]:
    """把评论结构归一化（正文自动做 protobuf 解码）。

    Args:
        raw: 原始评论字典。

    Returns:
        含 ``author`` / ``author_id`` / ``content`` / ``create_time`` 的字典。
    """
    if not isinstance(raw, dict):
        return {}
    author = raw.get("postUser") if isinstance(raw.get("postUser"), dict) else {}
    if not author and isinstance(raw.get("poster"), dict):
        author = raw["poster"]
    content = first_text(_lookup(raw, "content", "richContents", "contents"))
    return {
        "author": str(author.get("nick") or _lookup(raw, "nickName") or ""),
        "author_id": str(author.get("tinyId") or ""),
        "content": content.strip(),
        "create_time": as_int(_lookup(raw, "createTime", "create_time")),
    }


# --------------------------------------------------------------------------- #
# 抽取
# --------------------------------------------------------------------------- #
def _collect(node: Any, predicate) -> list[dict[str, Any]]:
    """递归找出所有满足 predicate 的字典（按身份去重，保持出现顺序）。"""
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if predicate(value) and id(value) not in seen:
                seen.add(id(value))
                found.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(node)
    return found


def extract_guilds(data: Any) -> list[dict[str, Any]]:
    """从任意响应里抽出频道条目（保持原始结构，供既有命令复用）。

    Args:
        data: 已解析的响应对象。

    Returns:
        原始频道字典列表。
    """

    def is_guild(value: dict[str, Any]) -> bool:
        keys = {str(key).lower() for key in value}
        has_name = bool(
            {"guildname", "strguildname", "bytesguildname", "uint32guildname"} & keys
        )
        has_id = bool({"guildid", "uint64guildid", "guild_id"} & keys)
        if not has_id:
            return False
        # 名字可能直接挂在字典上，也可能塞在 msgGuildInfo / guildUserInfo 这类嵌套里
        return has_name or any(
            nested in value for nested in ("msgGuildInfo", "guildInfo", "guildUserInfo")
        )

    return _collect(data, is_guild)


def extract_channels(data: Any) -> list[dict[str, Any]]:
    """从频道详情响应里抽出子频道列表。

    Args:
        data: 已解析的响应对象。

    Returns:
        原始子频道字典列表。
    """

    def is_channel(value: dict[str, Any]) -> bool:
        keys = {str(key).lower() for key in value}
        return bool({"channelid", "channel_id"} & keys) and bool(
            {"channelname", "byteschannelname", "name"} & keys
        )

    return _collect(data, is_channel)


def extract_feeds(data: Any) -> list[dict[str, Any]]:
    """从帖子流/搜索结果里抽出帖子条目。

    Args:
        data: 已解析的响应对象。

    Returns:
        原始帖子字典列表。
    """

    def is_feed(value: dict[str, Any]) -> bool:
        keys = {str(key).lower() for key in value}
        has_id = bool({"id", "feedid", "feed_id"} & keys)
        has_body = bool({"createtime", "create_time"} & keys) and bool(
            {"title", "content", "contents", "feedcontent", "share"} & keys
        )
        return has_id and has_body

    return _collect(data, is_feed)


def extract_comments(data: Any) -> list[dict[str, Any]]:
    """从评论响应里抽出评论条目（数组字段名是 ``vecComment``）。

    Args:
        data: 已解析的响应对象。

    Returns:
        原始评论字典列表。
    """
    if isinstance(data, dict):
        for key in ("vecComment", "vecComments", "commentList", "comments"):
            value = data.get(key)
            if isinstance(value, list) and value:
                return [item for item in value if isinstance(item, dict)]

    def is_comment(value: dict[str, Any]) -> bool:
        keys = {str(key).lower() for key in value}
        if {"postuser", "userinfo"} & keys:
            return True
        return bool({"content", "richcontents"} & keys) and bool(
            {"createtime", "commentid", "id"} & keys
        )

    return _collect(data, is_comment)


# --------------------------------------------------------------------------- #
# 相关度
# --------------------------------------------------------------------------- #
def keywords_of(text: str) -> list[str]:
    """把问题拆成检索用关键词（中文按 2-gram，英文/数字按词）。

    Args:
        text: 问题文本。

    Returns:
        去重后的关键词列表。
    """
    raw = str(text or "")
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", raw) if len(w) >= 2]
    cjk_chunks = re.findall(r"[\u4e00-\u9fff]+", raw)
    grams: list[str] = []
    for chunk in cjk_chunks:
        if len(chunk) == 1:
            grams.append(chunk)
            continue
        grams.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    ordered: list[str] = []
    for token in words + grams:
        if token not in ordered:
            ordered.append(token)
    return ordered


def relevance_score(text: str, keywords: list[str]) -> int:
    """统计文本命中的关键词个数（做简单加权：完整词 > 2-gram）。

    Args:
        text: 待评分文本。
        keywords: :func:`keywords_of` 产出的关键词。

    Returns:
        相关度分数。
    """
    lowered = str(text or "").lower()
    if not lowered:
        return 0
    score = 0
    for keyword in keywords:
        if keyword and keyword in lowered:
            score += 2 if len(keyword) >= 3 and not _CJK_RE.search(keyword) else 1
    return score


def rank_by_relevance(
    items: list[dict[str, Any]],
    question: str,
    *,
    text_keys: tuple[str, ...] = ("content", "title"),
) -> list[dict[str, Any]]:
    """按与问题的相关度给条目排序（分数相同保持原有顺序）。

    Args:
        items: 归一化后的条目列表。
        question: 用户问题。
        text_keys: 参与打分的字段。

    Returns:
        新列表，元素为 ``{"item": 原条目, "score": 分数}``，按分数降序。
    """
    keywords = keywords_of(question)
    scored = [
        {
            "item": item,
            "score": relevance_score(
                " ".join(str(item.get(key) or "") for key in text_keys), keywords
            ),
        }
        for item in items
    ]
    return sorted(scored, key=lambda row: -row["score"])
