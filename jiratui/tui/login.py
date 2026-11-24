"""Connection / login screen.

Connect runs two checks: an anonymous ``/rest/api/2/serverInfo`` probe (is this
Jira? which version? PATs need 8.14+) and an authenticated ``/myself`` call that
proves the credentials. **Detect** (Ctrl+T) runs only the probe, marks auth
methods the server cannot support and preselects the best one; the user can
still override the choice.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, Footer, Header, Input, Label, RadioButton, RadioSet, Static

from .. import config, keychain
from ..api import ConfigError, JiraClient, TLSOptions, make_auth, normalize_base_url
from ..api.models import ServerInfo
from .common import BaseScreen, error_text, guarded

if TYPE_CHECKING:  # pragma: no cover
    from .app import JiraApp

BEARER_LABEL = "Bearer: personal access token (Jira DC/Server 8.14+)"
BASIC_LABEL = "Basic: username + password (or username + token)"


class LoginScreen(BaseScreen):
    BINDINGS = [
        Binding("ctrl+s", "connect", "Connect"),
        Binding("ctrl+t", "detect", "Detect server"),
        Binding("ctrl+e", "load_env", "Load from env"),
        Binding("escape", "cancel_connect", "Cancel"),
    ]

    DEFAULT_CSS = """
    LoginScreen VerticalScroll { padding: 1 2; }
    LoginScreen Label.field { margin-top: 1; text-style: bold; width: 100%; }
    LoginScreen Checkbox { width: 100%; }
    LoginScreen #status { margin-top: 1; height: auto; }
    LoginScreen #status.error { color: $error; }
    LoginScreen #status.ok { color: $success; }
    LoginScreen #detect-info { height: auto; color: $text-muted; }
    LoginScreen #detect-info.ok { color: $success; }
    LoginScreen #detect-info.warn { color: $warning; }
    LoginScreen #insecure-warning { color: $error; text-style: bold; display: none; }
    LoginScreen #insecure-warning.show { display: block; }
    LoginScreen Horizontal { height: auto; margin-top: 1; }
    LoginScreen Button { margin-right: 2; }
    LoginScreen RadioSet { width: 100%; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._connect_worker = None
        self._detect_worker = None
        self.detected: Optional[ServerInfo] = None

    @property
    def japp(self) -> "JiraApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        s = self.japp.settings
        yield Header()
        with VerticalScroll():
            yield Static(
                Text.from_markup(
                    "[b]Connect to Jira Server / Data Center[/b]\n"
                    "Enter the base URL you open in the browser, your username and a personal access token.\n"
                    "Tokens are never written to disk unless you opt in to the macOS Keychain below."
                )
            )
            yield Label("Server URL", classes="field")
            yield Input(value=s.base_url, placeholder="https://jira.example.com", id="url")
            yield Button("Detect auth methods (Ctrl+T)", id="detect")
            yield Static(Text("Detect probes /rest/api/2/serverInfo (no credentials sent) to see which auth "
                              "methods this server supports."), id="detect-info")
            yield Label("Authentication method", classes="field")
            with RadioSet(id="method"):
                yield RadioButton(BEARER_LABEL, value=s.auth_method != "basic", id="m-bearer")
                yield RadioButton(BASIC_LABEL, value=s.auth_method == "basic", id="m-basic")
            yield Label("Username (required for Basic; checked against the token's user for Bearer)", classes="field")
            yield Input(value=s.username, placeholder="username", id="username")
            yield Label("Personal access token / password", classes="field", id="secret-label")
            yield Input(password=True, placeholder="paste token (hidden)", id="secret")
            with Collapsible(title="TLS options", collapsed=not (s.ca_bundle or s.insecure)):
                yield Label("Custom CA bundle (PEM) path, optional", classes="field")
                yield Input(value=s.ca_bundle, placeholder="~/certs/corp-ca.pem", id="ca_bundle")
                yield Checkbox(
                    "Also trust CA certificates from the macOS Keychain (recommended for internal CAs)",
                    value=s.use_macos_keychain_cas,
                    id="keychain_cas",
                    disabled=sys.platform != "darwin",
                )
                yield Checkbox("Disable TLS certificate verification (INSECURE)", value=s.insecure, id="insecure")
                yield Static(
                    "WARNING: TLS verification is OFF. Your token can be intercepted by anyone on the network path. "
                    "Use only for short-lived diagnosis.",
                    id="insecure-warning",
                    classes="show" if s.insecure else "",
                )
            yield Checkbox("Remember server URL and username on this machine (never the token)", value=s.remember, id="remember")
            yield Checkbox("Save token in macOS Keychain (opt-in)", value=s.token_in_keychain, id="save_token")
            with Horizontal():
                yield Button("Connect (Ctrl+S)", variant="primary", id="connect")
                yield Button("Load from env (Ctrl+E)", id="env")
                yield Button("Quit", id="quit")
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Jira TUI"
        self.sub_title = "connect"
        app = self.japp
        if app.env_creds and (app.env_creds.base_url or app.env_creds.secret):
            self._apply_env(app.env_creds, quiet=True)
        else:
            self._load_keychain_token()
        self.query_one("#url" if not self.query_one("#url", Input).value else "#secret", Input).focus()
        if app.auto_connect and self.query_one("#url", Input).value and self.query_one("#secret", Input).value:
            self.action_connect()

    def on_screen_resume(self) -> None:
        # Back on the login screen: the previous session (if any) is closed.
        self.japp.end_session()
        self.sub_title = "connect"

    # ------------------------------------------------------------- helpers
    def _method(self) -> str:
        return "basic" if self.query_one("#m-basic", RadioButton).value else "bearer"

    def _status(self, text: str, cls: str = "") -> None:
        st = self.query_one("#status", Static)
        st.update(Text(text))
        st.set_class(cls == "error", "error")
        st.set_class(cls == "ok", "ok")

    def _detect_status(self, text: str, cls: str = "") -> None:
        st = self.query_one("#detect-info", Static)
        st.update(Text(text))
        st.set_class(cls == "ok", "ok")
        st.set_class(cls == "warn", "warn")

    def _tls(self) -> TLSOptions:
        return TLSOptions(
            verify=not self.query_one("#insecure", Checkbox).value,
            ca_bundle=self.query_one("#ca_bundle", Input).value.strip() or None,
            use_macos_keychain=self.query_one("#keychain_cas", Checkbox).value,
        )

    def _apply_env(self, creds: config.EnvCredentials, quiet: bool = False) -> None:
        if creds.base_url:
            self.query_one("#url", Input).value = creds.base_url
        if creds.username:
            self.query_one("#username", Input).value = creds.username
        if creds.auth_method in ("basic", "bearer"):
            self.query_one("#m-basic" if creds.auth_method == "basic" else "#m-bearer", RadioButton).value = True
        if creds.secret:
            self.query_one("#secret", Input).value = creds.secret
        if creds.ca_bundle:
            self.query_one("#ca_bundle", Input).value = creds.ca_bundle
        parts = [n for n, v in (("URL", creds.base_url), ("user", creds.username), ("token", creds.secret)) if v]
        if parts:
            self._status(f"Loaded {', '.join(parts)} from environment.", "ok")
        elif not quiet:
            self._status("No JIRA_URL / JIRA_PAT / JIRA_USER / JIRA_PASSWORD found in the environment.", "error")

    def _load_keychain_token(self) -> None:
        s = self.japp.settings
        if not (s.token_in_keychain and s.base_url):
            return
        try:
            url = normalize_base_url(s.base_url)
        except ConfigError:
            return
        tok = keychain.get_token(url, s.auth_method, s.username)
        if tok:
            self.query_one("#secret", Input).value = tok
            self._status("Token loaded from macOS Keychain.", "ok")

    def apply_detection(self, info: ServerInfo, preselect: bool = True) -> None:
        """Mark unsupported methods and (optionally) preselect the best one."""
        self.detected = info
        bearer = self.query_one("#m-bearer", RadioButton)
        basic = self.query_one("#m-basic", RadioButton)
        pat = info.supports_pat
        if pat is True:
            bearer.label = f"{BEARER_LABEL}  ✓ supported ({info.version})"
            basic.label = f"{BASIC_LABEL}  ✓ supported"
            if preselect:
                bearer.value = True
            self._detect_status(f"{info.describe()}: personal access tokens (Bearer) are supported. "
                                "Bearer preselected; Basic also works.", "ok")
        elif pat is False:
            why = "Jira Cloud uses Basic with email + API token" if info.is_cloud else \
                f"Jira {info.version or '?'} is older than 8.14"
            bearer.label = f"{BEARER_LABEL}  ✗ not supported ({why})"
            basic.label = f"{BASIC_LABEL}  ✓ supported"
            if preselect:
                basic.value = True
            self._detect_status(f"{info.describe()}: personal access tokens are NOT supported ({why}). "
                                "Basic preselected; you can still override.", "warn")
        else:
            bearer.label = BEARER_LABEL
            basic.label = BASIC_LABEL
            self._detect_status(f"{info.describe()}: version unknown, cannot tell whether PATs are supported.",
                                "warn")
        self.query_one("#secret-label", Label).update(
            "Personal access token" if self._method() == "bearer" else "Password (or token, if your server accepts it)")

    # ------------------------------------------------------------- events
    @on(Checkbox.Changed, "#insecure")
    def _insecure_changed(self, ev: Checkbox.Changed) -> None:
        self.query_one("#insecure-warning").set_class(ev.value, "show")
        if ev.value:
            self.notify("TLS verification disabled: INSECURE", severity="error", timeout=6)

    @on(RadioSet.Changed, "#method")
    def _method_changed(self) -> None:
        self.query_one("#secret-label", Label).update(
            "Personal access token" if self._method() == "bearer" else "Password (or token, if your server accepts it)")

    @on(Input.Changed, "#url")
    def _url_changed(self, ev: Input.Changed) -> None:
        try:
            same = self.detected is not None and normalize_base_url(ev.value) == self.detected.base_url
        except ConfigError:
            same = False
        if self.detected is not None and not same:
            self.detected = None
            self.query_one("#m-bearer", RadioButton).label = BEARER_LABEL
            self.query_one("#m-basic", RadioButton).label = BASIC_LABEL
            self._detect_status("URL changed: press Detect (Ctrl+T) to probe again.")

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        self.action_connect()

    @on(Button.Pressed, "#connect")
    def _connect_pressed(self) -> None:
        self.action_connect()

    @on(Button.Pressed, "#detect")
    def _detect_pressed(self) -> None:
        self.action_detect()

    @on(Button.Pressed, "#env")
    def action_load_env(self) -> None:
        self._apply_env(config.credentials_from_env(self.japp.env_source()))

    @on(Button.Pressed, "#quit")
    def _quit(self) -> None:
        self.app.exit()

    def action_cancel_connect(self) -> None:
        for w, what in ((self._connect_worker, "Connection attempt"), (self._detect_worker, "Detection")):
            if w is not None and w.is_running:
                w.cancel()
                self._status(f"{what} cancelled.", "error")

    def action_detect(self) -> None:
        if self._detect_worker is not None and self._detect_worker.is_running:
            return
        try:
            url = normalize_base_url(self.query_one("#url", Input).value)
            client = self.japp.make_client(url, None, self._tls())
        except ConfigError as exc:
            self._status(error_text(exc), "error")
            return
        except (OSError, ValueError) as exc:
            self._status(f"TLS setup failed: {exc}", "error")
            return
        if self.query_one("#url", Input).value != url:
            self.query_one("#url", Input).value = url
        self._detect_status(f"Probing {url}/rest/api/2/serverInfo …  (Esc to cancel)")
        self._detect_worker = self.run_worker(self._do_detect(client), group="detect", exit_on_error=False)

    async def _do_detect(self, client: JiraClient) -> None:
        try:
            info = await guarded(client.server_info(), self.japp.ui_timeout)
        except asyncio.CancelledError:
            self._detect_status("Detection cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001
            self._detect_status("")
            self._status("Detection failed.\n" + error_text(exc), "error")
            return
        finally:
            await client.aclose()
        self.apply_detection(info)

    def action_connect(self) -> None:
        if self._connect_worker is not None and self._connect_worker.is_running:
            return
        url_raw = self.query_one("#url", Input).value
        method = self._method()
        username = self.query_one("#username", Input).value.strip()
        secret = self.query_one("#secret", Input).value
        try:
            url = normalize_base_url(url_raw)
            auth = make_auth(method, username=username, secret=secret)
        except ConfigError as exc:
            self._status(error_text(exc), "error")
            return
        except ValueError as exc:
            self._status(str(exc), "error")
            return
        if self.query_one("#url", Input).value != url:
            self.query_one("#url", Input).value = url
        tls = self._tls()
        try:
            client = self.japp.make_client(url, auth, tls)
        except (OSError, ValueError) as exc:  # e.g. CA bundle missing/invalid
            self._status(f"TLS setup failed: {exc}", "error")
            return
        note = ""
        if self.detected is not None and method == "bearer" and self.detected.supports_pat is False:
            note = "  (Bearer is not supported by this server version; trying anyway)"
        self._status(f"Connecting to {url} …  (Esc to cancel){note}")
        self._connect_worker = self.run_worker(
            self._do_connect(client, method, username, secret, tls), group="connect", exit_on_error=False
        )

    async def _do_connect(self, client: JiraClient, method: str, username: str, secret: str, tls: TLSOptions) -> None:
        try:
            info = await guarded(client.test_connection(), self.japp.ui_timeout)
        except asyncio.CancelledError:
            await client.aclose()
            raise
        except Exception as exc:  # noqa: BLE001
            await client.aclose()
            self._status("Connection failed.\n" + error_text(exc), "error")
            return
        self.apply_detection(info, preselect=False)
        s = self.japp.settings
        s.base_url = client.base_url
        s.auth_method = method
        s.username = username
        s.ca_bundle = tls.ca_bundle or ""
        s.use_macos_keychain_cas = tls.use_macos_keychain
        s.insecure = not tls.verify
        s.remember = self.query_one("#remember", Checkbox).value
        want_keychain = self.query_one("#save_token", Checkbox).value
        notes = []
        if want_keychain and not self.japp.demo:
            if keychain.set_token(client.base_url, method, username, secret):
                s.token_in_keychain = True
                notes.append("token saved in Keychain")
            else:
                notes.append("could not save token in Keychain")
        elif s.token_in_keychain and not want_keychain:
            keychain.delete_token(client.base_url, method, username)
            s.token_in_keychain = False
        if (s.remember or s.token_in_keychain) and not self.japp.demo:
            config.save(s)
        elif not s.remember and not self.japp.demo:
            config.forget()
        msg = f"Connected as {info.username or 'unknown user'} ({info.describe()})"
        if notes:
            msg += "; " + ", ".join(notes)
        self._status(msg, "ok")
        self.japp.start_session(client, info)
