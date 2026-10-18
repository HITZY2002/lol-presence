"""Opt-in real browser tests against a local DOM fixture, never Hexdata APIs."""

import os

import pytest

from astrbot_plugin_lol_presence.services.hexdata_browser import (
    HexdataBrowser,
    HexdataError,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("HEXDATA_BROWSER_TESTS") != "1",
    reason="Set HEXDATA_BROWSER_TESTS=1 to run isolated browser UI tests",
)

HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head><body>
<article data-matchmaking-state="idle">
<label for="game">游戏 ID</label><input id="game">
<button onclick="query()">查分</button>
<section role="region" aria-label="战绩卡" data-card-state="idle" hidden>
<h3>战绩卡</h3><button onclick="generate()">生成战绩卡</button>
</section></article>
<script>
function query() {
  const state = 'STATE';
  if (state === 'login') {
    document.body.insertAdjacentHTML('beforeend','<div role="dialog">请登录</div>');
    return;
  }
  document.querySelector('article').dataset.matchmakingState = state;
  document.querySelector('section').hidden = false;
}
function generate() {
  const section = document.querySelector('section');
  const canvas = document.createElement('canvas');
  canvas.width = 32; canvas.height = 48;
  canvas.getContext('2d').fillRect(0,0,32,48);
  const image = new Image();
  image.alt = 'MISMATCH' ? '旧账号#00000 的 Hexdata 战绩卡' : document.getElementById('game').value + ' 的 Hexdata 战绩卡';
  image.onload = () => { section.dataset.cardState = 'ready'; };
  image.src = canvas.toDataURL('image/png');
  section.append(image);
}
</script></body></html>"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,mismatch,error",
    [
        ("ready", False, None),
        ("collecting", False, None),
        ("ready", True, "不一致"),
        ("not_found", False, "未找到"),
        ("rate_limited", False, "额度"),
        ("failed", False, "无法完成"),
        ("login", False, "未登录"),
    ],
)
async def test_normal_controls_generate_or_stop(tmp_path, state, mismatch, error):
    service = HexdataBrowser(
        tmp_path,
        {
            "enabled": True,
            "headless": True,
            "browser_channel": os.environ.get("HEXDATA_TEST_CHANNEL", "msedge"),
        },
    )
    try:
        await service._start_browser()
        html = HTML.replace("'STATE'", repr(state)).replace(
            "'MISMATCH'", "true" if mismatch else "false"
        )
        await service._context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body=html
            ),
        )
        if error:
            with pytest.raises(HexdataError, match=error):
                await service.get_card("测试 玩家#12345")
            assert not list(service.card_dir.glob("*.png"))
        else:
            card = await service.get_card("测试 玩家#12345")
            assert card.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
            assert len(service._context.pages) == 1
            assert (await service.get_card("测试 玩家#12345")).cached
    finally:
        await service.close()
