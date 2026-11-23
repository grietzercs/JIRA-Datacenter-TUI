"""Command-line entry point: ``jiratui`` / ``python -m jiratui``."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List, Mapping, Optional, Tuple

from . import __version__, config
from .logsetup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jiratui",
        description="Terminal UI for Jira Server / Data Center. Tokens are never accepted as "
        "command-line arguments; use the login form, JIRA_PAT/JIRA_PASSWORD env vars, "
        "an --env-file, or the opt-in macOS Keychain.",
    )
    p.add_argument("--url", help="Jira base URL, e.g. https://jira.example.com (env: JIRA_URL)")
    p.add_argument("--auth", choices=["bearer", "basic"], help="auth method (env: JIRA_AUTH)")
    p.add_argument("--user", help="username (env: JIRA_USER)")
    p.add_argument("--env-file", metavar="PATH", help="read JIRA_* values from a .env file")
    p.add_argument("--ca-bundle", metavar="PEM", help="extra CA bundle to trust (env: JIRA_CA_BUNDLE)")
    p.add_argument("--no-keychain-cas", action="store_true", help="do not load CA certs from the macOS Keychain")
    p.add_argument("--insecure", action="store_true", help="DISABLE TLS verification (dangerous; diagnosis only)")
    p.add_argument("--timeout", type=float, default=None, help="per-request network timeout in seconds (default 30)")
    p.add_argument("--connect", action="store_true", help="connect immediately if URL and token are available")
    p.add_argument("--debug-log", metavar="PATH", default=os.environ.get("JIRATUI_DEBUG_LOG"),
                   help="write a redacted debug log (env: JIRATUI_DEBUG_LOG)")
    p.add_argument("--forget", action="store_true", help="delete remembered settings and exit")
    p.add_argument("--demo", action="store_true", help="run against a built-in fake Jira server (no network)")
    p.add_argument("--version", action="version", version=f"jiratui {__version__}")
    return p


def resolve_settings(args: argparse.Namespace, environ: Mapping[str, str]) -> Tuple[config.Settings, config.EnvCredentials, Dict[str, str]]:
    """Precedence: CLI flags > environment / --env-file > remembered config > defaults."""
    env: Dict[str, str] = dict(environ)
    if args.env_file:
        env.update(config.load_dotenv_file(args.env_file))
    settings = config.load_saved()
    creds = config.credentials_from_env(env)
    if creds.auth_method:
        settings.auth_method = creds.auth_method
    if creds.username:
        settings.username = creds.username
    if creds.base_url:
        settings.base_url = creds.base_url
    if creds.ca_bundle:
        settings.ca_bundle = creds.ca_bundle
    if args.url:
        settings.base_url = creds.base_url = args.url
    if args.auth:
        settings.auth_method = creds.auth_method = args.auth
    if args.user:
        settings.username = creds.username = args.user
    if args.ca_bundle:
        settings.ca_bundle = creds.ca_bundle = args.ca_bundle
    if args.no_keychain_cas:
        settings.use_macos_keychain_cas = False
    if args.insecure:
        settings.insecure = True
    if args.timeout:
        settings.request_timeout = max(1.0, args.timeout)
    return settings, creds, env


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.forget:
        config.forget()
        print(f"Removed {config.config_path()} (Keychain items, if any, remain under service 'jira-tui'; "
              "saved filters/queries in the same directory are kept).")
        return 0

    log_path = setup_logging(args.debug_log)
    try:
        settings, creds, env = resolve_settings(args, os.environ)
    except OSError as exc:
        print(f"Cannot read env file {args.env_file}: {exc}", file=sys.stderr)
        return 2

    transport_factory = None
    filter_store = query_store = view_state = None
    if args.demo:
        import tempfile

        from .filters import FilterStore, QueryStore, ViewStateStore
        from .testing.fake_server import DEMO_TOKEN, DEMO_URL, DEMO_USER, FakeJira

        fake = FakeJira()
        transport_factory = fake.transport
        settings = config.Settings(base_url=DEMO_URL, auth_method="bearer", username=DEMO_USER,
                                   use_macos_keychain_cas=False, last_project="PLAT")
        creds = config.EnvCredentials(base_url=DEMO_URL, username=DEMO_USER, auth_method="bearer", secret=DEMO_TOKEN)
        # The demo never touches your real saved filters / query history.
        demo_dir = tempfile.mkdtemp(prefix="jiratui-demo-")
        filter_store = FilterStore(os.path.join(demo_dir, "filters.json"))
        query_store = QueryStore(os.path.join(demo_dir, "queries.json"))
        view_state = ViewStateStore(os.path.join(demo_dir, "view_state.json"))

    from .tui.app import JiraApp

    app = JiraApp(
        settings,
        env_creds=creds,
        env=env,
        transport_factory=transport_factory,
        auto_connect=args.connect or args.demo,
        ui_timeout=max(30.0, settings.request_timeout * 2 + 5),
        demo=args.demo,
        filter_store=filter_store,
        query_store=query_store,
        view_state=view_state,
    )
    app.run()
    if log_path:
        print(f"Debug log (redacted): {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
