"""MCP 网关的请求构造、凭据脱敏与失败分类（纯函数，可单测）。

把三件容易出错、又必须一致的事情集中到一处：

1. **请求 URL / 请求头构造**——实测网关对 Token 位置的要求按方法区分：
   ``initialize`` / ``tools/list`` / ``notifications/initialized`` 只认 URL query 上的
   token（附加 ``Authorization`` 头会返回 8011 "api info not exist"），而 ``tools/call``
   必须额外附加 ``Authorization`` 头（否则 oidb 层报 151 "登录态验证失败"）。
2. **凭据脱敏**——Token 现在会出现在 URL query 里，任何日志/异常文案都必须先抹掉，
   否则会随 AstrBot 的未捕获异常日志一起落盘。
3. **失败分类**——区分"值得重试的瞬时抖动"和"重试无意义的确定性错误"
   （如工具名拼错导致的 ``api info not exist``）。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

TOKEN_QUERY_KEY = "token"
REDACTED = "***"

# 可重试：网关/oidb 层抖动、限流、网络问题。
# 注意这些标记必须与 main.py 中 _post_json 实际抛出的中文文案对齐，
# 否则重试策略会静默失效（曾经踩过：只写英文标记导致 429/超时从不重试）。
TRANSIENT_MCP_MARKERS = (
    "频率限制",
    "请求频率过高",
    "频率受限",
    "接口调用已超过申请的频率上限",
    "超时",
    "端点失败",
    "登录态验证失败",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "timeout",
    "timed out",
    "connection reset",
    "cannot connect",
)

# 不可重试：网关上根本没有这个工具/接口（通常工具名拼错），重试不会变好。
NON_RETRYABLE_MCP_MARKERS = (
    "api info not exist",
    "130001",
)

_TOKEN_IN_TEXT_RE = re.compile(r"(?i)\b(token=)[^&\s'\"]+")


def is_transient_mcp_failure(detail: str) -> bool:
    """判断 MCP 调用失败是否属于可重试的瞬时故障。

    先排除确定性错误（``api info not exist`` / 130001 表示工具或接口不存在），
    再看是否命中抖动/限流/网络类标记。

    Args:
        detail: 错误详情文本（HTTP 层或 JSON-RPC 层的消息）。

    Returns:
        True 表示值得重试。
    """
    lowered = str(detail or "").lower()
    if any(marker in lowered for marker in NON_RETRYABLE_MCP_MARKERS):
        return False
    return any(marker in lowered for marker in TRANSIENT_MCP_MARKERS)


def redact_token(text: str, token: str = "") -> str:
    """把文本里的 Token 替换成 ``***``（明文与 URL 编码形式都处理）。

    Args:
        text: 原始文本，可能是异常消息或 URL。
        token: 当前 Token；为空时只做 ``token=xxx`` 形式的兜底脱敏。

    Returns:
        脱敏后的文本。
    """
    raw = str(text or "")
    if token:
        for variant in {token, quote(token, safe="")}:
            if variant:
                raw = raw.replace(variant, REDACTED)
    return _TOKEN_IN_TEXT_RE.sub(r"\1" + REDACTED, raw)


def build_mcp_url(endpoint: str, token: str = "") -> str:
    """拼接 MCP 端点 URL，并把 Token 作为 query 参数带上。

    Args:
        endpoint: MCP 端点地址。
        token: QQ AI Connect Token；为空时不附加参数。

    Returns:
        已附加 token 查询参数的端点 URL。
    """
    if not token:
        return str(endpoint or "")
    parts = urlsplit(str(endpoint or ""))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != TOKEN_QUERY_KEY
    ]
    query.append((TOKEN_QUERY_KEY, token))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def build_mcp_headers(
    token: str = "", *, with_authorization: bool = False
) -> dict[str, str]:
    """构造 MCP 请求头。

    Args:
        token: QQ AI Connect Token。
        with_authorization: 是否附加 ``Authorization`` 头（仅 ``tools/call`` 需要）。

    Returns:
        请求头字典。
    """
    headers = {"Content-Type": "application/json", "X-Forwarded-Method": "POST"}
    if token and with_authorization:
        headers["Authorization"] = f"Bearer {token}"
    return headers
