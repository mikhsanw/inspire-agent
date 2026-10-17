import json
import time

import httpx
import pytest

from inspire import catalog as C

RAW = {
    "deepseek": {
        "name": "DeepSeek", "env": ["DEEPSEEK_API_KEY"], "api": "https://api.deepseek.com",
        "models": {
            "deepseek-flash": {"name": "DeepSeek Flash", "tool_call": True, "release_date": "2026-08-01",
                               "limit": {"context": 1000000, "output": 64000}, "cost": {"input": 0.15, "output": 0.6},
                               "modalities": {"input": ["text", "image"]}},
            "deepseek-v4-pro": {"name": "DeepSeek V4 Pro", "tool_call": True, "release_date": "2026-06-01",
                                "limit": {"context": 1000000}, "cost": {"input": 0.66, "output": 1.98}},
            "deepseek-chat": {"name": "old", "tool_call": True, "status": "deprecated", "release_date": "2025-01-01",
                              "limit": {"context": 64000}, "cost": {"input": 0.3, "output": 1}},
            "no-tools": {"name": "x", "tool_call": False, "limit": {"context": 1000}},
        },
    },
    "openrouter": {
        "name": "OpenRouter", "env": ["OPENROUTER_API_KEY"], "api": "https://openrouter.ai/api/v1",
        "models": {
            "google/gemma-4-31b-it:free": {"name": "Gemma 4 31B", "tool_call": True, "release_date": "2026-05-01",
                                           "limit": {"context": 262144}, "cost": {"input": 0, "output": 0}},
            "vendor/cheap": {"name": "Cheap", "tool_call": True, "release_date": "2026-09-01",
                             "limit": {"context": 32000}, "cost": {"input": 0.05, "output": 0.1}},
        },
    },
    "google": {"name": "Google", "env": ["GOOGLE_API_KEY", "GEMINI_API_KEY"], "models": {
        "gemini-3.6-flash": {"name": "Gemini Flash", "tool_call": True, "limit": {"context": 1000000},
                             "cost": {"input": 0.75, "output": 3.75}, "modalities": {"input": ["text", "image"]}}}},
    "opencode": {
        "name": "OpenCode Zen", "api": "https://opencode.ai/inference/openai/v1", "npm": "@ai-sdk/openai-compatible",
        "env": ["OPENCODE_API_KEY"], "models": {
            "big-pickle": {"name": "Big Pickle", "tool_call": True, "limit": {"context": 200000},
                           "cost": {"input": 0, "output": 0}},                                  # gratis → terkunci
            "glm-5.3-flash": {"name": "GLM Flash", "tool_call": True, "limit": {"context": 200000},
                              "cost": {"input": 0.1, "output": 0.3}, "release_date": "2026-09-01"},
            "claude-haiku-4-5": {"name": "Claude Haiku", "tool_call": True, "limit": {"context": 200000},
                                 "cost": {"input": 1, "output": 5}, "release_date": "2026-08-01",
                                 "provider": {"npm": "@ai-sdk/anthropic", "api": "https://opencode.ai/inference/anthropic/v1"}},
            "gpt-5.4": {"name": "GPT 5.4", "tool_call": True, "limit": {"context": 400000}, "cost": {"input": 2, "output": 8},
                        "provider": {"npm": "@ai-sdk/openai", "api": "https://opencode.ai/inference/openai/v1"}},
        }},
    "opencode-go": {"name": "OpenCode Go", "api": "https://opencode.ai/inference/go/openai/v1", "env": ["OPENCODE_API_KEY"],
                    "models": {"m": {"name": "m", "tool_call": True, "limit": {"context": 1}, "cost": {"input": 0.1, "output": 0.1}}}},
    "github-copilot": {"name": "GitHub Copilot", "api": "https://api.githubcopilot.com", "env": [], "models": {
        "m": {"name": "m", "tool_call": True, "limit": {"context": 1}, "cost": {"input": 0, "output": 0}}}},
    "merge-gateway": {"name": "Merge", "api": "https://x/v1", "npm": "merge-gateway-ai-sdk-provider", "env": ["K"], "models": {
        "m": {"name": "m", "tool_call": True, "limit": {"context": 1}, "cost": {"input": 1, "output": 1}}}},
    "alibaba-token-plan": {"name": "Alibaba plan", "api": "https://x/v1", "env": ["K"], "models": {
        "qwen": {"name": "Qwen", "tool_call": True, "limit": {"context": 1}, "cost": {"input": 0, "output": 0}}}},
    "azure": {"name": "Azure", "env": ["AZURE_API_KEY"], "models": {"gpt": {"name": "gpt", "tool_call": True, "limit": {}}}},
    "lmstudio": {"name": "LM Studio", "api": "http://127.0.0.1:1234/v1", "env": ["LMSTUDIO_API_KEY"], "models": {
        "qwen/qwen3": {"name": "Qwen3", "tool_call": True, "limit": {"context": 32000}}}},
    "kosong": {"name": "Tanpa tool", "models": {"m": {"name": "m", "tool_call": False}}},
}


