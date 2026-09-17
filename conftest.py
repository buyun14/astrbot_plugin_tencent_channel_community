"""pytest 配置：把插件根目录加入 sys.path，使 tests/ 可以直接 import channel_data。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
