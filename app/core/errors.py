"""插件统一的上游错误类型。

单独成模块是为了让 mcp_client / device_login / main 都能引用它而互不产生循环导入。
"""

from __future__ import annotations

from typing import Any


class TencentChannelError(Exception):
    """腾讯频道接口错误。

    Args:
        message: 可展示给管理员的错误信息。
        data: 上游返回的原始数据。
    """

    def __init__(self, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.data = data
