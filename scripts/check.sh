#!/bin/sh
# 一键代码检查
#
# ## 为什么是这四个工具
#
# 刻意**不装第二个类型检查器**（mypy / pyrefly）：
# 同维度重复验证的边际收益接近零，产出的是同一批问题的第二份措辞。
# 真正的第二双眼睛必须来自**不同维度**（D-103）。
#
# | 工具          | 查什么                  | 谁查不到        |
# |---------------|-------------------------|-----------------|
# | Ruff          | 风格 + 静态缺陷（F 类） | 类型检查器不查  |
# | BasedPyright  | 类型                    | Ruff 不查        |
# | vulture       | 死代码                  | Ruff 的 F401 只管未用导入 |
# | coverage       | 哪些代码从没被执行       | 任何静态工具都答不了 |
#
# 用法：
#   ./scripts/check.sh          检查全部
#   ./scripts/check.sh --fix    顺便自动修可修的问题
#   ./scripts/check.sh --audit  额外跑覆盖率报告与死代码扫描（审计时用）
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
#
# **审计 D-242**：basedpyright **只要有 warning 就返回 1**，
# 而本脚本开了 errexit，于是引入双检查器那天起，
# 本行之后的**全部检查（内容包 192 项、契约一致性、决策号）
# 从未执行过**——每轮「check.sh 全绿」的记录里，它们根本没跑。
#
# 判据：0 errors 才是通过（本项目一贯口径，warnings 记录接受）。
# 故这里显式按输出判定退出码，而不是信工具的退出码。
"$V/basedpyright" | tee /tmp/.basedpyright.out
ERRCNT=$(grep -oE "[0-9]+ errors" /tmp/.basedpyright.out | grep -oE "[0-9]+" | head -1)
ERRCNT=${ERRCNT:-0}
if [ "$ERRCNT" != "0" ]; then
    echo "类型检查有 ${ERRCNT} 个 error——停在这里，后面的检查不跑。"
    exit 1
fi

# 覆盖率与死代码不在默认门禁里，原因见 AUDIT.md：
# 覆盖率是**趋势指标**不是**通过指标**——把它设成硬门禁，
# 会逼着人写"为了凑覆盖率而执行"的无效测试，反而污染判据。
# 死代码扫描在端口层有已知误报（Protocol 形参），需人工判读。
if [ "$1" = "--audit" ]; then
    echo ""
    echo "── 覆盖率（审计基线）──"
    # PYTHONPATH= 与 -p pytest_cov 是必需的：本机 /app/user-packages 抢在
    # .venv 之前，且其中的 platformdirs 残缺，会让插件加载直接失败。
    PYTHONPATH= "$V/python" -m pytest tests/ -q -p no:cacheprovider \
        -p pytest_cov --cov=src/zhengtong --cov-report=term-missing 2>&1 | tail -20

    echo ""
    echo "── 死代码（vulture）──"
    "$V/vulture" src/ --min-confidence 80 || true
fi

echo ""
echo "── 内容包约束（红线 / 门禁 / D-Card / 专有名词）──"
# 内容包是**软数据**，Ruff 与类型检查器都看不见它——
# 一份把 E14 红线删掉、或漏掉三态标注的提示词，代码侧四项检查全绿。
# 所以这里必须有一道单独的、针对内容本身的门禁。
# 字节码缓存先查：源码比 .pyc 新时，**运行的是旧代码**，
# 而 grep/sed/inspect 读文件全都看不出来（本项目踩过一次）
.venv/bin/python scripts/check_stale_bytecode.py --清理

python3 scripts/check_content_pack.py

# 第四轮新增：内容包与代码侧契约一致性。
# 内容包写 D1/角色/阶段/门禁，代码有唯一定义；不一致会让链路必然失败，
# 而任何单侧检查都查不出来（软数据对硬枚举）。
python3 scripts/check_content_contract.py

# 第二轮全面检查新增（D-241）：决策号一致性。
# D-157 那次撞号，判据只查内容包，而真正抓到的是 .py 里的悬空引用——
# 代码侧的 `D-xxx` 引用此前**无判据常驻**，写错号会安静通过。
python3 scripts/check_decision_refs.py

echo ""
echo "全部通过。"