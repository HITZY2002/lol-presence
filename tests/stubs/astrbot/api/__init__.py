"""astrbot.api stub —— 最小接口面。"""

import logging

logger = logging.getLogger("astrbot")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class AstrBotConfig(dict):
    def save_config(self):
        pass
