"""Super Browser — Comprehensive browser control for AI agents."""

from super_browser.agent.facade import SuperBrowser as SuperBrowser  # noqa: F401
from super_browser.agent.llm import create_llm as create_llm  # noqa: F401
from super_browser.agent.types import StreamEvent as StreamEvent  # noqa: F401
from super_browser.config import Config as Config  # noqa: F401
from super_browser.results.types import ActionResult as ActionResult  # noqa: F401

__version__ = "2.13.1"

__all__ = [
    "SuperBrowser",
    "Config",
    "ActionResult",
    "create_llm",
    "StreamEvent",
]
