"""mcp_protocol 纯函数单测：请求构造、凭据脱敏、失败分类。"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import mcp_protocol as mp

ENDPOINT = "https://graph.qq.com/mcp_gateway/open_platform_agent_mcp/mcp"
TOKEN = "bot:v1_exampleT0ken+/="


def test_url_carries_token_in_query():
    url = mp.build_mcp_url(ENDPOINT, TOKEN)
    assert parse_qs(urlsplit(url).query)["token"] == [TOKEN]


def test_url_preserves_other_query_params_and_replaces_token():
    url = mp.build_mcp_url(ENDPOINT + "?a=1&token=stale", TOKEN)
    query = parse_qs(urlsplit(url).query)
    assert query["a"] == ["1"]
    assert query["token"] == [TOKEN]


def test_url_without_token_is_unchanged():
    assert mp.build_mcp_url(ENDPOINT, "") == ENDPOINT


def test_headers_add_authorization_only_when_requested():
    # initialize / tools/list 带上这个头会被网关判成 8011，所以默认不能带
    assert "Authorization" not in mp.build_mcp_headers(TOKEN)
    # tools/call 必须带，否则 oidb 报 151
    assert mp.build_mcp_headers(TOKEN, with_authorization=True)["Authorization"] == (
        f"Bearer {TOKEN}"
    )
    assert "Authorization" not in mp.build_mcp_headers("", with_authorization=True)
    assert mp.build_mcp_headers(TOKEN)["X-Forwarded-Method"] == "POST"


def test_redact_token_hides_credential_in_urls():
    url = mp.build_mcp_url(ENDPOINT, TOKEN)
    redacted = mp.redact_token(url, TOKEN)
    assert TOKEN not in redacted
    assert "***" in redacted


def test_redact_token_without_knowing_the_token():
    assert "s3cr3t" not in mp.redact_token("...?token=s3cr3t&x=1")


def test_transient_classification_matches_real_messages():
    # 这些文案来自 main.py 的 _post_json，必须被判为可重试
    assert mp.is_transient_mcp_failure("腾讯频道接口触发频率限制，请稍后再试。")
    assert mp.is_transient_mcp_failure("请求腾讯频道端点超时。")
    assert mp.is_transient_mcp_failure("请求腾讯频道端点失败: Cannot connect to host ...")
    assert mp.is_transient_mcp_failure("腾讯频道端点返回 HTTP 502。")
    assert mp.is_transient_mcp_failure("151 [oidb]登录态验证失败")


def test_non_retryable_classification():
    # 工具/接口不存在，重试不会变好
    assert not mp.is_transient_mcp_failure(
        '腾讯频道 MCP 工具返回错误：{"message":"api info not exist","retcode":130001}'
    )
    assert not mp.is_transient_mcp_failure("腾讯频道鉴权失败，请检查 QQ AI Connect Token")
