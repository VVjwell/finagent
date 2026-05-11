"""MCP Server 入口。

本文件本身不定义任何 tool。它做两件事：
1. 自动发现 skills/ 目录下所有非下划线开头的模块并 import 进来，
   每个模块在被导入时通过 @mcp.tool() 自行注册自己的工具
2. 启动 FastMCP 服务（默认 stdio）

所以以后加新 skill = 在 skills/ 下新建一个 .py 文件，本文件不用改。
"""

from __future__ import annotations

import importlib
import pkgutil

import skills
from skills import mcp


def _autoload_skills() -> list[str]:
    """遍历 skills 包，import 所有非下划线开头的子模块以触发 @mcp.tool() 注册。"""
    loaded: list[str] = []
    for mod_info in pkgutil.iter_modules(skills.__path__):
        name = mod_info.name
        if name.startswith("_"):
            continue
        importlib.import_module(f"skills.{name}")
        loaded.append(name)
    return loaded


def main() -> None:
    """`finagent-mcp` console-script 入口；`python finMCP.py` 也走这里。"""
    import sys

    loaded = _autoload_skills()
    # stderr 打印到 MCP Inspector 的 server log，便于调试启动是否正常
    print(f"[finMCP] loaded skills: {loaded}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
