"""skills 包：每个 .py 文件 = 一个独立技能。

约定
----
1. 所有 skill 文件用 `from skills import mcp` 拿到全局唯一的 FastMCP 实例
2. 在自己模块里用 `@mcp.tool()` 装饰函数即可注册一个新的 MCP 工具
3. 新增 skill = 在本目录创建一个新的 .py 文件即可，finMCP.py 会自动发现并加载
   （加载逻辑见 finMCP.py 里的 pkgutil.iter_modules）

下划线开头的文件（如 _common.py）会被自动发现器忽略，用来放共享工具/常量。
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("finance")

__all__ = ["mcp"]
