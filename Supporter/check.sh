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
# | pytest        | **行为**：按设计工作吗   | 任何静态工具都答不了 |
# | Ruff          | 风格 + 静态缺陷（F 类） | 类型检查器不查  |
# | BasedPyright  | 类型                    | Ruff 不查        |
# | vulture       | 死代码                  | Ruff 的 F401 只管未用导入 |
# | coverage       | 哪些代码从没被执行       | 任何静态工具都答不了 |
#
# **pytest 排在最前，不是随便放的**（D-290）：
# 它是唯一能说「行为对不对」的判据，而 `set -e` 意味着
# 任何一道早退都会让后面的检查**从未执行**——
# D-242 栽过一次（basedpyright 提前退出），本条是同一个坑的更大幅度版。
#
# 用法：
#   ./Supporter/check.sh          检查全部（含测试）
#   ./Supporter/check.sh --fix    顺便自动修可修的问题
#   ./Supporter/check.sh --audit  额外跑覆盖率报告与死代码扫描（审计时用）
set -e
# 主体在同级目录 `政通Harness/`。Supporter 是开发期工具，不进交付物，
# 但它得知道主体在哪才能查它。
cd "$(dirname "$0")/../政通Harness"

V=".venv/bin"
if [ ! -x "$V/ruff" ] || [ ! -x "$V/basedpyright" ]; then
    echo "缺少开发依赖。先执行："
    echo "  uv venv .venv --python 3.11"
    echo "  uv pip install --python .venv/bin/python -e '.[dev]'"
    exit 1
fi

FIX=""
[ "$1" = "--fix" ] && FIX="--fix"
# 显式 `--fix` 才允许格式化改文件（见下方 D-244 说明）
#
# **`ruff format` 没有 `--fix`**（实测 ruff 0.16.9：给了直接
# `error: unexpected argument '--fix' found`）。它**默认就改文件**，
# 要「只报不改」得显式给 `--check`。
#
# 故修的模式下**什么参数都不给**，而不是给一个不存在的 `--fix`——
# 那会让 `./Supporter/check.sh --fix` **一用就崩**，
# 而它此前确实一直崩着：没人用过这个选项（都跑不带参数的版本），
# 于是「声明了但一用就错」又是一个没人走到的分支（D-281 同型）。
FORMAT_MODE="--check"
[ "$1" = "--fix" ] && FORMAT_MODE=""

# ── `--fix` 前后拍快照（D-284）──────────────────────────────
# 拍在**动手之前**。改动之后才拍，拍到的就是改完的样子，diff 永远为空。
#
# 为什么要这道：`ruff check --fix` 会改它路径下**每一个**可修违规，
# 不管我有没有写过那个文件、那条问题我知不知道，
# 而它默认**不告诉你改了哪儿**——于是「动了生产代码」这件事
# 是靠事后撞上症状才发现的。
#
# 快照写进 `.git/`：**不放工作区**，否则 git status 会看见它，
# 守卫会把自己的临时文件当成「新改动」报出来。
#
# **变量名用 ASCII**：本脚本是 `#!/bin/sh`，实际由 dash 执行，
# 而 dash **不接受非 ASCII 变量名**（写了报 `xxx: not found`）。
# 注释可以用中文，标识符不行。
# **仓库根不在 CWD**：`.git` 已随目录拆分上移到 `../`（仓库根 = `Harness/`，
# 主体只是它的一个子目录）。原先写死 `.git` 是因为脚本在项目根内。
# 用 `git rev-parse --git-dir` 问 git 自己，比手写相对路径可靠——
# 它对「从哪个子目录调用」免疫。
GITDIR="$(git rev-parse --git-dir 2>/dev/null || true)"
if [ "$FIX" = "--fix" ]; then
    SNAP="$GITDIR/check_fix_before"
    if [ -n "$GITDIR" ] && [ -d "$GITDIR" ]; then
        git status --porcelain | sort > "$SNAP" 2>/dev/null || : > "$SNAP"
    else
        SNAP="/tmp/check_fix_before.$$"
        : > "$SNAP"
    fi
