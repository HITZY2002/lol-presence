"""pytest 配置：安装 astrbot stub + 包路径。"""

import importlib.util
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent / "stubs"))

from conftest_setup import install_stubs  # noqa: E402

install_stubs()

# Repository names and installation folder names need not be Python identifiers.
spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_lol_presence",
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
assert spec is not None and spec.loader is not None
plugin_package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin_package
spec.loader.exec_module(plugin_package)
