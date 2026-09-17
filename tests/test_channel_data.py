"""channel_data 纯函数单测。

fixtures 全部取自真实接口返回（已脱敏，只保留结构特征）。
"""

from __future__ import annotations

import base64

import channel_data as cd

# ---- 真实样本 -------------------------------------------------------------- #
GUILD_NAME_B64 = "5ZCv57+U5rmW55WULeilv+WMl+W3peS4muWkp+Wtpg=="  # 启翔湖畔-西北工业大学
CHANNEL_NAME_B64 = "5pGE5b2x5Yy6"  # 摄影区
CHANNEL_NAME_B64_2 = "6ICD56CU5L+h5oGv"  # 考研信息
COMMENT_FULL = (
    "CioIARomCiTlnKjmuLjms7PmsaDopb/ovrnmnInkuIDmoIvlsI/mpbzph4wKDAgEMggKAzMxORIBMQ=="
)
COMMENT_TRUNCATED = (
    "CkIIARo+CjzlrabkuaDmoLzmlpfmnK/nmoTvvIzlnKjmuLjms7PmsaDml4HovrnpgqKr"
)


# ---- 解码 ------------------------------------------------------------------ #
def test_decode_bytes_text_decodes_base64_plaintext():
    assert cd.decode_bytes_text(GUILD_NAME_B64) == "启翔湖畔-西北工业大学"
    assert cd.decode_bytes_text(CHANNEL_NAME_B64) == "摄影区"
    assert cd.decode_bytes_text(CHANNEL_NAME_B64_2) == "考研信息"


def test_decode_bytes_text_is_idempotent_for_plain_text():
    assert cd.decode_bytes_text("今天下午三点上课") == "今天下午三点上课"
    assert cd.decode_bytes_text("npu0lakeside") == "npu0lakeside"
    assert cd.decode_bytes_text("abc") == "abc"


def test_decode_bytes_text_handles_empty_and_invalid():
    assert cd.decode_bytes_text(None) == ""
    assert cd.decode_bytes_text("") == ""
    # 长度是 4 的倍数且像 base64，但解出来不是 UTF-8 —— 必须原样返回
    assert cd.decode_bytes_text("ab+/") == "ab+/"


def test_decode_protobuf_text_recovers_chinese_text():
    # 通用解码器（只用于帖子正文兜底）会收集所有文本字段，所以也含 type=4 实体节点
    # 的载荷「319」（一个表情 id）。评论必须走 decode_comment_content，
    # 才不会把实体、IP 属地拼进正文——见下面的「评论正文 / IP 属地」用例。
    assert cd.decode_protobuf_text(COMMENT_FULL).startswith("在游泳池西边有一栋小楼里")


def test_decode_protobuf_text_survives_truncated_payload():
    """上游会把正文截断在字符中间，解码器不能因此整体放弃。"""
    text = cd.decode_protobuf_text(COMMENT_TRUNCATED)
    assert text.startswith("学习格斗术的，在游泳池旁边")


def test_decode_protobuf_text_keeps_plain_text():
    assert cd.decode_protobuf_text("今天下午三点上课") == "今天下午三点上课"
    assert cd.decode_protobuf_text("See you at C0702") == "See you at C0702"
    assert cd.decode_protobuf_text(None) == ""


# ---- 富文本 ----------------------------------------------------------------- #
def test_first_text_handles_rich_structures():
    assert cd.first_text("简单字符串") == "简单字符串"
    assert (
        cd.first_text(
            {"contents": [{"type": 1, "textContent": {"text": "体育馆西侧"}}]}
        )
        == "体育馆西侧"
    )
    assert (
        cd.first_text(
            {
                "contents": [
                    {"type": 1, "textContent": {"text": "上"}},
                    {"type": 1, "textContent": {"text": "课"}},
                ]
            }
        )
        == "上 课"
    )
    assert cd.first_text(None) == ""
    assert cd.first_text({"unknown": 1}) == ""


def test_as_int_is_lenient():
    assert cd.as_int("42") == 42
    assert cd.as_int(42) == 42
    assert cd.as_int("abc", default=-1) == -1
    assert cd.as_int(None) == 0


