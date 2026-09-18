"""腾讯频道返回数据的解码、归一化与抽取。

本模块只做纯数据处理，不依赖 astrbot / aiohttp，因此可以直接单元测试。

背景（实测结论）：
- 频道名 / 频道号等字段是 base64 编码的 UTF-8 明文（如 ``bytesGuildName``）；
- 帖子正文、评论正文是 base64 包裹的 protobuf，可读文本以 length-delimited
  字符串形式嵌在里面；
- 评论 content 是一个富文本节点列表：顶层 ``#1``（可重复）是节点、``#4`` 是 IP 属地。
  节点内 ``#1`` 是类型、``#(类型+2)`` 是载荷：类型 1 = 文本、2 = @提及、3 = 卡片、
  4 = 表情。早期实现递归收集所有文本字段，会把属地、表情 id、卡片一起拼进正文，
  故评论走 :func:`decode_comment_content` 按结构解析（见该函数注释）；
- 表情名字表来自官方 Emoji 文档（``assets/qq_face_map.json``），官方声明该表**只有
  部分表情**，所以查不到名字时展示 id，绝不静默丢弃（type=2 的 emoji 直接用码点还原，
  不需要表）；
- 不同接口返回的帖子 id 字段名不一致（热门流是 ``id``，搜索是 ``feedId``），
  子频道 id 在热门流里埋在 ``share.channelShareInfo.channelSign.channelId``；
- 评论数组字段名是 ``vecComment``，评论作者在 ``postUser``。
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
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


def _is_emoji(char: str) -> bool:
    """判断字符是不是 emoji 一类的符号（含变体选择符与零宽连接符）。

    这类字符的 ``isprintable()`` 不一定为 True（零宽连接符 ZWJ 就是 False），
    但它们属于正文的一部分，不能因为"不是中文/数字"被丢掉。
    """
    code = ord(char)
    if code in (0x200D, 0xFE0E, 0xFE0F):  # ZWJ / 变体选择符
        return True
    return code >= 0x2000 and unicodedata.category(char) in {"So", "Sk"}


def _clean_text(text: str) -> str:
    """裁剪 protobuf 噪声并校验一段候选文本（中文 / 数字 / emoji 视为正文）。

    只含 ASCII 单词的片段不在这里放行：那类字节太容易撞上，交给
    :func:`_heuristic_text` 兜底。

    Args:
        text: 已解码的候选文本。

    Returns:
        可用的正文片段；判定为噪声时返回空串。
    """
    # protobuf 标签/长度字节常落到 ASCII 标点上，剪掉收尾的这类噪声
    text = text.strip().lstrip(_ASCII_NOISE)
    if not text:
        return ""
    if any(not (_is_emoji(ch) or ch.isprintable() or ch.isspace()) for ch in text):
        return ""
    if _CJK_RE.search(text):
        return text
    if text.isdigit() and len(text) >= 2:
        return text
    if any(_is_emoji(ch) for ch in text):
        return text
    return ""


def _as_printable_text(chunk: bytes) -> str:
    """判断一段载荷是不是可读文本（含中文 / 数字 / emoji），否则交由上层继续下钻。"""
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        text = chunk.decode("utf-8", errors="ignore")
        # 丢掉少量尾部残字节（例如被上游截断的正文）仍然可用，丢太多则视为二进制
        if len(text.encode("utf-8")) <= len(chunk) - 4:
            return ""
    return _clean_text(text)


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
        if (
            _CJK_RE.search(run)
            or (run.isdigit() and len(run) >= 2)
            or (run.isalpha() and run.islower() and len(run) >= 3)
        ):
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


# --------------------------------------------------------------------------- #
# 评论富文本解析：正文 / IP 属地 / 实体（表情、卡片、@提及）
# --------------------------------------------------------------------------- #
_COMMENT_NODE_FIELD = 1  # 顶层 #1（可重复）= 一个富文本节点
_COMMENT_LOCATION_FIELD = 4  # 顶层 #4 = IP 属地
_COMMENT_NODE_TYPE_FIELD = 1  # 节点内 #1 = 节点类型
# 实测规律：节点载荷字段号 = 节点类型 + 2（1→#3 文本、2→#4 提及、3→#5 卡片、4→#6 表情）
_COMMENT_PAYLOAD_OFFSET = 2
_NODE_TYPE_TEXT = 1
_NODE_TYPE_MENTION = 2
_NODE_TYPE_CARD = 3
_NODE_TYPE_FACE = 4
# 字段号大于它的"消息"基本是碰巧能当 varint 解出来的文本（emoji 就是这种情况）
_MAX_PLAUSIBLE_FIELD_NO = 512


@dataclass
class CommentContent:
    """评论 content 的解析结果。

    Attributes:
        body: 正文。实体按出现顺序内联成 ``[表情:汪汪]`` / ``[卡片:标题]`` / ``[@昵称]``，
            type=2 的 emoji 直接给真字符。
        location: IP 属地，未知时为空串。
        faces: 表情（``id`` / ``type`` / ``name``），id 永不丢。
        cards: 卡片（``url`` / ``title``）。url 不进正文，避免长链接污染检索。
        mentions: @提及（``tiny_id`` / ``name``）。
    """

    body: str
    location: str = ""
    faces: list[dict[str, str]] = field(default_factory=list)
    cards: list[dict[str, str]] = field(default_factory=list)
    mentions: list[dict[str, str]] = field(default_factory=list)


_FACE_MAP_PATH = Path(__file__).resolve().parents[2] / "assets" / "qq_face_map.json"
_face_maps: dict[str, dict[str, str]] | None = None


def _load_face_maps() -> dict[str, dict[str, str]]:
    """惰性加载表情名字表。

    表缺失或损坏时退化成空表：只影响"能不能显示名字"，不影响正文解析，
    更不会让插件报错。

    Returns:
        ``{"system": {id: 名字}, "emoji": {id: 标签}}``。
    """
    global _face_maps
    if _face_maps is None:
        try:
            raw = json.loads(_FACE_MAP_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _face_maps = {
            key: value
            for key, value in raw.items()
            if not key.startswith("_") and isinstance(value, dict)
        }
    return _face_maps


def emoji_char(emoji_id: Any) -> str:
    """把 type=2 表情的 id 还原成真实字符。

    官方 Emoji 模型里 type=2 的 id 就是 Unicode 码点（128170 → 💪），
    这部分因此不需要维护任何对照表。非法码点返回空串。

    Args:
        emoji_id: 载荷里的 id。

    Returns:
        对应字符；无法还原时为空串。
    """
    text = str(emoji_id or "").strip()
    if not text.isdigit():
        return ""
    code = int(text)
    if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF:
        return chr(code)
    return ""


def face_display(face_id: Any, face_type: Any) -> tuple[str, str]:
    """把一个表情载荷转成 ``(内联文本, 可读名字)``。

    策略：**id 永不丢**——名字表只是"更好读"，查不到就展示 id。官方文档声明
    表情表只有部分表情，所以"未知就退化成 id"是正常路径，不是异常。

    Args:
        face_id: 表情 id。
        face_type: 官方 EmojiType（1=系统表情，2=emoji 表情）。

    Returns:
        ``(内联文本, 可读名字)``；type=2 且无官方标签时，名字填入还原出的字符。
    """
    key = str(face_id or "").strip()
    kind = str(face_type or "").strip()
    maps = _load_face_maps()
    if kind == "2":
        char = emoji_char(key)
        if char:
            return char, maps.get("emoji", {}).get(key, "") or char
    name = maps.get("system", {}).get(key, "")
    if name:
        return f"[表情:{name}]", name
    return (f"[表情:{key}]" if key else "[表情]", "")


def _parse_fields_strict(data: bytes) -> list[tuple[int, int, Any]] | None:
    """严格解析 protobuf 顶层字段，结构不完整时返回 ``None``。

    与 :func:`_iter_length_delimited` 的宽松不同：这里要求每个字段声明的长度都精确
    落在缓冲区里。只有这样才能把"真嵌套消息"和"一段恰好能当消息解的文本"区分开
    —— emoji 的 4 个字节就能被解成一个字段号巨大的 varint 字段。

    Args:
        data: 待解析字节。

    Returns:
        ``[(字段号, wire_type, 载荷或 varint 值)]``；解析失败返回 ``None``。
    """
    fields: list[tuple[int, int, Any]] = []
    index = 0
    while index < len(data):
        tag, index = _read_varint(data, index)
        if tag is None:
            return None
        field_no, wire = tag >> 3, tag & 0x07
        if field_no == 0:
            return None
        if wire == 0:
            value, index = _read_varint(data, index)
            if value is None:
                return None
            fields.append((field_no, wire, value))
        elif wire == 2:
            length, index = _read_varint(data, index)
            if length is None or index + length > len(data):
                return None
            fields.append((field_no, wire, data[index : index + length]))
            index += length
        elif wire == 5:
            if index + 4 > len(data):
                return None
            index += 4
        elif wire == 1:
            if index + 8 > len(data):
                return None
            index += 8
        else:
            return None
    return fields or None


def _parse_message(data: bytes) -> list[tuple[int, int, Any]] | None:
    """把载荷当成嵌套消息解析；不像合法消息时返回 ``None``。"""
    fields = _parse_fields_strict(data)
    if fields is None:
        return None
    if any(field_no > _MAX_PLAUSIBLE_FIELD_NO for field_no, _, _ in fields):
        return None
    return fields


def _plain_text(payload: bytes) -> str:
    """把载荷直接当字符串解读（像合法消息、或不可打印时返回空串）。"""
    if _parse_message(payload) is not None:
        return ""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return _clean_text(text)


def _nested_text(payload: bytes) -> str:
    """在嵌套消息里取第一个可用的文本字段（用于 ``#3.1`` 这一层）。"""
    fields = _parse_message(payload)
    if fields is None:
        return ""
    for _field_no, wire, value in fields:
        if wire != 2:
            continue
        text = _plain_text(value)
        if text:
            return text
    return ""


