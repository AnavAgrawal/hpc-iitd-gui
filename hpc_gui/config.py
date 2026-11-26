"""Configuration: non-secret settings in JSON, password in OS keyring."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

try:
    import keyring
    _HAS_KEYRING = True
except Exception:  # pragma: no cover - keyring may fail to import on odd setups
    _HAS_KEYRING = False

SERVICE_NAME = "hpc-iitd-gui"

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home() / ".config")) / "hpc-iitd-gui"
CONFIG_FILE = CONFIG_DIR / "settings.json"


@dataclass
class Settings:
    host: str = "hpc.iitd.ac.in"
    username: str = ""
    project: str = ""
    remote_workdir: str = ""
    email: str = ""
    save_password: bool = True
    auto_connect: bool = False
    auto_refresh_jobs: bool = True
    auto_refresh_interval: int = 15
    recent_pbs_scripts: list = field(default_factory=list)
    recent_log_paths: list = field(default_factory=list)
    recent_remote_dirs: list = field(default_factory=list)
    # Window/UI state
    window_geometry: str = ""   # base64-encoded QByteArray
    window_state: str = ""      # base64-encoded QByteArray
    last_tab: int = 1           # dashboard by default
    splitters: dict = field(default_factory=dict)  # name -> base64 state
    word_wrap_logs: bool = False
    # Saved login profiles. Each entry is a dict with keys
    # name/host/username/project/remote_workdir/email. The password lives in
    # the OS keyring and is keyed by (host, username) as before.
    profiles: list = field(default_factory=list)
    active_profile: str = ""


def load_settings() -> Settings:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            valid = {k: v for k, v in data.items() if k in Settings.__dataclass_fields__}
            return Settings(**valid)
        except Exception:
            pass
    return Settings()


def save_settings(settings: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


def save_password(host: str, username: str, password: str) -> bool:
    if not _HAS_KEYRING or not password:
        return False
    try:
        keyring.set_password(f"{SERVICE_NAME}:{host}", username, password)
        return True
    except Exception:
        return False


def load_password(host: str, username: str) -> Optional[str]:
    if not _HAS_KEYRING or not username:
        return None
    try:
        return keyring.get_password(f"{SERVICE_NAME}:{host}", username)
    except Exception:
        return None


def delete_password(host: str, username: str) -> None:
    if not _HAS_KEYRING:
        return
    try:
        keyring.delete_password(f"{SERVICE_NAME}:{host}", username)
    except Exception:
        pass


PROFILE_FIELDS = ("name", "host", "username", "project", "remote_workdir", "email")


def profile_from_settings(s: "Settings", name: str) -> dict:
    """Snapshot the credential-related fields of ``s`` into a profile dict."""
    return {
        "name": name,
        "host": s.host,
        "username": s.username,
        "project": s.project,
        "remote_workdir": s.remote_workdir,
        "email": s.email,
    }


def find_profile(profiles: list, name: str) -> Optional[dict]:
    for p in profiles:
        if isinstance(p, dict) and p.get("name") == name:
            return p
    return None


def upsert_profile(profiles: list, profile: dict) -> list:
    """Insert or replace a profile by name. Returns the same list (mutated)."""
    name = profile.get("name", "")
    for i, p in enumerate(profiles):
        if isinstance(p, dict) and p.get("name") == name:
            profiles[i] = profile
            return profiles
    profiles.append(profile)
    return profiles


def push_recent(lst: list, value: str, limit: int = 10) -> list:
    """Return a new list with value moved to front; capped at limit."""
    if not value:
        return lst
    out = [value] + [x for x in lst if x != value]
    return out[:limit]
