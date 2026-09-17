"""MCP 层回归测试：Token 摆放位置、握手补发 notifications/initialized。

这些是 v0.3.x 踩过的坑，用假 transport 固化住，避免回归。
需要 astrbot 运行时可导入；没有则整体跳过。
"""

from __future__ import annotations

import asyncio
import importlib
import pathlib
import sys

import pytest
from urllib.parse import parse_qs, urlsplit

pytest.importorskip("astrbot")

PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR.parent))

main = importlib.import_module(f"{PLUGIN_DIR.name}.main")

TOKEN = "tok:v1_abc"
ENDPOINT = "https://example.invalid/mcp"


def make_instance():
    inst = main.TencentChannelCommunityPlugin.__new__(
        main.TencentChannelCommunityPlugin
    )
    inst.config = {
        "account_settings": {"qq_ai_connect_token": TOKEN},
        "connection_settings": {"mcp_endpoint": ENDPOINT},
    }
    inst._session = None
    inst._server_info = None
    inst._tool_cache = None
    inst._login_task = None
    inst._throttle_lock = asyncio.Lock()
    inst._last_call_at = 0.0
    inst._data_cache = {}
    return inst


def install_fake_transport(inst, calls, *, tool_result=None):
    """替换 _post_json，记录每次调用的 method / url / headers。"""

    async def fake_post(url, payload, headers=None):
        calls.append((payload.get("method"), url, dict(headers or {})))
        method = payload.get("method")
        if method == "initialize":
            return {"result": {"serverInfo": {"name": "fake"}}}
        if method == "notifications/initialized":
            return {}
        return tool_result if tool_result is not None else {"result": {}}

    inst._post_json = fake_post


def test_initialize_sends_notification_and_never_uses_authorization_header():
    inst = make_instance()
    calls = []
    install_fake_transport(inst, calls)

    asyncio.run(inst._initialize_mcp())

    assert [method for method, _, _ in calls] == ["initialize", "notifications/initialized"]
    for method, url, headers in calls:
        assert parse_qs(urlsplit(url).query)["token"] == [TOKEN], f"{method} 必须把 token 放在 query"
        assert "Authorization" not in headers, f"{method} 不能带 Authorization 头"


def test_tools_call_adds_authorization_header_on_top_of_query_token():
    inst = make_instance()
    calls = []
    install_fake_transport(
        inst,
        calls,
        tool_result={
            "result": {
                "isError": False,
                "content": [
                    {"type": "text", "text": "code(0):0"},
                    {"type": "text", "text": 'message(返回信息):{"ok":true}'},
                ],
            }
        },
    )

    parsed = asyncio.run(inst.call_mcp_tool("get_guild_feeds", {"guildId": "1"}))

    tool_calls = [item for item in calls if item[0] == "tools/call"]
    assert len(tool_calls) == 1
    _, url, headers = tool_calls[0]
    assert parse_qs(urlsplit(url).query)["token"] == [TOKEN]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert parsed["code"] == 0
    assert parsed["message"] == {"ok": True}


def test_transient_failure_is_retried_then_succeeds():
    """第一次超时，第二次成功——重试必须真的发生。"""
    inst = make_instance()
    main.MCP_RETRY_BACKOFF_SECONDS = 0  # 测试里不真的等待
    attempts = {"count": 0}

    async def fake_post(url, payload, headers=None):
        if payload.get("method") == "initialize":
            return {"result": {"serverInfo": {}}}
        if payload.get("method") == "notifications/initialized":
            return {}
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise main.TencentChannelError("请求腾讯频道端点超时。")
        return {"result": {"isError": False, "content": [{"type": "text", "text": "code(0):0"}]}}

    inst._post_json = fake_post
    parsed = asyncio.run(inst.call_mcp_tool("get_guild_feeds", {"guildId": "1"}))
    assert attempts["count"] == 2
    assert parsed["code"] == 0


def test_non_retryable_error_is_not_retried():
    """api info not exist 表示工具名不对，不该浪费重试。"""
    inst = make_instance()
    attempts = {"count": 0}

    async def fake_post(url, payload, headers=None):
        if payload.get("method") == "initialize":
            return {"result": {"serverInfo": {}}}
        if payload.get("method") == "notifications/initialized":
            return {}
        attempts["count"] += 1
        return {
            "result": {
                "isError": True,
                "_meta": {"AdditionalFields": {"retCode": 130001, "errMsg": "api info not exist"}},
                "content": [{"type": "text", "text": "code(130001):130001"}],
            }
        }

    inst._post_json = fake_post
    with pytest.raises(main.TencentChannelError):
        asyncio.run(inst.call_mcp_tool("not_a_real_tool", {}))
    assert attempts["count"] == 1


def test_empty_cache_is_treated_as_miss():
    """空结果不能进缓存，否则一次偶发空响应会锁死 300 秒。"""
    inst = make_instance()
    inst._cache_set("guilds", [])
    assert inst._cache_get("guilds") is None
    inst._cache_set("guilds", [{"guild_id": "1"}])
    assert inst._cache_get("guilds") == [{"guild_id": "1"}]


def test_schema_declared_config_keys_are_readable():
    """schema 里声明的键必须在 CONFIG_PATHS 注册，否则 WebUI 设置永远不生效。"""
    assert main.CONFIG_PATHS["min_request_interval_ms"] == (
        "connection_settings",
        "min_request_interval_ms",
    )
    assert main.CONFIG_PATHS["cache_ttl_seconds"] == (
        "connection_settings",
        "cache_ttl_seconds",
    )

    inst = make_instance()
    inst.config["connection_settings"]["min_request_interval_ms"] = 1500
    inst.config["connection_settings"]["cache_ttl_seconds"] = 0
    assert inst._cfg("min_request_interval_ms") == 1500
    assert inst._cache_ttl() == 0.0  # 0 表示关闭缓存，不能被默认值覆盖


def test_declared_tool_names_match_registered_tools():
    """TXCM_LLM_TOOL_NAMES 白名单必须与实际注册的工具一一对应。"""
    inst = make_instance()
    registered: list[str] = []

    class FakeContext:
        def add_llm_tools(self, *tools):
            registered.extend(tool.name for tool in tools)

    inst.context = FakeContext()
    asyncio.run(inst.initialize())
    assert set(registered) == set(main.TXCM_LLM_TOOL_NAMES)


def test_every_declared_tool_has_a_dispatch_branch():
    """注册了工具却没写分发分支，会让模型调用时报"未知工具"。"""
    text = (PLUGIN_DIR / "tools" / "tencent_channel_tools.py").read_text(
        encoding="utf-8"
    )
    for name in main.TXCM_LLM_TOOL_NAMES:
        assert f'"{name}"' in text, f"{name} 缺少分发分支"