fi
# ── pytest（D-290）────────────────────────────────────────────
#
# **位置说明：为什么排在静态检查之前**
#
# 一开始我把它放在 basedpyright 之后，理由是「类型过了再看测试更划算」。
# 实测这个顺序有个致命后果：**类型检查一挂，后面全都不跑**——
# 和 D-242 那个 basedpyright 提前退出的老 bug 同一型，只是范围更大。
#
# 测试是**唯一能说「这份代码按设计工作」的判据**，
# 必须排在最前面当快筛：静态检查慢、且只说「形状不对」；
# 测试一挂说明**行为已经不对了**，没道理再往下看。
echo ""
echo "── pytest（测试）──"
# `PYTHONPATH=` 与 `-p no:cacheprovider` 的理由见 --audit 段那段注释。
#
# **退出码直接用 pytest 的，不接管道。**
# 写成 `... | tail -20` 的话，`$?` 是 `tail` 的退出码——
# `tail` 永远成功，于是测试红成什么样都算「全绿」。
#
# **用 `if ! …` 而不是先跑再取 `$?`**：
# 本脚本开头有 `set -e`，pytest 一红就在**那一行**直接退出了，
# 后面那句提示根本执行不到。证伪时看到的就是「exit=1 但什么也没说」。
# 退出码对、提示是死的——比不做还糟，看起来像有意为之。
if ! PYTHONPATH= "$V/python" -m pytest tests/ -q -p no:cacheprovider; then
    echo ""
    echo "测试未全过——停在这里，后面的检查不跑。"
    exit 1
fi

echo "── Ruff（格式 + 静态检查）──"
# **默认只检查不改**（D-244）。
#
# 此前每次都跑 `ruff format src tests`，它会**改文件**且退出码为 0。
# 后果不是「多了个自动修复」，而是：
#
# 1. `ruff format` 排在 basedpyright **之前**。类型检查不过就退出，
#    格式化被跳过——**同一次运行的不同阶段看到的不是同一份代码**，
#    而报错行号是格式化前的，对不上。
# 2. 门禁跑到一半停下时，工作区停在「已格式化但未检查」的中间态，
#    且没有任何输出提示这一点。
#
# 故默认 `--check`（只报不改），真要改用 `--fix` 显式触发。
# 注意 `--fix` 只属于 `ruff check`；`ruff format` 的对应参数也是 `--fix`，
# 但两者作用不同，故分开写。
"$V/ruff" format $FORMAT_MODE src tests
"$V/ruff" check $FIX src tests

# **报账**：`--fix` 到底动了哪些文件（D-284）。
#
# 分三类：
#   - 本轮我已改过的（拍快照时就已经脏）→ 正常
#   - `--fix` **新弄脏**的 → **这一类才是要盯的**
#
# **不用 `<(...)` 进程替换**：那是 bash 专有语法，
# 本脚本由 dash 执行（`#!/bin/sh`），写了会 `Syntax error`。
# 改用排序后的临时文件喂 `comm`。
#
# **边界（如实说）**：本守卫只抓「修完仍脏」的文件。
# 若 `--fix` 把文件修回干净（内容被改过但最终与 HEAD 一致），它不响。
# 那种情况文件最终是对的，故不响不算漏——但**不能宣称全覆盖**。
if [ "$FIX" = "--fix" ] && [ -n "$GITDIR" ] && [ -d "$GITDIR" ]; then
    AFTER="$GITDIR/check_fix_after"
    git status --porcelain | sort > "$AFTER" 2>/dev/null || : > "$AFTER"
    NEWLY="$(comm -13 "$SNAP" "$AFTER" 2>/dev/null || true)"
    echo ""
    echo "── --fix 改了什么（D-284）──"
    if [ -n "$NEWLY" ]; then
        echo "⚠️  以下文件是 \`--fix\` **新改动**的（此前不在本轮改动范围内）："
        echo "$NEWLY" | sed 's/^/   /'
        echo "   请逐条确认：这些改动是否是你要的。确认无误后再提交。"
        rm -f "$AFTER" "$SNAP"
        exit 2
    fi
    echo "没有新改动文件（--fix 只碰了本轮已在改的文件）。"
    rm -f "$AFTER" "$SNAP"
