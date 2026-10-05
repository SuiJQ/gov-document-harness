#!/bin/sh
# Supporter 自查入口：**只开 F 类**。
#
# ## 为什么单独一个入口，而不是回到主体配置里加 exclude
#
# 实测三条（`ruff check .` 不越出项目根）：
# - Supporter 在 `../Supporter/`，跑主体的 `ruff check .` 压根扫不到它；
# - 写 `exclude = ["../Supporter/**"]` 能解析、不报错，但那是**空操作**——
#   排除一个压根不在扫描面里的东西；
# - 所以主体侧正确做法是**不设 exclude**，让 Supporter 自己管自己。
#
# ## 为什么只开 F 类（用户拍板 A2）
#
# 主体那条规则集有 11 类。Supporter 全开的话，按项目规则集实测有 **71 条**
# （E501 行长 25、RUF100 无效 noqa 10、E741 变量名 9、W292 缺尾换行 6…）。
# 在辅助脚本上刷这些**不值**：它们不掩盖真 bug，只制造要忽略的红。
#
# F 类（pyflakes）不一样——它抓的是**会真出错**的东西：
# 未用变量、未定义名、重复键、被遮蔽的 import。
# 实测正是它抓出 5 处 F841，其中一处揭示了
# `check_content_contract.py` 里有个 `return []` 的空壳函数
# 却摆着两个准备好的白名单——**读代码的人极难相信它什么都没查**。
#
# 依据：
# - Supporter 的类型检查：`../政通Harness/.venv/bin/basedpyright ../Supporter`
#   （主体配置 `include=["src","tests"]` 不含它，得显式传路径）
set -e
cd "$(dirname "$0")"
V="../政通Harness/.venv/bin"

if [ ! -x "$V/ruff" ]; then
  echo "找不到 $V/ruff——先在 政通Harness/ 建好虚拟环境" >&2
  exit 2
fi

echo "── Supporter 自查（Ruff，只开 F 类）──"
"$V/ruff" check --no-cache --isolated --select F .

echo
echo "── Supporter 类型检查（basedpyright）──"
# **必须点名主体配置**：`basedpyright .` 在 Supporter 下找不到
# `pyproject.toml`，会退回内置默认规则（实测 17 errors，全是配置之差
# 而非代码之差）。主体配置里的 include/ignore 才是本项目的口径。
"$V/basedpyright" --project ../政通Harness/pyproject.toml . 2>/dev/null | tail -1

printf '%s\n' \
  '注意：主体侧 ruff check . 与 basedpyright 都不会扫到这里' \
  '（两者都以项目根为界，不越出）。故本脚本是 Supporter 唯一的自查途径。'
