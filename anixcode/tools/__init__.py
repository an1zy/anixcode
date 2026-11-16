"""All tools available to the agent."""
from __future__ import annotations

from .base import Tool, ToolContext, ToolError, ToolResult
from .files import EditTool, GlobTool, LsTool, ReadTool, WriteTool
from .search import GrepTool
from .shell import BashTool, shell_name

ALL_TOOLS: tuple[type[Tool], ...] = (ReadTool, LsTool, GlobTool, GrepTool, EditTool, WriteTool, BashTool)

__all__ = ["Tool", "ToolContext", "ToolError", "ToolResult", "ALL_TOOLS", "default_tools", "shell_name"]


def default_tools() -> dict[str, Tool]:
    return {cls.name: cls() for cls in ALL_TOOLS}
