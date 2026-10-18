"""集成测试 conftest：使用真实 AstrBot 包 + AstrBot venv 依赖，不安装 stub。"""

import importlib.util
import os
import sys
from pathlib import Path

plugin_root = Path(__file__).resolve().parents[1]
astrbot_source = os.environ.get("ASTRBOT_SOURCE_DIR")
if astrbot_source:
    sys.path.insert(0, str(Path(astrbot_source).resolve()))

# Load the checkout under the stable plugin identifier, regardless of its name.
spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_lol_presence",
    plugin_root / "__init__.py",
    submodule_search_locations=[str(plugin_root)],
)
assert spec is not None and spec.loader is not None
plugin_package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_package
spec.loader.exec_module(plugin_package)