@pytest.fixture
def cat():
    return C.parse(C.compact(RAW), time.time())


def test_compact_keeps_only_tool_models_and_drops_empty_providers():
    data = C.compact(RAW)
    assert "kosong" not in data
    assert "no-tools" not in data["deepseek"]["models"] and "deepseek-flash" in data["deepseek"]["models"]
    assert len(json.dumps(data)) < len(json.dumps(RAW))


def test_provider_visibility(cat):
    assert [p.id for p in cat.popular()] == ["openrouter", "deepseek", "google", "opencode"]   # urutan POPULAR
    ids = [p.id for p in cat.search_providers("")]
    assert {"opencode", "deepseek", "openrouter", "google", "lmstudio"} <= set(ids)     # OpenCode tersedia (berbayar)
    assert "alibaba-token-plan" not in ids and "opencode-go" not in ids                 # langganan
    assert "github-copilot" not in ids                                                  # login OAuth, bukan API key
    assert "merge-gateway" not in ids                                                   # SDK khusus: tak satu pun model bisa dipakai
    assert "azure" not in ids                                                           # tak ada URL & bukan native
    assert [p.id for p in cat.search_providers("deep xyz")] == []                       # semua kata harus cocok
    assert [p.id for p in cat.search_providers("lm studio")] == ["lmstudio"]            # kata terpisah, cocok per kata


def test_model_filters_and_sorts(cat):
    ids = lambda **kw: [m.id for m in cat.models("deepseek", **kw)]  # noqa: E731
    assert ids() == ["deepseek-flash", "deepseek-v4-pro"]                               # deprecated disembunyikan
    assert "deepseek-chat" in ids(deprecated=True)
    assert ids(query="PRO") == ["deepseek-v4-pro"] and ids(query="flash deep") == ["deepseek-flash"]
    assert ids(sort="harga") == ["deepseek-flash", "deepseek-v4-pro"]
    assert [m.id for m in cat.models("openrouter", free=True)] == ["google/gemma-4-31b-it:free"]
    assert [m.id for m in cat.models("openrouter", sort="baru")][0] == "vendor/cheap"   # terbaru dulu
    assert [m.id for m in cat.models("openrouter", sort="konteks")][0] == "google/gemma-4-31b-it:free"
    assert cat.models("tidak-ada") == []


def test_labels(cat):
    m = cat.get("deepseek").models["deepseek-flash"]
    assert m.label() == "deepseek-flash  ·  1000k  ·  $0.15/$0.6  👁" and not m.free
    free = cat.get("openrouter").models["google/gemma-4-31b-it:free"]
    assert free.free and free.price() == "$0" and "262k" in free.label()
    assert C.CModel("x", "x", "p").price() == "harga ?"


def test_resolve_native_generic_and_local(cat):
    ds = cat.get("deepseek")
    r = C.resolve(ds, ds.models["deepseek-flash"])
    assert (r.model, r.api_base, r.key_env, r.key_required) == ("deepseek/deepseek-flash", None, "DEEPSEEK_API_KEY", True)
    g = cat.get("google")
    assert C.resolve(g, g.models["gemini-3.6-flash"]).model == "gemini/gemini-3.6-flash"
    assert C.resolve(g, g.models["gemini-3.6-flash"]).key_env == "GEMINI_API_KEY"        # dipakai ulang fitur gambar
    lm = cat.get("lmstudio")
    r = C.resolve(lm, lm.models["qwen/qwen3"])
    assert r.model == "openai/qwen/qwen3" and r.api_base == "http://127.0.0.1:1234/v1" and not r.key_required
    az = C.CProvider("azure", "Azure")
    assert C.resolve(az, C.CModel("m", "m", "azure")) is None


