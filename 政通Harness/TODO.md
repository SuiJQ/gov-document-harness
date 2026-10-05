# TODO · 尚未实现的事项

每条都记着**它卡在哪、什么时候记的**。做完即删。

## 1 `verify_endpoint.py` 缺第 5 项探测（推理开关）

- **卡在哪**：D-295 作废了流式探测（`HttpLlm.探测流式` 与
  `Transport.post_stream` 整体删除），**第 5 项改成了推理开关（D-048）**，
  但 `Supporter/verify_endpoint.py` 里的流式探测段没人删也没人替换。
- **本轮处置**：整段删掉。原代码调 `llm.探测流式()`——**该方法已不存在**，
  被 `except Exception` 吞成「流式能力 不可用」，**每次都这么打且不报错**。
  是 basedpyright 报 `HttpLlm` 无此属性才把它翻出来的。
- **欠什么**：按 D-048 补一个推理开关探测项。
  「不使用流式」这一半继续遵守。
- **记于**：2026-10-05，Harness 两层拆分那轮（D-384）。

## 2 便携 zip 打包脚本不存在

- **卡在哪**：用户要的交付形态是 Windows 便携 zip（D-003），
  而全仓 `grep -rlE "zip|便携|dist" Supporter/*.py Supporter/*.sh` 为空。
- **为什么要紧**：D-384 的硬要求「脱离 Supporter 程序能独立工作」
  目前**只能靠判据证明**（真把 Supporter 移走跑 pytest），
  证不了「交付物里真的没有 Supporter」——那要等打包脚本落地。
- **记于**：2026-10-05，D-384 落地时。
