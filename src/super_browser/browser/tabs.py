"""Tab management — engine-owned multi-tab storage of NormalizedPages.

PR 1 item P5 (PLAN-COMPOSITION-HARDENING.md step 4). The manager no longer
touches raw backend pages or a Playwright ``BrowserContext``: it owns
:class:`NormalizedPage` instances created by the selected engine's
``new_page()`` and activates them through the portable
``EnginePage.activate()`` operation.

The initial façade page is an unlisted ``base_page``: ``list_tabs()``
continues to report managed tabs only, and when the last managed tab closes
the façade falls back to the base page instead of pointing at a closed one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from super_browser.browser.page import NormalizedPage

logger = logging.getLogger(__name__)


@dataclass
class TabHandle:
    """Reference to an open browser tab."""

    tab_id: int
    title: str = ""
    url: str = ""

    def __repr__(self) -> str:
        return f"TabHandle(id={self.tab_id}, url={self.url!r:.60})"


@dataclass
class TabSnapshot:
    """Snapshot of all open (managed) tabs."""

    tabs: list[TabHandle] = field(default_factory=list)
    active_tab_id: int = -1

    @property
    def count(self) -> int:
        return len(self.tabs)

    @property
    def active(self) -> Optional[TabHandle]:
        for t in self.tabs:
            if t.tab_id == self.active_tab_id:
                return t
        return None


class TabManager:
    """Owns managed tabs as NormalizedPages created by the engine.

    ``base_page`` is the façade's initial page, kept unlisted: managed-tab
    operations never report it, but closing the final managed tab falls back
    to it (activated) rather than leaving the façade on a closed page.
    """

    def __init__(self, engine: Any, base_page: Optional[NormalizedPage] = None) -> None:
        self._engine = engine
        self._base_page = base_page
        self._next_id = 1
        self._tabs: dict[int, NormalizedPage] = {}
        self._active_id: Optional[int] = None

    async def open_tab(self, url: Optional[str] = None) -> TabHandle:
        """Create a managed tab via ``engine.new_page()``, navigate, activate.

        :param url: Optional URL to navigate to immediately.
        :returns: TabHandle with assigned tab_id.
        """
        page = NormalizedPage(engine_page=await self._engine.new_page())
        tab_id = self._next_id
        self._next_id += 1
        self._tabs[tab_id] = page

        if url:
            await page.goto(url, wait_until="domcontentloaded")
        await page.activate()
        self._active_id = tab_id

        title = await page.title() if url else ""
        logger.info("Opened tab %d: %s", tab_id, url or "blank")
        return TabHandle(tab_id=tab_id, title=title, url=page.url)

    async def switch_tab(self, tab_id: int) -> TabHandle:
        """Activate a stored tab.

        :param tab_id: The tab ID returned by open_tab().
        :returns: TabHandle for the activated tab.
        :raises KeyError: If tab_id is not found.
        """
        page = self._require(tab_id)
        await page.activate()
        self._active_id = tab_id
        title = await page.title()
        logger.info("Switched to tab %d: %s", tab_id, page.url)
        return TabHandle(tab_id=tab_id, title=title, url=page.url)

    async def close_tab(self, tab_id: int) -> Optional[NormalizedPage]:
        """Close a managed tab.

        If the closed tab was active, the most recently opened remaining tab
        is activated and returned. If no managed tabs remain and a base page
        exists, the base page is activated and ``None`` is returned — the
        caller should make the base page active again.

        :param tab_id: The tab ID to close.
        :returns: The newly active managed page, or ``None`` when control
            falls back to the base page.
        :raises KeyError: If tab_id is not found.
        """
        page = self._require(tab_id)
        del self._tabs[tab_id]
        await page.close()
        logger.info("Closed tab %d", tab_id)

        if self._active_id != tab_id:
            return None

        if self._tabs:
            self._active_id = max(self._tabs)
            replacement = self._tabs[self._active_id]
            await replacement.activate()
            return replacement

        self._active_id = None
        if self._base_page is not None:
            await self._base_page.activate()
        return None

    async def list_tabs(self) -> TabSnapshot:
        """Snapshot of managed tabs (the base page is never listed)."""
        tabs = []
        for tid, page in self._tabs.items():
            try:
                title = await page.title()
            except Exception:
                title = ""
            tabs.append(TabHandle(tab_id=tid, title=title, url=page.url))
        return TabSnapshot(tabs=tabs, active_tab_id=self._active_id or -1)

    def active_page(self) -> NormalizedPage:
        """The active managed page.

        :raises KeyError: When no managed tab is active.
        """
        return self._require(self._active_id)

    @property
    def active_tab_id(self) -> Optional[int]:
        return self._active_id

    @property
    def tab_count(self) -> int:
        return len(self._tabs)

    def _require(self, tab_id: Optional[int]) -> NormalizedPage:
        if tab_id is None or tab_id not in self._tabs:
            raise KeyError(
                f"Tab {tab_id} not found. Managed tabs: {list(self._tabs.keys())}"
            )
        return self._tabs[tab_id]
