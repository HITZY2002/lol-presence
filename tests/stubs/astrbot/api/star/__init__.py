"""astrbot.api.star stub。"""

from pathlib import Path


class Context:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, message_chain):
        self.sent.append((session, list(message_chain)))
        return True


class Star:
    def __init__(self, context):
        self.context = context


def register(*_a, **_k):
    def deco(cls):
        return cls

    return deco


class StarTools:
    _root = None

    @classmethod
    def get_data_dir(cls, plugin_name=None):
        import tempfile

        root = cls._root or tempfile.mkdtemp(prefix="lol_plugin_test_")
        p = Path(root) / (plugin_name or "default")
        p.mkdir(parents=True, exist_ok=True)
        return p
