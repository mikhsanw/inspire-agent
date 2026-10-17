"""Katalog model dari models.dev (sumber yang sama dengan OpenCode).

Isinya ±230 penyedia dan ribuan model, lengkap dengan dukungan tool calling, harga, ukuran
konteks, dan input gambar. Dipakai wizard / `/setup` supaya nama model selalu valid dan
pilihan tidak terbatas pada preset buatan tangan.

Catatan penting: "gratis" TIDAK bisa disimpulkan dari katalog. Free tier Gemini atau Groq
tercatat berharga normal, sedangkan harga $0 sering berarti langganan atau terkunci
(mis. tier gratis OpenCode hanya dari dalam OpenCode). Penyedia langganan disembunyikan,
dan harga $0 hanya ditampilkan apa adanya.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

URL = "https://models.dev/api.json"
MAX_AGE = 24 * 3600
CACHE_VERSION = 2   # naikkan kalau bentuk data cache berubah → cache lama otomatis diambil ulang

# Urutan tampil di pilihan penyedia.
POPULAR = ["openrouter", "deepseek", "google", "groq", "cerebras", "mistral", "nvidia",
           "openai", "anthropic", "xai", "togetherai", "fireworks-ai", "opencode"]
# litellm punya integrasi asli untuk ini (menangani kekhasan tiap API). Selain itu, penyedia
# yang punya URL API OpenAI-compatible dipakai lewat jalur generik openai/<model> + api_base.
NATIVE = {"openrouter": "openrouter", "deepseek": "deepseek", "google": "gemini", "groq": "groq",
          "cerebras": "cerebras", "mistral": "mistral", "anthropic": "anthropic",
          "openai": "openai", "xai": "xai", "togetherai": "together_ai", "deepinfra": "deepinfra",
          "cohere": "cohere_chat"}
# Nama key di settings (dipakai ulang oleh fitur lain: Groq untuk suara, Gemini untuk gambar).
KEY_ENV = {"google": "GEMINI_API_KEY"}
# Halaman untuk mengambil key (petunjuk; tak ada di katalog).
KEY_URL = {
    "opencode": "https://opencode.ai/console",
    "openrouter": "https://openrouter.ai/keys", "deepseek": "https://platform.deepseek.com/api_keys",
    "google": "https://aistudio.google.com/apikey", "groq": "https://console.groq.com/keys",
    "cerebras": "https://cloud.cerebras.ai/", "mistral": "https://console.mistral.ai/api-keys",
    "nvidia": "https://build.nvidia.com/", "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys", "xai": "https://console.x.ai/",
    "togetherai": "https://api.together.ai/settings/api-keys", "fireworks-ai": "https://fireworks.ai/account/api-keys",
}
# Tak bisa dipakai dengan cara kita: langganan (opencode-go) atau login OAuth, bukan API key.
HIDDEN = {"opencode-go", "github-copilot"}
# Penyedia yang model "$0"-nya terkunci ke aplikasinya sendiri: hanya model berbayarnya yang tampil.
LOCKED_FREE = {"opencode"}
# Catatan yang ditampilkan saat penyedia dipilih.
NOTES = {
    "opencode": ("OpenCode (Zen): hanya model BERBAYAR yang tampil — pakai saldo di opencode.ai/console. Model "
                 "gratis (-free) terkunci ke aplikasi OpenCode. Jalurnya sudah dicek menerima request, tapi "
                 "belum teruji sukses dengan key berisi saldo."),
}
OPENAI_COMPAT = "@ai-sdk/openai-compatible"
ANTHROPIC = "@ai-sdk/anthropic"
HIDDEN_PARTS = ("-plan", "token-plan", "coding-plan")  # langganan: bukan API umum
HIDDEN_NOTE = ("Penyedia langganan / login OAuth tidak ditampilkan, begitu pula penyedia yang jalur API-nya "
               "belum didukung.")

SORTS = {"baru": "terbaru", "harga": "termurah", "konteks": "konteks terbesar"}


@dataclass
class CModel:
    id: str
    name: str
    provider: str
    context: int = 0
    output: int = 0
    cost_in: float | None = None     # dolar per 1 juta token
    cost_out: float | None = None
    vision: bool = False
    released: str = ""
    status: str = ""                 # "", beta, alpha, deprecated
    npm: str = ""                    # jalur API KHUSUS model ini (bila beda dari penyedia)
    api: str = ""

    @property
    def free(self) -> bool:
        return self.cost_in == 0 and self.cost_out == 0

    def price(self) -> str:
        if self.cost_in is None or self.cost_out is None:
            return "harga ?"
        if self.free:
            return "$0"
        return f"${self.cost_in:g}/${self.cost_out:g}"

    def label(self) -> str:
        ctx = f"{self.context // 1000}k" if self.context else "?"
        flags = ("👁" if self.vision else "") + (" β" if self.status in ("beta", "alpha") else "")
        return f"{self.id}  ·  {ctx}  ·  {self.price()}{('  ' + flags.strip()) if flags.strip() else ''}"


@dataclass
class CProvider:
    id: str
    name: str
    env: list[str] = field(default_factory=list)
    api: str | None = None
    npm: str = ""
    models: dict[str, CModel] = field(default_factory=dict)

    @property
    def supported(self) -> bool:
        return self.id in NATIVE or bool(self.api)

    @property
    def hidden(self) -> bool:
        return self.id in HIDDEN or any(p in self.id for p in HIDDEN_PARTS)

    @property
    def key_env(self) -> str:
        return KEY_ENV.get(self.id) or (self.env[0] if self.env else f"{self.id.upper().replace('-', '_')}_API_KEY")

    @property
    def local(self) -> bool:
        host = urlparse(self.api or "").hostname or ""
        return host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local")

    @property
    def key_required(self) -> bool:
        return not self.local


@dataclass
class Resolved:
    model: str                 # nama untuk litellm
    api_base: str | None
    key_env: str
    key_required: bool


def resolve(provider: CProvider, model: CModel) -> Resolved | None:
    """Petakan (penyedia, model) katalog → parameter litellm. None = tak bisa dipakai.

    Penyedia dengan integrasi asli litellm (NATIVE) menangani semua modelnya sendiri. Selain itu
    katalog menandai jalur API PER MODEL (mis. OpenCode: sebagian OpenAI-compatible, sebagian
    Anthropic, sebagian /responses). Yang kita dukung: openai-compatible dan anthropic; sisanya
    (jalur /responses, SDK khusus) disembunyikan daripada ditawarkan lalu gagal."""
    if provider.id in NATIVE:
        return Resolved(f"{NATIVE[provider.id]}/{model.id}", None, provider.key_env, True)
    npm = model.npm or provider.npm
    api = model.api or provider.api
    if not api:
        return None
    if npm in ("", OPENAI_COMPAT):
        return Resolved(f"openai/{model.id}", api, provider.key_env, provider.key_required)
    if npm == ANTHROPIC:
        # litellm menambahkan /v1/messages sendiri; URL katalog (gaya AI SDK) berakhiran /v1.
        base = api[:-3] if api.rstrip("/").endswith("/v1") else api
        return Resolved(f"anthropic/{model.id}", base.rstrip("/"), provider.key_env, True)
    return None


class Catalog:
    def __init__(self, providers: dict[str, CProvider], fetched: float = 0):
        self.providers = providers
        self.fetched = fetched

    def get(self, pid: str) -> CProvider | None:
        return self.providers.get(pid)

    def popular(self) -> list[CProvider]:
        return [self.providers[p] for p in POPULAR
                if p in self.providers and self.providers[p].supported and self.models(p)]

    def search_providers(self, query: str) -> list[CProvider]:
        toks = query.lower().split()
        found = [
            p for p in self.providers.values()
            if p.supported and not p.hidden and p.models
            and all(t in p.id.lower() or t in p.name.lower() for t in toks)
            and self.models(p.id)                            # minimal satu model yang bisa dipakai
        ]
        return sorted(found, key=lambda p: (p.id not in POPULAR, p.name.lower()))

    def models(self, pid: str, query: str = "", free: bool = False, sort: str = "baru",
               deprecated: bool = False) -> list[CModel]:
        prov = self.providers.get(pid)
        if not prov:
            return []
        toks = query.lower().split()
        locked = pid in LOCKED_FREE
        out = [
            m for m in prov.models.values()
            if (deprecated or m.status != "deprecated")
            and (not free or m.free)
            and not (locked and m.free)                      # model "$0" terkunci → tak ditawarkan
            and resolve(prov, m) is not None                 # jalur API-nya harus kita dukung
            and all(t in m.id.lower() or t in m.name.lower() for t in toks)
        ]
        if sort == "harga":
            out.sort(key=lambda m: ((m.cost_in if m.cost_in is not None else 1e9)
                                    + (m.cost_out if m.cost_out is not None else 1e9), m.id))
        elif sort == "konteks":
            out.sort(key=lambda m: (-m.context, m.id))
        else:  # terbaru
            out.sort(key=lambda m: (m.released or "", m.id), reverse=True)
        return out

    def age_hours(self) -> float:
        return (time.time() - self.fetched) / 3600 if self.fetched else float("inf")


# --- baca / simpan ------------------------------------------------------------------


def compact(raw: dict) -> dict:
    """api.json penuh (±6MB) → hanya yang kita pakai: model dengan tool calling (±1MB)."""
    out = {}
    for pid, p in raw.items():
        models = {}
        for mid, m in (p.get("models") or {}).items():
            if not m.get("tool_call"):
                continue
            cost, limit = m.get("cost") or {}, m.get("limit") or {}
            models[mid] = {
                "n": m.get("name") or mid, "c": limit.get("context") or 0, "o": limit.get("output") or 0,
                "i": cost.get("input"), "p": cost.get("output"),
                "v": "image" in ((m.get("modalities") or {}).get("input") or []),
                "r": m.get("release_date") or "", "s": m.get("status") or "",
            }
            ov = m.get("provider") or {}
            if ov.get("npm"):
                models[mid]["x"] = ov["npm"]
            if ov.get("api"):
                models[mid]["a"] = ov["api"]
        if models:
            out[pid] = {"name": p.get("name") or pid, "env": p.get("env") or [], "api": p.get("api"),
                        "npm": p.get("npm") or "", "models": models}
    return out


def parse(data: dict, fetched: float = 0) -> Catalog:
    providers = {}
    for pid, p in data.items():
        models = {
            mid: CModel(mid, m["n"], pid, m["c"], m["o"], m["i"], m["p"], m["v"], m["r"], m["s"],
                        m.get("x", ""), m.get("a", ""))
            for mid, m in p["models"].items()
        }
        providers[pid] = CProvider(pid, p["name"], p["env"], p["api"], p.get("npm", ""), models)
    return Catalog(providers, fetched)


def _read(cache: Path) -> tuple[dict, float] | None:
    try:
        blob = json.loads(cache.read_text(encoding="utf-8"))
        if blob.get("version") != CACHE_VERSION:
            return None
        return blob["providers"], float(blob["fetched"])
    except (OSError, ValueError, KeyError):
        return None


def _write(cache: Path, providers: dict) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=".catalog-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"version": CACHE_VERSION, "fetched": time.time(), "providers": providers}, f, separators=(",", ":"))
    os.replace(tmp, cache)


async def load(cache: Path, max_age: int = MAX_AGE, force: bool = False) -> Catalog | None:
    """Katalog dari cache kalau masih segar; selain itu ambil dari models.dev. Kalau jaringan
    gagal, pakai cache lama. None = belum pernah berhasil mengambil."""
    cached = _read(cache)
    if cached and not force and time.time() - cached[1] < max_age:
        return parse(*cached)
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            r = await c.get(URL)
        r.raise_for_status()
        providers = compact(r.json())
        if not providers:
            raise ValueError("katalog kosong")
        _write(cache, providers)
        return parse(providers, time.time())
    except Exception as e:
        log.warning("gagal mengambil katalog model: %s", e)
        return parse(*cached) if cached else None
