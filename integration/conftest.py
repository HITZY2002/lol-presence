"""集成测试 conftest：使用真实 AstrBot 包 + AstrBot venv 依赖，不安装 stub。"""

import os
import sys
from pathlib import Path

plugin_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(plugin_root.parent))
astrbot_source = os.environ.get("ASTRBOT_SOURCE_DIR")
if astrbot_source:
    sys.path.insert(0, str(Path(astrbot_source).resolve()))
