"""pytest 配置：把插件上级目录加入 sys.path，使测试可以按包导入 app.* 模块。"""

from __future__ import annotations

import sys
from pathlib import Path

PARENT = Path(__file__).resolve().parents[2]
if str(PARENT) not in sys.path:
    sys.path.insert(0, str(PARENT))
