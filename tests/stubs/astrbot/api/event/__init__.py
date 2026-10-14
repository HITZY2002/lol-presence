"""astrbot.api.event stub。"""


class MessageChain(list):
    """MessageChain stub： behaves like list of components."""

    def __init__(self, chain=None):
        super().__init__(chain or [])

    def message(self, text):
        from astrbot.api.message_components import Plain

        self.append(Plain(text))
        return self

    def at(self, name, qq):
        from astrbot.api.message_components import At

        self.append(At(qq=qq, name=name))
        return self


def filter(*_a, **_k):
    def deco(fn):
        return fn

    return deco


class AstrMessageEvent:
    pass
