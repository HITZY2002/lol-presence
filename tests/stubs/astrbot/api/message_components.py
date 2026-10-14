"""astrbot.api.message_components stub。"""


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq=None, name=""):
        self.qq = qq
        self.name = name


class AtAll(At):
    def __init__(self):
        super().__init__(qq="all")
