"""Binding result messaging tests."""

from astrbot_plugin_lol_presence.commands.bind import _bind_result
from astrbot_plugin_lol_presence.providers.base import ObservationStatus


def test_bind_result_confirms_observable_watch_account():
    status = ObservationStatus(True, "观察号", "10001")

    text = _bind_result("✅ 绑定成功", "玩家#12345", status)

    assert "监控已生效" in text
    assert "观察号#10001" in text
    assert "我的播报" not in text


def test_bind_result_warns_when_friendship_is_missing():
    status = ObservationStatus(False, "观察号", "10001")

    text = _bind_result("✅ 绑定成功", "玩家#12345", status)

    assert "绑定已保存" in text
    assert "尚未检测到该账号" in text
    assert "最迟在 5 分钟" in text
    assert "我的播报" not in text
