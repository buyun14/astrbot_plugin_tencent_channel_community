"""channel_data 纯函数单测。

fixtures 全部取自真实接口返回（已脱敏，只保留结构特征）。
"""

from __future__ import annotations

import channel_data as cd

# ---- 真实样本 -------------------------------------------------------------- #
GUILD_NAME_B64 = "5ZCv57+U5rmW55WULeilv+WMl+W3peS4muWkp+Wtpg=="          # 启翔湖畔-西北工业大学
CHANNEL_NAME_B64 = "5pGE5b2x5Yy6"                                        # 摄影区
CHANNEL_NAME_B64_2 = "6ICD56CU5L+h5oGv"                                  # 考研信息
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


def test_decode_protobuf_text_recovers_chinese_and_room_number():
    assert cd.decode_protobuf_text(COMMENT_FULL) == "在游泳池西边有一栋小楼里319"


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
        cd.first_text({"contents": [{"type": 1, "textContent": {"text": "体育馆西侧"}}]})
        == "体育馆西侧"
    )
    assert cd.first_text({"contents": [{"type": 1, "textContent": {"text": "上"}}, {"type": 1, "textContent": {"text": "课"}}]}) == "上 课"
    assert cd.first_text(None) == ""
    assert cd.first_text({"unknown": 1}) == ""


def test_as_int_is_lenient():
    assert cd.as_int("42") == 42
    assert cd.as_int(42) == 42
    assert cd.as_int("abc", default=-1) == -1
    assert cd.as_int(None) == 0


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
    assert comment["content"] == "在游泳池西边有一栋小楼里319"


def test_normalize_channel_decodes_name():
    assert cd.normalize_channel({"channelId": "652774337", "channelName": CHANNEL_NAME_B64}) == {
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
        {"guildInfoList": [{"channelList": [{"channelId": "1", "channelName": CHANNEL_NAME_B64}]}]}
    )
    assert [c["channelId"] for c in channels] == ["1"]

    feeds = cd.extract_feeds(
        {
            "feeds": [
                {"id": "f1", "createTime": 1, "contents": [{"type": 1, "textContent": {"text": "hi"}}]},
                {"not_a_feed": True},
            ]
        }
    )
    assert [f["id"] for f in feeds] == ["f1"]

    comments = cd.extract_comments({"vecComment": [{"postUser": {"nick": "a"}, "content": "x"}]})
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