def _node_text(node: bytes) -> str:
    """取一个文本节点（顶层 ``#1``，类型 1）的文本。

    实测节点结构是 ``#1 → #3``，而 ``#3`` 可能是字符串、也可能再套一层
    （``#3.1`` 才是字符串）。这里只认文本节点的载荷字段：回复节点里
    ``#1.6``（计数）之类的字段不会被当成正文。

    Args:
        node: 顶层 ``#1`` 的载荷。

    Returns:
        节点文本；取不到时返回空串。
    """
    fields = _parse_message(node)
    if fields is None:
        return ""
    text_field = _NODE_TYPE_TEXT + _COMMENT_PAYLOAD_OFFSET
    for field_no, wire, value in fields:
        if wire != 2 or field_no != text_field:
            continue
        text = _plain_text(value) or _nested_text(value)
        if text:
            return text
    return ""


def _string_pairs(data: bytes) -> dict[int, str]:
    """把 ``{字段号: 字符串}`` 形态的载荷读成字典。

    值本身还是消息、或解不出 UTF-8 时整体返回空字典（宁可不解析，也不猜）。

    Args:
        data: 实体节点的载荷。

    Returns:
        ``{字段号: 文本}``。
    """
    fields = _parse_message(data)
    if fields is None:
        return {}
    pairs: dict[int, str] = {}
    for field_no, wire, value in fields:
        if wire != 2:
            continue
        if _parse_message(value) is not None:
            return {}
        try:
            pairs[field_no] = value.decode("utf-8")
        except UnicodeDecodeError:
            return {}
    return pairs


