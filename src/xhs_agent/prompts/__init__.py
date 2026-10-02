"""Prompt 资产目录（数据文件，不含逻辑）。

一个任务一份 Markdown 文件，文件名即 task_id，内含固定的五段结构：
01 System / 02 Task / 03 Constraints / 04 Examples / 05 Output Schema。
结构与渲染规则见 `docs/contracts/prompt契约.md`；加载与渲染实现在 `tools/prompt.py`。

本目录只放资源文件，不写代码。
"""