"""覆盖 zh-hans 内置本地化数字格式：启用标准千分位（每 3 位，逗号）。

Django 自带 zh_Hans 把 THOUSAND_SEPARATOR 设为空、分组为 4，导致金额不显示千分位。
本模块经 settings.FORMAT_MODULE_PATH 生效，优先于 Django 内置。
"""
THOUSAND_SEPARATOR = ","
NUMBER_GROUPING = 3
DECIMAL_SEPARATOR = "."