def _node_type(node: bytes) -> int | None:
    """取富文本节点的类型（节点内 ``#1`` 的 varint）。

    Args:
        node: 顶层 ``#1`` 的载荷。

    Returns:
        节点类型；读不到时返回 ``None``。
    """
    fields = _parse_message(node)
    if fields is None:
        return None
    return next(
        (
            int(value)
            for field_no, wire, value in fields
            if field_no == _COMMENT_NODE_TYPE_FIELD and wire == 0
        ),
        None,
    )


def _node_entity(node: bytes) -> tuple[str, str, dict[str, str]] | None:
    """解析一个非文本节点（@提及 / 卡片 / 表情）。

    Args:
        node: 顶层 ``#1`` 的载荷。

    Returns:
        ``(类别, 内联文本, 结构化描述)``；解析不出内容时返回 ``None``。
    """
    node_type = _node_type(node)
    if node_type is None or node_type == _NODE_TYPE_TEXT:
        return None
    fields = _parse_message(node)
    payload = next(
        (
            value
            for field_no, wire, value in (fields or [])
            if wire == 2 and field_no == node_type + _COMMENT_PAYLOAD_OFFSET
        ),
        None,
    )
    if payload is None:
        return None
    pairs = _string_pairs(payload)
    if not pairs:
        return None

    if node_type == _NODE_TYPE_MENTION:
        tiny_id = pairs.get(1, "")
        name = pairs.get(2, "")
        inline = f"[@{name or tiny_id}]" if (name or tiny_id) else ""
        return "mention", inline, {"tiny_id": tiny_id, "name": name}
    if node_type == _NODE_TYPE_CARD:
        title = pairs.get(2, "")
        # url 只进结构化字段：正文里塞长链接会污染检索
        return (
            "card",
            f"[卡片:{title}]" if title else "[卡片]",
            {"url": pairs.get(1, ""), "title": title},
        )
    if node_type == _NODE_TYPE_FACE:
        face_id, face_type = pairs.get(1, ""), pairs.get(2, "")
        inline, name = face_display(face_id, face_type)
        return "face", inline, {"id": face_id, "type": face_type, "name": name}
    return None


