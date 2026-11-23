"""Textual application: owns settings, the authenticated client session and
per-session caches (field catalog, labels, people...)."""

from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

import httpx
from textual.app import App
from textual.binding import Binding

from ..api import AuthStrategy, JiraClient, TLSOptions
from ..api.models import FieldCatalog, ServerInfo
from ..config import EnvCredentials, Settings
from ..filters import FilterStore, QueryStore, ViewStateStore
from .common import HelpScreen
from .login import LoginScreen

TransportFactory = Callable[[], httpx.AsyncBaseTransport]


class JiraApp(App):
    TITLE = "Jira TUI"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("f1", "help", "Help"),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(
        self,
        settings: Optional[Settings] = None,
        *,
        env_creds: Optional[EnvCredentials] = None,
        env: Optional[Mapping[str, str]] = None,
        transport_factory: Optional[TransportFactory] = None,
        auto_connect: bool = False,
        ui_timeout: float = 60.0,
        demo: bool = False,
        filter_store: Optional[FilterStore] = None,
        query_store: Optional[QueryStore] = None,
        view_state: Optional[ViewStateStore] = None,
    ) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.env_creds = env_creds
        self._env: Dict[str, str] = dict(env if env is not None else os.environ)
        self.transport_factory = transport_factory
        self.auto_connect = auto_connect
        self.ui_timeout = ui_timeout
        self.demo = demo
        self.client: Optional[JiraClient] = None
        self.server: Optional[ServerInfo] = None
        self.filter_store = filter_store or FilterStore()
        self.query_store = query_store or QueryStore()
        self.view_state = view_state or ViewStateStore()
        self._cache: Dict[str, Any] = {}
        # Set after a create/transition/assign so list views reload when shown again.
        self.issues_changed = False

    # ------------------------------------------------------------ session
    def env_source(self) -> Mapping[str, str]:
        return self._env

    def make_client(self, url: str, auth: Optional[AuthStrategy], tls: TLSOptions) -> JiraClient:
        transport = self.transport_factory() if self.transport_factory else None
        return JiraClient(
            url,
            auth,
            tls=tls,
            timeout=self.settings.request_timeout,
            connect_timeout=min(10.0, self.settings.request_timeout),
            transport=transport,
        )

    def require_client(self) -> JiraClient:
        if self.client is None:
            raise RuntimeError("Not connected")
        return self.client

    @property
    def username(self) -> str:
        return self.server.username if self.server else ""

    def session_label(self) -> str:
        if not self.server:
            return ""
        host = urlsplit(self.server.base_url).netloc
        return f"{self.server.username or '?'} @ {host}" + ("  [DEMO]" if self.demo else "")

    def start_session(self, client: JiraClient, info: ServerInfo) -> None:
        self.end_session()
        self.client = client
        self.server = info
        from .main import MainScreen

        self.push_screen(MainScreen())

    def end_session(self) -> None:
        self._cache.clear()
        if self.client is not None:
            client = self.client
            self.client = None
            self.server = None
            self.run_worker(client.aclose(), exit_on_error=False)

    async def cached(self, key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Session cache for slow-changing lookups (labels, people, boards...).
        Failures are not cached, so a retry really retries."""
        if key not in self._cache:
            self._cache[key] = await factory()
        return self._cache[key]

    def invalidate(self, prefix: str = "") -> None:
        for k in [k for k in self._cache if k.startswith(prefix)]:
            del self._cache[k]

    async def catalog(self) -> FieldCatalog:
        return await self.require_client().fields()

    def remember_project(self, key: str) -> None:
        if key and key != self.settings.last_project:
            self.settings.last_project = key
            if self.settings.remember and not self.demo:
                from .. import config

                config.save(self.settings)

    # ------------------------------------------------------------ app
    def on_mount(self) -> None:
        if self.settings.insecure:
            self.notify("TLS verification is DISABLED (--insecure).", severity="error", timeout=10)
        self.push_screen(LoginScreen())

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    async def on_unmount(self) -> None:
        if self.client is not None:
            await self.client.aclose()
            self.client = None
