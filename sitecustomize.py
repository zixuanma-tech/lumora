"""本地开发时把按职责拆分的后端目录加入模块搜索路径。"""

import sys
from pathlib import Path

BACKEND = str(Path(__file__).resolve().parent / "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
