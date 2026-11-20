"""Settings: CLI > environment (incl. --env-file) > remembered config > defaults.

Only non-secret values are ever written to the config file. Tokens can be
persisted *only* through the OS keychain (see :mod:`jiratui.keychain`), opt-in.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Mapping, Optional

log = logging.getLogger("jiratui.config")

ENV_URL = ("JIRA_URL",)
ENV_USER = ("JIRA_USER", "JIRA_USERNAME")
ENV_TOKEN = ("JIRA_PAT", "JIRA_TOKEN")
ENV_PASSWORD = ("JIRA_PASSWORD",)
ENV_AUTH = ("JIRA_AUTH", "JIRA_AUTH_METHOD")
ENV_CA = ("JIRA_CA_BUNDLE",)


@dataclass
class Settings:
    base_url: str = ""
    auth_method: str = "bearer"  # bearer | basic
    username: str = ""
    ca_bundle: str = ""
    use_macos_keychain_cas: bool = sys.platform == "darwin"
    insecure: bool = False
    remember: bool = False
    token_in_keychain: bool = False
    last_project: str = ""
    request_timeout: float = 30.0

    # Persisted keys. Never add a secret here.
    PERSISTED = (
        "base_url",
        "auth_method",
        "username",
        "ca_bundle",
        "use_macos_keychain_cas",
        "remember",
        "token_in_keychain",
        "last_project",
    )


def config_dir() -> str:
    """``$JIRATUI_CONFIG_DIR`` or ``$XDG_CONFIG_HOME/jira-tui`` (default ``~/.config/jira-tui``)."""
    override = os.environ.get("JIRATUI_CONFIG_DIR")
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "jira-tui")


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def write_json_private(path: str, data: Any) -> str:
    """Atomically write JSON with mode 0600 (directory 0700)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    return path


def read_json(path: str) -> Optional[Any]:
    """Read a JSON file; ``None`` if missing or unreadable (logged, never fatal)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable file %s: %s", path, exc)
        return None


def load_saved(settings: Optional[Settings] = None) -> Settings:
    s = settings or Settings()
    data = read_json(config_path())
    if not isinstance(data, dict):
        return s
    names = {f.name for f in fields(Settings)}
    for k, v in data.items():
        if k in Settings.PERSISTED and k in names:
            setattr(s, k, v)
    return s


def save(settings: Settings) -> str:
    """Persist non-secret settings (0600). Returns the path written."""
    data = {k: v for k, v in asdict(settings).items() if k in Settings.PERSISTED}
    return write_json_private(config_path(), data)


def forget() -> None:
    try:
        os.remove(config_path())
    except FileNotFoundError:
        pass


_DOTENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def parse_dotenv(text: str) -> Dict[str, str]:
    """Minimal .env parser: KEY=VALUE, optional 'export', quotes, # comments."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _DOTENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
            if m.group(2).strip().startswith('"'):
                val = val.replace("\\n", "\n").replace('\\"', '"')
        else:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        out[key] = val
    return out


def load_dotenv_file(path: str) -> Dict[str, str]:
    with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
        return parse_dotenv(fh.read())


def _first(env: Mapping[str, str], names) -> str:
    for n in names:
        v = env.get(n)
        if v:
            return v
    return ""


@dataclass
class EnvCredentials:
    base_url: str = ""
    username: str = ""
    auth_method: str = ""
    secret: str = ""
    ca_bundle: str = ""

    def __repr__(self) -> str:  # never show the secret
        return (
            f"EnvCredentials(base_url={self.base_url!r}, username={self.username!r}, "
            f"auth_method={self.auth_method!r}, secret={'set' if self.secret else 'unset'})"
        )


def credentials_from_env(env: Mapping[str, str]) -> EnvCredentials:
    token = _first(env, ENV_TOKEN)
    password = _first(env, ENV_PASSWORD)
    method = _first(env, ENV_AUTH).lower()
    username = _first(env, ENV_USER)
    if not method:
        if password and not token:
            method = "basic"
        elif token:
            method = "bearer"
    if method == "basic":
        secret = password or token
    else:
        secret = token or password
    return EnvCredentials(
        base_url=_first(env, ENV_URL),
        username=username,
        auth_method=method,
        secret=secret,
        ca_bundle=_first(env, ENV_CA),
    )
