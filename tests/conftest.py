"""pytest 配置：安装 astrbot stub + 包路径。"""

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT.parent))  # 使 astrbot_plugin_lol_presence 可导入
sys.path.insert(0, str(Path(__file__).parent / "stubs"))

from conftest_setup import install_stubs  # noqa: E402

install_stubs()
