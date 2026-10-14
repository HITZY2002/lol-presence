#!/usr/bin/env bash
# LOL Presence 插件测试运行器
# 单元测试（astrbot stub，快速）与集成测试（真实 AstrBot 包）分开运行，
# 因为两者不能在同一 Python 进程内共存。
set -e
cd "$(dirname "$0")"
echo "== 单元测试 (tests/) =="
python -m pytest tests/ -q
echo ""
echo "== 集成测试 (integration/, 需要本机 AstrBot checkout) =="
python -m pytest integration/ -q -p no:cacheprovider
echo ""
echo "ALL TEST SUITES PASSED"
