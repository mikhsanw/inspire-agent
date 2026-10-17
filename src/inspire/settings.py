"""Setelan di satu file: data/settings.json (izin 600).

Isi:
  keys     — rahasia, nama ala env (TELEGRAM_BOT_TOKEN, DEEPSEEK_API_KEY, …).
             Dipasang ke os.environ, jadi litellm & ${VAR} langsung membacanya.
  owner    — {"telegram": [id], "discord": [id]} hasil pairing.
  config   — pohon setelan, formatnya sama dengan config.yaml lama.
  skipped  — bagian yang sengaja dilewati (untuk status ⏸).
  pairing  — kode pairing selama belum ada pemilik.

Lapisan (belakang menimpa depan): DEFAULTS → settings.config → data/config.yaml.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import tempfile
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# Nilai ${VAR} diisi dari os.environ (= keys + env container).
DEFAULTS: dict = {
    "timezone": "Asia/Jakarta",
    "telegram": {"token": "${TELEGRAM_BOT_TOKEN}"},
    "discord": {"token": "${DISCORD_BOT_TOKEN}", "command_prefix": "!"},
    "models": {"default": "", "fallback": [], "list": {}},
    "shell": {"sandbox_url": "${SANDBOX_URL}", "token": "${SANDBOX_TOKEN}"},
    "opencode": {"key": "${OPENCODE_SANDBOX_KEY}"},
    "media": {
        "stt": {"api_key": "${GROQ_API_KEY}"},
        "inbox_dir": "${INSPIRE_INBOX}",
    },
}

LEGACY_KEYS = [
    "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
    "OPENCODE_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY",
]


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def home() -> Path:
    return Path(os.environ.get("INSPIRE_HOME", "data"))


class Settings:
    def __init__(self, data_dir: Path | None = None):
        self.dir = Path(data_dir or home())
        self.path = self.dir / "settings.json"
        self.overlay_path = self.dir / "config.yaml"
        self.data: dict = {}
        self.load()

    # --- file -----------------------------------------------------------

    def load(self) -> None:
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        else:
            self.data = {}
        self.data.setdefault("version", 1)
        for k, empty in (("keys", {}), ("owner", {}), ("config", {}), ("skipped", [])):
            self.data.setdefault(k, empty)
        self.apply_env()

    def save(self) -> None:
        """Tulis atomik dengan izin 600 (rahasia ada di dalamnya)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".settings-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self.apply_env()

    def exists(self) -> bool:
        return self.path.exists()

    # --- keys -----------------------------------------------------------

    def apply_env(self) -> None:
        for k, v in self.data["keys"].items():
            if v:
                os.environ[k] = v

    def key(self, name: str) -> str:
        return self.data["keys"].get(name) or os.environ.get(name, "")

    def set_key(self, name: str, value: str) -> None:
        value = value.strip()
        if value:
            self.data["keys"][name] = value
        else:
            self.data["keys"].pop(name, None)
            os.environ.pop(name, None)
        self.save()

    # --- pemilik & pairing ------------------------------------------------

    def owners(self, gateway: str) -> list[int]:
        return [int(x) for x in self.data["owner"].get(gateway, [])]

    def add_owner(self, gateway: str, user_id: int) -> None:
        ids = self.owners(gateway)
        if user_id not in ids:
            ids.append(user_id)
        self.data["owner"][gateway] = ids
        self.data.pop("pairing", None)
        self.save()

    def pairing_code(self, rotate: bool = False) -> str:
        if rotate or not self.data.get("pairing"):
            self.data["pairing"] = f"{secrets.randbelow(10**6):06d}"
            self.save()
        return self.data["pairing"]

    # --- config ---------------------------------------------------------

    def get(self, dotted: str, default=None):
        node = self.data["config"]
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value) -> None:
        node = self.data["config"]
        *parents, last = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        if value is None:
            node.pop(last, None)
        else:
            node[last] = value
        self.save()

    def overlay(self) -> dict:
        if not self.overlay_path.exists():
            return {}
        return yaml.safe_load(self.overlay_path.read_text(encoding="utf-8")) or {}

    def raw(self) -> dict:
        """Pohon setelan gabungan, siap di-parse config.from_raw()."""
        raw = deep_merge(deep_merge(DEFAULTS, self.data["config"]), self.overlay())
        raw["telegram"]["allowed_user_ids"] = self.owners("telegram") + _as_list(
            raw["telegram"].get("allowed_user_ids")
        )
        raw["discord"]["allowed_user_ids"] = self.owners("discord") + _as_list(
            raw["discord"].get("allowed_user_ids")
        )
        return raw

    # --- dilewati -------------------------------------------------------

    def skip(self, section: str, skipped: bool = True) -> None:
        s = set(self.data["skipped"])
        s.add(section) if skipped else s.discard(section)
        self.data["skipped"] = sorted(s)
        self.save()

    def is_skipped(self, section: str) -> bool:
        return section in self.data["skipped"]

    # --- model ----------------------------------------------------------

    def add_model(self, name: str, spec: dict, make_default: bool = True) -> None:
        models = self.data["config"].setdefault("models", {})
        models.setdefault("list", {})[name] = spec
        fb = models.setdefault("fallback", [])
        if name not in fb:
            fb.append(name)
        if make_default or not models.get("default"):
            models["default"] = name
        self.save()

    # --- migrasi dari .env + config.yaml lama -----------------------------

    def migrate_legacy(self, project_dir: Path) -> bool:
        """Sekali saja: kalau settings.json belum ada tapi ada .env / config.yaml lama."""
        if self.exists():
            return False
        env_file = project_dir / ".env"
        cfg_file = Path(os.environ.get("INSPIRE_CONFIG", project_dir / "config.yaml"))
        found = False
        if env_file.exists():
            from dotenv import dotenv_values

            for k, v in dotenv_values(env_file).items():
                if v and k not in ("SANDBOX_TOKEN", "LOG_LEVEL"):
                    self.data["keys"][k] = v
            found = True
        for k in LEGACY_KEYS:  # env container (env_file compose lama)
            if os.environ.get(k) and k not in self.data["keys"]:
                self.data["keys"][k] = os.environ[k]
                found = True
        if cfg_file.exists():
            old = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
            old.pop("data_dir", None)
            self.data["config"] = old
            found = True
        if not found:
            return False
        self.apply_env()
        for gw, env in (("telegram", "ALLOWED_USER_IDS"), ("discord", "DISCORD_USER_IDS")):
            ids = self.data["keys"].pop(env, "") or os.environ.get(env, "")
            if ids:
                self.data["owner"][gw] = _as_list(ids)
        # Rujukan ${…} ke id lama tak dibutuhkan lagi: pemilik kini di owner.
        for gw in ("telegram", "discord"):
            self.data["config"].get(gw, {}).pop("allowed_user_ids", None)
        self.save()
        log.info("setelan lama dipindah ke %s", self.path)
        return True


def _as_list(raw) -> list[int]:
    if not raw:
        return []
    if not isinstance(raw, list):
        raw = str(raw).replace(",", " ").split()
    out = []
    for x in raw:
        x = str(x).strip()
        if x.isdigit():
            out.append(int(x))
    return out
