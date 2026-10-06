# TODO · 尚未实现的事项

每条都记着**它卡在哪、什么时候记的**。做完即删。

## 1 便携 zip 打包脚本不存在

- **卡在哪**：用户要的交付形态是 Windows 便携 zip（D-003），
  而全仓 `grep -rlE "zip|便携|dist" Supporter/*.py Supporter/*.sh` 为空。
- **为什么要紧**：D-384 的硬要求「脱离 Supporter 程序能独立工作」
  目前**只能靠判据证明**（真把 Supporter 移走跑 pytest），
  证不了「交付物里真的没有 Supporter」——那要等打包脚本落地。
- **记于**：2026-10-05，D-384 落地时。