# ---- 评论正文 / IP 属地 / 实体 ----------------------------------------------- #
# fixture 按真实抓包的结构构造（可见文本做了替换，字段号与嵌套关系保持一致）：
#   顶层 #1（可重复）= 一个富文本节点，节点内 #1 = 类型、#(类型+2) = 载荷
#       类型 1 → #3 → #3.1 = 文本
#       类型 2 → #4 = {tiny_id, name}   @提及
#       类型 3 → #5 = {url, title}      链接卡片
#       类型 4 → #6 = {id, type}        表情（type: 1=系统表情 2=emoji）
#   顶层 #4 = IP 属地
def _varint(value: int) -> bytes:
    out = b""
    while True:
        byte = value & 0x7F
        value >>= 7
        out += bytes([byte | (0x80 if value else 0)])
        if not value:
            return out


def _field(field_no: int, payload: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(payload)) + payload


def _varint_field(field_no: int, value: int) -> bytes:
    return _varint(field_no << 3) + _varint(value)


def _node(node_type: int, payload: bytes) -> bytes:
    """一个富文本节点：``#1 = {#1: 类型, #(类型+2): 载荷}``。"""
    return _field(1, _varint_field(1, node_type) + _field(node_type + 2, payload))


def _text_node(text: str) -> bytes:
    """type=1 文本节点：``#1 → #3 → #3.1 = 文本``。"""
    return _node(1, _field(1, text.encode()))


def _face_node(face_id: str, face_type: str = "1") -> bytes:
    """type=4 表情节点：``#6 = {1: id, 2: EmojiType}``。"""
    return _node(4, _field(1, face_id.encode()) + _field(2, face_type.encode()))


def _link_node(url: str, title: str) -> bytes:
    """type=3 链接卡片节点：``#5 = {1: url, 2: 标题}``。"""
    return _node(3, _field(1, url.encode()) + _field(2, title.encode()))


def _mention_node(tiny_id: str, name: str) -> bytes:
    """type=2 @提及节点：``#4 = {1: tiny_id, 2: 昵称}``。"""
    return _node(2, _field(1, tiny_id.encode()) + _field(2, name.encode()))


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_decode_comment_content_separates_ip_location():
    """顶层 #4 是 IP 属地，不能粘在正文末尾（真实抓包的回归点）。"""
    raw = _text_node("挂个") + _text_node("🪜") + _field(4, "陕西".encode())
    parsed = cd.decode_comment_content(_b64(raw))
    assert parsed.body == "挂个🪜"
    assert parsed.location == "陕西"


def test_decode_comment_content_keeps_emoji_only_segment():
    """只有 emoji 的节点也必须保留（它的字节能被误判成 varint 字段）。"""
    parsed = cd.decode_comment_content(_b64(_text_node("🪜")))
    assert parsed.body == "🪜"
    assert parsed.location == ""


def test_decode_comment_content_annotates_faces_in_place():
    """表情不丢弃：按出现顺序内联成带标注形式，id 留在结构化字段里。"""
    raw = _text_node("哈哈哈哈") + _face_node("277") + _text_node("太真实了")
    parsed = cd.decode_comment_content(_b64(raw))
    assert parsed.body == "哈哈哈哈[表情:汪汪]太真实了"
    assert parsed.faces == [{"id": "277", "type": "1", "name": "汪汪"}]


def test_decode_comment_content_uses_char_for_unicode_emoji():
    """type=2 的 id 就是 Unicode 码点，直接还原真字符，不需要维护对照表。"""
    parsed = cd.decode_comment_content(_b64(_face_node("128523", "2")))
    assert parsed.body == "😋"
    assert parsed.faces[0]["id"] == "128523"
    assert parsed.faces[0]["type"] == "2"


def test_decode_comment_content_keeps_unknown_face_id():
    """官方表情表只有部分表情：查不到名字时展示 id，绝不丢弃。"""
    parsed = cd.decode_comment_content(_b64(_face_node("999999")))
    assert parsed.body == "[表情:999999]"
    assert parsed.faces == [{"id": "999999", "type": "1", "name": ""}]


def test_decode_comment_content_degrades_without_face_map(monkeypatch):
    """名字表缺失时只退化成显示 id，不能报错、更不能丢实体。"""
    monkeypatch.setattr(cd, "_face_maps", {})
    parsed = cd.decode_comment_content(_b64(_face_node("277")))
    assert parsed.body == "[表情:277]"
    assert parsed.faces[0]["id"] == "277"


