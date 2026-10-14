"""astrbot stub 安装器（tests/stubs 已在 sys.path）。"""


def install_stubs() -> None:
    import importlib

    for mod in (
        "astrbot.api",
        "astrbot.api.event",
        "astrbot.api.star",
        "astrbot.api.message_components",
    ):
        try:
            importlib.import_module(mod)
        except ImportError as e:
            raise RuntimeError(f"stub load failed: {mod}: {e}")