fi

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
.venv/bin/python ../Supporter/check_stale_bytecode.py --清理

python3 ../Supporter/check_content_pack.py

# 第四轮新增：内容包与代码侧契约一致性。
# 内容包写 D1/角色/阶段/门禁，代码有唯一定义；不一致会让链路必然失败，
# 而任何单侧检查都查不出来（软数据对硬枚举）。
python3 ../Supporter/check_content_contract.py

# 第三轮（D-247）：接线专项——「存进 self 后类内从无读取」。
# 历史缺陷里栽得最多的不是写错，而是**值在那里没人往下传**
# （D-222/D-231/D-233/D-234/D-235 五条同型）。
python3 ../Supporter/check_wiring.py || true

# 第四轮（D-257）：数据字段消费——「字段声明了但全仓无人读」。
# 与上一条**形状不同**：check_wiring 盯 `self.X`（类实例），
# 本条盯 frozen dataclass 字段。D-239 三项属后者——
# `TIER_BY_ID[档].字段名` 的读取**不出现在类内 self 链上**，查 wiring 查不到。
python3 ../Supporter/check_field_consumption.py

# 第五轮（D-270）：端口契约字段的**用户路径**消费。
# 与上一条**判据不同**：那条问「全仓有没有人读」，这条问
# 「用户路径上有没有人读」——`prompt_tokens` 在 `llm_probe` 里被读过，
# 于是「有人读」成立，但那是**连接检测**路径，用户看不到。
# **「有人读」≠「用户看得到」**（D-269 的成因）。
python3 ../Supporter/check_port_fields.py

# 第二轮全面检查新增（D-241）：决策号一致性。
# D-157 那次撞号，判据只查内容包，而真正抓到的是 .py 里的悬空引用——
# 代码侧的 `D-xxx` 引用此前**无判据常驻**，写错号会安静通过。
python3 ../Supporter/check_decision_refs.py

echo ""
# 第六轮：接线逃逸口——「合法但失真」的三种写法。
# 与前面几条**形状不同**：它们盯「有没有人在读」，
# 这条盯「读取发生了，但读取方式是假的」——
# `object` 标注 / `# type: ignore` / `getattr(x, "字段", 默认)`
# 三者**都是合法 Python**，所以类型检查不报、测试不报、
# 代码读起来完全正常，而接线断了之后界面全静默（D-267/D-296 同族）。
#
# `object`/ignore 只查界面层：全仓查会误报 43 处，绝大多数是
# `def _as_int(value: object, ...)` 这类**解析外部输入的正确写法**。
# 43 处误报的检查等于没有检查。
python3 ../Supporter/check_escape_hatches.py

# 第七轮：事件接线——「定义即完成」。
# 架构 §5.4 列了 38 个事件，枚举里也全有，但「有定义」不等于
# 「有发射点」。本轮体检查出 7 个事件既不发射也不消费，
# 而 2084 条判据全绿——读架构的人会以为它们存在。
#
# 同时查「有消费但零发射」：消费代码在等一个永不到来的事件
# （D-118 删掉的就是这种死分支）。
python3 ../Supporter/check_event_wiring.py

# 第八轮：未接线运行时实测。
#
# **前面七支全部读源码**，而 D-406 那七个缺陷源码上完全正常：
# 字段有声明、处理器有判空与失败返回、测试全绿。
# 只有造一个真实 App 读完字段的实际值才看得见——模型调那些工具
# 直接失败，而程序不报任何错。
#
# 用 `.venv/bin/python`（不是裸 python3）：本支**要导入真实模块**，
# 而 `tomli_w` 只装在 venv 里。裸 python3 会 ModuleNotFoundError——
# 脚本对此 exit 2 而不是当成通过。
.venv/bin/python ../Supporter/check_wiring_runtime.py
echo ""
echo "全部通过。"