def test_decode_comment_content_annotates_cards_without_url_in_body():
    """卡片只把标题放进正文，url 留在结构化字段（长链接会污染检索）。"""
    raw = _text_node("推荐一下老八院") + _link_node(
        "https://pd.qq.com/s/example", "某帖子标题"
    )
    parsed = cd.decode_comment_content(_b64(raw))
    assert parsed.body == "推荐一下老八院[卡片:某帖子标题]"
    assert "pd.qq.com" not in parsed.body
    assert parsed.cards == [
        {"url": "https://pd.qq.com/s/example", "title": "某帖子标题"}
    ]


def test_decode_comment_content_annotates_mentions():
    raw = _text_node("谢谢") + _mention_node("100000000000000001", "某同学")
    parsed = cd.decode_comment_content(_b64(raw))
    assert parsed.body == "谢谢[@某同学]"
    assert parsed.mentions == [{"tiny_id": "100000000000000001", "name": "某同学"}]


def test_decode_comment_content_keeps_entities_in_order():
    """多个实体要按原始顺序内联，不能都堆到末尾。"""
    raw = (
        _face_node("319")
        + _text_node("这话在理")
        + _link_node("https://pd.qq.com/s/example", "某帖子标题")
    )
    parsed = cd.decode_comment_content(_b64(raw))
    assert parsed.body == "[表情:比心]这话在理[卡片:某帖子标题]"


def test_decode_comment_content_falls_back_for_unknown_shape():
    """结构不认识时退回通用解码，保证不比旧行为更差。"""
    assert cd.decode_comment_content("今天下午三点上课").body == "今天下午三点上课"
    assert cd.decode_comment_content(None).body == ""
    # 被上游截断的样本解析不成合法消息 → 走通用解码，正文仍然拿得回来
    parsed = cd.decode_comment_content(COMMENT_TRUNCATED)
    assert parsed.body.startswith("学习格斗术的，在游泳池旁边")
    assert parsed.location == ""


# ---- 归一化 ----------------------------------------------------------------- #
def test_normalize_guild_real_shape():
    raw = {
        "uint64GuildId": 59441451636637955,
        "msgGuildInfo": {
            "bytesGuildName": GUILD_NAME_B64,
            "bytesGuildNumber": "bnB1MGxha2VzaWRl",
            "uint32MemberNum": 46884,
        },
        "guildUserInfo": {"uint32Role": 2, "uint64Tinyid": 123456},
    }
    guild = cd.normalize_guild(raw)
    assert guild["guild_id"] == "59441451636637955"
    assert guild["name"] == "启翔湖畔-西北工业大学"
    assert guild["guild_number"] == "npu0lakeside"
    assert guild["member_count"] == 46884
    assert guild["role"] == 2
    assert guild["tiny_id"] == "123456"


def test_normalize_feed_handles_hot_and_search_shapes():
    hot = {
        "id": "B_hot_1",
        "createTime": 1789642574,
        "commentCount": 27,
        "contents": [{"type": 1, "textContent": {"text": "启翔湖里有蛇在游"}}],
        "poster": {"nick": "直视我崽种", "tinyId": "9"},
        "share": {"channelShareInfo": {"channelSign": {"channelId": "652774337"}}},
        "images": [{"url": "a"}, {"url": "b"}],
    }
    search = {
        "feedId": "B_search_1",
        "channelId": "660605426",
        "createTime": 1789000000,
        "title": "【研途考研】西工大26考研暑期强化班",
        "content": "下班了",
        "nickName": "某机构",
    }

    hot_norm = cd.normalize_feed(hot)
    assert hot_norm["feed_id"] == "B_hot_1"
    assert hot_norm["channel_id"] == "652774337"
    assert hot_norm["content"] == "启翔湖里有蛇在游"
    assert hot_norm["author"] == "直视我崽种"
    assert hot_norm["comment_count"] == 27
    assert hot_norm["image_count"] == 2

    search_norm = cd.normalize_feed(search)
    assert search_norm["feed_id"] == "B_search_1"
    assert search_norm["channel_id"] == "660605426"
    assert search_norm["title"] == "【研途考研】西工大26考研暑期强化班"
    assert search_norm["content"] == "下班了"
    assert search_norm["author"] == "某机构"


