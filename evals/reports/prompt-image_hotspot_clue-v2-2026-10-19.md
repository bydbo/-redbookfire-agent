# image_hotspot_clue v2：Constraints

本次仓库提交只修改 §03 Constraints 并递增版本，说明文字排版归 visual、内容主题归 topic，禁止 text / object / person；保留九类领域枚举。非法输出仍由服务端拒绝。

相同冻结夜跑图的真实样本：v1 曾输出非法 text；v2 有一次重新复现的失败，原因记录为 type='text'。因此 v2 不能视为效果通过；后续 Examples / Output Schema 修复分段提交。完整七维度及独立评分未执行，M18 待验收。原始失败与费用在 runs/_e7b6_final/model-results.json，详见 [续工报告](context-repair-2026-10-19.md)。