def mock_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_load_fetches_then_uses_fresh_cache_then_force(tmp_path, monkeypatch):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(200, json=RAW)

    mock_http(monkeypatch, handler)
    cache = tmp_path / "d" / "catalog.json"
    cat = await C.load(cache)
    assert cat.get("deepseek") and cache.exists() and len(calls) == 1
    assert (await C.load(cache)).get("deepseek") and len(calls) == 1                    # cache segar: tak ambil lagi
    await C.load(cache, force=True)
    assert len(calls) == 2


async def test_load_offline_uses_stale_cache_or_none(tmp_path, monkeypatch):
    def down(req):
        raise httpx.ConnectError("offline")

    mock_http(monkeypatch, down)
    cache = tmp_path / "catalog.json"
    assert await C.load(cache) is None                                                  # belum pernah berhasil
    cache.write_text(json.dumps({"version": C.CACHE_VERSION, "fetched": 1.0, "providers": C.compact(RAW)}))
    stale = await C.load(cache)
    assert stale.get("deepseek") and stale.age_hours() > 1000                           # tetap dipakai walau basi


async def test_load_bad_payload_keeps_cache(tmp_path, monkeypatch):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json={}))
    cache = tmp_path / "catalog.json"
    cache.write_text(json.dumps({"version": C.CACHE_VERSION, "fetched": 1.0, "providers": C.compact(RAW)}))
    assert (await C.load(cache)).get("deepseek")


def test_together_and_unsupported_providers():
    raw = {
        "togetherai": {"name": "Together AI", "env": ["TOGETHER_API_KEY"], "models": {
            "meta/llama": {"name": "Llama", "tool_call": True, "limit": {"context": 1}}}},
        "nvidia": {"name": "Nvidia", "env": ["NVIDIA_API_KEY"], "models": {                # tanpa URL & bukan native
            "m": {"name": "m", "tool_call": True, "limit": {}}}},
    }
    cat = C.parse(C.compact(raw))
    tg = cat.get("togetherai")
    assert C.resolve(tg, tg.models["meta/llama"]).model == "together_ai/meta/llama"
    assert [p.id for p in cat.popular()] == ["togetherai"]                                # yang tak didukung tak ditawarkan


def test_opencode_only_paid_models_and_per_model_routes(cat):
    ids = [m.id for m in cat.models("opencode")]
    assert "big-pickle" not in ids                                       # gratis: terkunci ke aplikasi OpenCode
    assert "gpt-5.4" not in ids                                          # jalur /responses: belum didukung
    assert sorted(ids) == ["claude-haiku-4-5", "glm-5.3-flash"]
    assert cat.models("opencode", free=True) == []
    oc = cat.get("opencode")
    r = C.resolve(oc, oc.models["glm-5.3-flash"])                        # openai-compatible → jalur generik
    assert (r.model, r.api_base) == ("openai/glm-5.3-flash", "https://opencode.ai/inference/openai/v1")
    r = C.resolve(oc, oc.models["claude-haiku-4-5"])                     # jalur Anthropic, tanpa akhiran /v1
    assert (r.model, r.api_base, r.key_env) == ("anthropic/claude-haiku-4-5", "https://opencode.ai/inference/anthropic",
                                                "OPENCODE_API_KEY")
    assert C.resolve(oc, oc.models["gpt-5.4"]) is None
    assert "terkunci" in C.NOTES["opencode"] or "gratis" in C.NOTES["opencode"]


def test_old_cache_format_is_refetched(tmp_path):
    cache = tmp_path / "catalog.json"
    cache.write_text(json.dumps({"fetched": time.time(), "providers": C.compact(RAW)}))   # tanpa versi
    assert C._read(cache) is None