def test_normalize_feed_tolerates_garbage():
    assert cd.normalize_feed(None) == {}
    assert cd.normalize_feed({"id": "x"})["feed_id"] == "x"


def test_normalize_comment_decodes_body_and_author():
    raw = {
        "postUser": {"nick": "rtgravia", "tinyId": "77"},
        "content": COMMENT_FULL,
        "createTime": 1789000000,
    }
    comment = cd.normalize_comment(raw)
    assert comment["author"] == "rtgravia"
    assert comment["author_id"] == "77"
    # 该样本末尾的 type=4 节点是「比心」表情（id 319）：不再当正文数字，改为内联标注
    assert comment["content"] == "在游泳池西边有一栋小楼里[表情:比心]"
    assert comment["faces"] == [{"id": "319", "type": "1", "name": "比心"}]
    assert comment["location"] == ""


def test_normalize_comment_exposes_location_and_entities():
    raw = {"content": _b64(_text_node("挂个") + _field(4, "陕西".encode()))}
    comment = cd.normalize_comment(raw)
    assert comment["location"] == "陕西"
    assert comment["faces"] == []
    assert comment["cards"] == []
    assert comment["mentions"] == []
    # 富文本 dict 形态没有 protobuf 结构可拆，属地与实体都只能是空
    plain = cd.normalize_comment({"content": {"contents": [{"text": "上课"}]}})
    assert plain["location"] == ""
    assert plain["faces"] == []


def test_normalize_channel_decodes_name():
    assert cd.normalize_channel(
        {"channelId": "652774337", "channelName": CHANNEL_NAME_B64}
    ) == {
        "channel_id": "652774337",
        "name": "摄影区",
    }


# ---- 抽取 ------------------------------------------------------------------- #
def test_extract_guilds_from_real_envelope():
    payload = {
        "msgRspSortGuilds": [
            {
                "uint64GuildId": 1,
                "msgGuildInfo": {"bytesGuildName": GUILD_NAME_B64},
                "guildUserInfo": {"uint32Role": 2},
            }
        ]
    }
    guilds = cd.extract_guilds(payload)
    assert len(guilds) == 1
    assert guilds[0]["uint64GuildId"] == 1


def test_extract_channels_and_feeds_and_comments():
    channels = cd.extract_channels(
        {
            "guildInfoList": [
                {"channelList": [{"channelId": "1", "channelName": CHANNEL_NAME_B64}]}
            ]
        }
    )
    assert [c["channelId"] for c in channels] == ["1"]

    feeds = cd.extract_feeds(
        {
            "feeds": [
                {
                    "id": "f1",
                    "createTime": 1,
                    "contents": [{"type": 1, "textContent": {"text": "hi"}}],
                },
                {"not_a_feed": True},
            ]
        }
    )
    assert [f["id"] for f in feeds] == ["f1"]

    comments = cd.extract_comments(
        {"vecComment": [{"postUser": {"nick": "a"}, "content": "x"}]}
    )
    assert len(comments) == 1

    assert cd.extract_comments({"vecComment": []}) == []
    assert cd.extract_comments({"other": 1}) == []


# ---- 相关度 ----------------------------------------------------------------- #
def test_keywords_of_mixes_cjk_bigrams_and_ascii_words():
    keywords = cd.keywords_of("大学生安全防卫学上课地点 C0702")
    assert "大学" in keywords and "地点" in keywords
    assert "c0702" in keywords
    assert len(keywords) == len(set(keywords))


def test_rank_by_relevance_puts_on_topic_first():
    items = [
        {"content": "今天天气不错", "title": "闲聊"},
        {"content": "这门课在游泳池旁边的小教室上", "title": "安全防卫学"},
    ]
    ranked = cd.rank_by_relevance(items, "安全防卫学在哪上课")
    assert ranked[0]["item"]["title"] == "安全防卫学"
    assert ranked[0]["score"] > ranked[1]["score"]


def test_rank_by_relevance_keeps_order_on_ties():
    items = [{"content": "aaa"}, {"content": "bbb"}]
    ranked = cd.rank_by_relevance(items, "完全无关的词")
    assert [row["item"]["content"] for row in ranked] == ["aaa", "bbb"]