def decode_comment_content(value: Any) -> CommentContent:
    """解析评论 / 回复的 content，分离正文、IP 属地与实体。

    实测结构（见模块 docstring）：顶层 ``#1``（可重复）= 富文本节点、``#4`` = IP 属地；
    节点内 ``#1`` = 类型，``#(类型+2)`` = 载荷。

    正文里实体按**出现顺序**内联成 ``[表情:汪汪]`` / ``[卡片:标题]`` / ``[@昵称]``
    （type=2 的 emoji 直接给真字符），同时把 id / url 等原始信息留在结构化字段里：
    名字表不全时展示 id，**不静默丢弃**。

    兜底策略：结构不认识（严格解析失败，例如上游截断）→ 退回
    :func:`decode_protobuf_text`，行为与旧版一致；结构认识但没有文本节点
    （例如整条评论只有一个表情）→ 正文就是空的，不回退，否则表情 id 又会变成正文。

    Args:
        value: 原始 content 字段，通常是 base64 包裹的 protobuf。

    Returns:
        :class:`CommentContent`。
    """
    if not isinstance(value, str) or not value.strip():
        return CommentContent(body="")
    text = value.strip()
    if len(text) < 8 or not _BASE64_RE.fullmatch(text):
        return CommentContent(body=decode_protobuf_text(text))
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=False)
    except (ValueError, TypeError):
        return CommentContent(body=decode_protobuf_text(text))

    fields = _parse_message(raw)
    if fields is None:
        return CommentContent(body=decode_protobuf_text(text))

    parts: list[str] = []
    faces: list[dict[str, str]] = []
    cards: list[dict[str, str]] = []
    mentions: list[dict[str, str]] = []
    location = ""
    node_seen = False
    for field_no, wire, payload in fields:
        if wire != 2:
            continue
        if field_no == _COMMENT_NODE_FIELD:
            node_seen = True
            node_type = _node_type(payload)
            if node_type in (None, _NODE_TYPE_TEXT):
                part = _node_text(payload)
                if part:
                    parts.append(part)
                continue
            entity = _node_entity(payload)
            if entity is None:
                continue
            kind, inline, described = entity
            if inline:
                parts.append(inline)
            if kind == "face":
                faces.append(described)
            elif kind == "card":
                cards.append(described)
            elif kind == "mention":
                mentions.append(described)
        elif field_no == _COMMENT_LOCATION_FIELD and not location:
            location = _plain_text(payload)

    if not node_seen:
        # 结构不认识（正文不挂在 #1 下）：退回通用解码，至少不丢正文
        return CommentContent(body=decode_protobuf_text(text), location=location)
    return CommentContent(
        body=_join_parts(parts),
        location=location,
        faces=faces,
        cards=cards,
        mentions=mentions,
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
        "guild_id": str(_lookup(raw, *GUILD_ID_KEYS) or ""),
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
    """把评论结构归一化（正文自动解析，IP 属地与实体分开返回）。

    Args:
        raw: 原始评论字典。

    Returns:
        含 ``author`` / ``author_id`` / ``content`` / ``location`` / ``faces`` /
        ``cards`` / ``mentions`` / ``create_time`` 的字典。
        ``faces`` / ``cards`` / ``mentions`` 可以为空列表；``location`` 未知时为空串。
    """
    if not isinstance(raw, dict):
        return {}
    author = raw.get("postUser") if isinstance(raw.get("postUser"), dict) else {}
    if not author and isinstance(raw.get("poster"), dict):
        author = raw["poster"]
    raw_content = _lookup(raw, "content", "richContents", "contents")
    if isinstance(raw_content, str):
        parsed = decode_comment_content(raw_content)
    else:
        # 富文本 dict 形态没有 protobuf 结构可拆，实体与属地都无从谈起
        parsed = CommentContent(body=first_text(raw_content))
    return {
        "author": str(author.get("nick") or _lookup(raw, "nickName") or ""),
        "author_id": str(author.get("tinyId") or ""),
        "content": parsed.body.strip(),
        "location": parsed.location,
        "faces": parsed.faces,
        "cards": parsed.cards,
        "mentions": parsed.mentions,
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
