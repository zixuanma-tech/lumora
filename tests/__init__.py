"""测试入口：让测试保持使用后端模块的简洁顶层导入。"""

import sys
from pathlib import Path

BACKEND = str(Path(__file__).resolve().parents[1] / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
