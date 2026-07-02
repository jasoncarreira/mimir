"""Distribution marker for Mimir's controlled Claude Code adapter target.

The runtime import package remains ``langchain_claude_code``. This package
exists so Mimir can depend on a normal, versioned distribution while retaining
the existing adapter import and runtime patch layer.
"""

__version__ = "0.1.0"
