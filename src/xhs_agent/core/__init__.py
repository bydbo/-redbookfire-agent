"""核心层：配置、日志、异常与常量（跨层共用，不依赖 api / services / workflows）。

依赖方向：`core/` 只依赖标准库与 `config.py`；上层（api / services / workflows）可以导入它，
它不反向导入任何上层模块。
"""
