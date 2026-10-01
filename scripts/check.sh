#!/bin/sh
# 一键代码检查：Ruff（快，风格与静态缺陷）+ BasedPyright（慢，类型）
#
# 两者分工不重叠：F 类（未用导入、未定义名）只有 Ruff 查；
# 类型问题只有 BasedPyright 查。所以两个都要跑，缺一不可。
#
# 用法：
#   ./scripts/check.sh          检查全部
#   ./scripts/check.sh --fix    顺便自动修可修的问题
set -e
cd "$(dirname "$0")/.."

V=".venv/bin"
if [ ! -x "$V/ruff" ] || [ ! -x "$V/basedpyright" ]; then
    echo "缺少开发依赖。先执行："
    echo "  uv venv .venv --python 3.11"
    echo "  uv pip install --python .venv/bin/python -e '.[dev]'"
    exit 1
fi

FIX=""
[ "$1" = "--fix" ] && FIX="--fix"

echo "── Ruff（格式 + 静态检查）──"
# 注意：--fix 只属于 `ruff check`（它修代码），
# `ruff format` 是格式化器，没有这个选项，传了会直接报 unexpected argument。
"$V/ruff" format src tests
"$V/ruff" check $FIX src tests

echo ""
echo "── BasedPyright（类型检查）──"
# 类型检查不用 --fix：类型错误必须人工判断上下文，不能机械改。
"$V/basedpyright"

echo ""
echo "全部通过。"
