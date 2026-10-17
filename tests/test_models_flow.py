"""Alur menambah model: inti setup, wizard terminal, dan /setup Telegram (semua dari katalog)."""

import os
from types import SimpleNamespace

import pytest

from inspire import catalog as C
from inspire import setup as S
from inspire.config import from_settings
from inspire.settings import Settings
from inspire.setup import wizard

from test_catalog import RAW

BAD_MODEL = "BadRequestError: OpenAIException - Upstream request failed: Model is unavailable."


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CUSTOM_CUSTOM_BIG"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def cat():
    return C.parse(C.compact(RAW), 0)


@pytest.fixture
def tested(monkeypatch):
    """Ganti tes model sungguhan; catat spec yang dites. Atur hasil lewat tested.result."""
    box = SimpleNamespace(specs=[], result=lambda spec: (True, "OK"))

    async def fake(spec):
        box.specs.append(spec)
        return box.result(spec)

    monkeypatch.setattr(S.checks, "model", fake)
    return box


# --- inti: tes dulu, simpan kemudian -------------------------------------------------------------

async def test_add_catalog_model_saves_spec_key_and_metadata(tmp_path, cat, tested):
    s = Settings(tmp_path)
    ds = cat.get("deepseek")
    ok, msg = await S.add_catalog_model(s, ds, ds.models["deepseek-flash"], key="sk-bagus")
    assert ok and msg.startswith("deepseek-flash siap")
    assert tested.specs[0]["api_key"] == "sk-bagus" and tested.specs[0]["model"] == "deepseek/deepseek-flash"
    spec = s.get("models.list")["deepseek-flash"]
    assert spec == {"model": "deepseek/deepseek-flash", "api_key": "${DEEPSEEK_API_KEY}", "vision": True,
                    "context": 1000000, "cost_in": 0.15, "cost_out": 0.6}
    assert "sk-bagus" not in str(s.data["config"]) and s.key("DEEPSEEK_API_KEY") == "sk-bagus"
    cfg = from_settings(s)
    m = cfg.models["deepseek-flash"]
    assert m.api_key == "sk-bagus" and m.context == 1000000 and m.vision and m.cost_out == 0.6   # ${…} terisi
    assert cfg.default_model == "deepseek-flash"


async def test_names_are_unique_and_short(tmp_path, cat, tested):
    s = Settings(tmp_path)
    s.set_key("OPENROUTER_API_KEY", "k")
    orr = cat.get("openrouter")
    await S.add_catalog_model(s, orr, orr.models["google/gemma-4-31b-it:free"])
    await S.add_catalog_model(s, orr, orr.models["google/gemma-4-31b-it:free"], make_default=False)
    assert list(s.get("models.list")) == ["or-gemma-4-31b-it", "or-gemma-4-31b-it-2"]


async def test_failed_test_saves_nothing(tmp_path, cat, tested):
    s = Settings(tmp_path)
    tested.result = lambda spec: (False, "401 Unauthorized")
    ds = cat.get("deepseek")
    ok, msg = await S.add_catalog_model(s, ds, ds.models["deepseek-flash"], key="sk-salah")
    assert not ok and "401" in msg
    assert not s.key("DEEPSEEK_API_KEY") and "DEEPSEEK_API_KEY" not in os.environ and not s.get("models.list")
    s.set_key("DEEPSEEK_API_KEY", "sk-lama")                                           # key lama aman
    await S.add_catalog_model(s, ds, ds.models["deepseek-flash"], key="sk-salah")
    assert s.key("DEEPSEEK_API_KEY") == "sk-lama"


async def test_missing_key_fails_without_calling_the_model(tmp_path, cat, tested):
    ds = cat.get("deepseek")
    ok, msg = await S.add_catalog_model(Settings(tmp_path), ds, ds.models["deepseek-flash"])
    assert not ok and tested.specs == [] and S.explain_model_error(msg).field == "key"


async def test_reuses_stored_key_and_shared_gemini_groq_names(tmp_path, cat, tested):
    s = Settings(tmp_path)
    s.set_key("GEMINI_API_KEY", "g-key")                                               # dari fitur "gambar"
    g = cat.get("google")
    ok, _ = await S.add_catalog_model(s, g, g.models["gemini-3.6-flash"])
    assert ok and tested.specs[0]["api_key"] == "g-key"
    assert s.get("models.list")["gemini-3.6-flash"]["api_key"] == "${GEMINI_API_KEY}"


async def test_local_provider_needs_no_key(tmp_path, cat, tested):
    s = Settings(tmp_path)
    lm = cat.get("lmstudio")
    ok, _ = await S.add_catalog_model(s, lm, lm.models["qwen/qwen3"])
    spec = s.get("models.list")["lmstudio-qwen3"]
    assert ok and spec["api_key"] == "none" and spec["api_base"] == "http://127.0.0.1:1234/v1"
    assert from_settings(s).models["lmstudio-qwen3"].api_key == "none"


async def test_custom_endpoint_with_and_without_key(tmp_path, tested):
    s = Settings(tmp_path)
    ok, _ = await S.add_custom_model(s, "qwen2.5:7b", "http://localhost:11434/v1")
    assert ok and s.get("models.list")["custom-qwen2.5-7b"]["api_key"] == "none"
    ok, _ = await S.add_custom_model(s, "big", "https://api.contoh.com/v1", key="sk-x", make_default=False)
    assert ok and s.key("CUSTOM_CUSTOM_BIG") == "sk-x"
    assert s.get("models.list")["custom-big"]["api_key"] == "${CUSTOM_CUSTOM_BIG}"
    assert "sk-x" not in str(s.data["config"]) and tested.specs[1]["api_key"] == "sk-x"


# --- wizard terminal -----------------------------------------------------------------------------------

def script(monkeypatch, answers):
    asked = []
    it = iter(answers)

    def fake_ask(prompt, default=""):
        asked.append(prompt)
        return next(it)

    monkeypatch.setattr(wizard, "ask", fake_ask)
    monkeypatch.setattr(wizard, "say", lambda *a, **k: None)
    return asked


def test_pick_model_filters_sort_search(monkeypatch, cat):
    prov = cat.get("openrouter")
    script(monkeypatch, ["g", "-", "cheap", "1"])           # $0 saja → reset → cari 'cheap' → pilih 1
    assert wizard._pick_model(cat, prov).id == "vendor/cheap"
    script(monkeypatch, ["h", "t", "k", "m", "2"])          # ganti urutan, halaman berikut, pilih nomor 2
    assert wizard._pick_model(cat, prov).id in prov.models
    script(monkeypatch, [""])                               # Enter = kembali
    assert wizard._pick_model(cat, prov) is None


def test_pick_provider_number_search_custom_skip(monkeypatch, cat):
    script(monkeypatch, ["2"])
    assert wizard._pick_provider(cat).id == "deepseek"
    script(monkeypatch, ["e"])
    assert wizard._pick_provider(cat) == "custom"
    script(monkeypatch, ["0"])
    assert wizard._pick_provider(cat) is None
    script(monkeypatch, ["merge", "lm studio"])             # SDK khusus: tak ada hasil; lalu ketemu satu
    assert wizard._pick_provider(cat).id == "lmstudio"


def test_wizard_wrong_model_reopens_only_the_model_list(tmp_path, monkeypatch, cat, tested):
    s = Settings(tmp_path)
    tested.result = lambda spec: (False, BAD_MODEL) if spec["model"].endswith("deepseek-flash") else (True, "OK")
    script(monkeypatch, ["1", "2"])                          # pilih model 1 (gagal) → daftar terbuka lagi → pilih 2
    monkeypatch.setattr(wizard, "ask_secret", lambda prompt: "sk-baru")
    prov = cat.get("deepseek")
    assert wizard._setup_catalog(s, cat, prov) is True
    assert [sp["model"] for sp in tested.specs] == ["deepseek/deepseek-flash", "deepseek/deepseek-v4-pro"]
    assert [m for m in s.get("models.list")] == ["deepseek-v4-pro"] and s.key("DEEPSEEK_API_KEY") == "sk-baru"


def test_wizard_bad_key_asks_only_for_the_key(tmp_path, monkeypatch, cat, tested):
    s = Settings(tmp_path)
    tested.result = lambda spec: (True, "OK") if spec["api_key"] == "sk-benar" else (False, "401 Unauthorized")
    keys = iter(["sk-salah", "sk-benar"])
    monkeypatch.setattr(wizard, "ask_secret", lambda prompt: next(keys))
    asked = script(monkeypatch, ["1"])                        # hanya SATU kali memilih model
    assert wizard._setup_catalog(s, cat, cat.get("deepseek")) is True
    assert len([p for p in asked if p.startswith("Nomor")]) == 1
    assert s.key("DEEPSEEK_API_KEY") == "sk-benar"


def test_wizard_provider_block_returns_to_provider_list(tmp_path, monkeypatch, cat, tested):
    s = Settings(tmp_path)
    tested.result = lambda spec: (False, "OpenCode's free tier can only be used from within OpenCode.")
    monkeypatch.setattr(wizard, "ask_secret", lambda prompt: "k")
    script(monkeypatch, ["1"])
    assert wizard._setup_catalog(s, cat, cat.get("deepseek")) is False
    assert not s.get("models.list")


def test_wizard_custom_retries_only_the_wrong_part(tmp_path, monkeypatch, tested):
    s = Settings(tmp_path)
    answers = {"URL API (OpenAI-compatible), mis. http://localhost:11434/v1": ["https://zen.example/v1"],
               "Nama model": ["opencode"], "Nama model yang benar": ["deepseek-v4"]}
    asked = []

    def fake_ask(prompt, default=""):
        asked.append(prompt)
        return answers[prompt].pop(0)

    monkeypatch.setattr(wizard, "ask", fake_ask)
    monkeypatch.setattr(wizard, "say", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "ask_secret", lambda prompt: "")
    tested.result = lambda spec: (False, BAD_MODEL) if spec["model"] == "openai/opencode" else (True, "OK")
    assert wizard._setup_custom(s) is True
    assert [sp["model"] for sp in tested.specs] == ["openai/opencode", "openai/deepseek-v4"]
    assert asked.count("Nama model yang benar") == 1 and sum(p.startswith("URL API (") for p in asked) == 1
    assert s.get("models.list")["custom-deepseek-v4"]["api_base"] == "https://zen.example/v1"


def test_doctor_tests_every_model(tmp_path, monkeypatch, capsys):
    s = Settings(tmp_path)
    s.add_model("a", {"model": "deepseek/deepseek-flash", "api_key": "k"})
    s.add_model("b", {"model": "gemini/gemini-3.6-flash"}, make_default=False)
    seen = []

    async def fake(spec):
        seen.append(spec["model"])
        return (spec["model"].startswith("deepseek"), "OK" if spec["model"].startswith("deepseek") else "401")

    monkeypatch.setattr(S.checks, "model", fake)
    assert wizard.doctor(s) == 1                              # satu gagal → kode keluar 1
    out = capsys.readouterr().out
    assert sorted(seen) == ["deepseek/deepseek-flash", "gemini/gemini-3.6-flash"]
    assert "Model a (utama)" in out and "Model b" in out


# --- /setup Telegram -----------------------------------------------------------------------------------

def tg_update(text="", data=None, chat=1):
    sent, deleted = [], []

    async def send_message(text, reply_markup=None, **kw):
        sent.append((text, reply_markup))

    async def delete():
        deleted.append(True)

    async def answer(*a, **k):
        pass

    async def edit(text, reply_markup=None, **kw):
        sent.append((text, reply_markup))

    q = SimpleNamespace(data=data, answer=answer, edit_message_text=edit) if data else None
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat, send_message=send_message),
        effective_message=SimpleNamespace(text=text, delete=delete), callback_query=q), sent, deleted


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture
def tg(tmp_path, cat):
    from inspire import build
    from inspire.gateway.tg_setup import TelegramSetup

    app = build(Settings(tmp_path))
    setup = TelegramSetup(app)
    setup._catalog = cat
    return app, setup


async def test_tg_model_picker_full_flow(tg, tested):
    app, setup = tg
    upd, sent, _ = tg_update(data="su:sec:model")
    await setup.on_button(upd)
    assert "su:prov:deepseek" in buttons(sent[-1][1]) and "su:provq" in buttons(sent[-1][1])

    upd, sent, _ = tg_update(data="su:prov:deepseek")        # key belum ada → minta key
    await setup.on_button(upd)
    assert setup.is_waiting("1") and "Tempel key DeepSeek" in sent[-1][0]

    upd, sent, deleted = tg_update(text="sk-rahasia")        # key dihapus dari chat, daftar model tampil
    await setup.on_text(upd)
    assert deleted and "sk-rahasia" not in sent[-1][0]
    assert "deepseek-flash" in sent[-1][0] and "su:mdl:0" in buttons(sent[-1][1])
    assert not app.settings.key("DEEPSEEK_API_KEY")          # belum disimpan sebelum tes lolos

    upd, sent, _ = tg_update(text="pro")                      # teks = kata pencarian
    await setup.on_text(upd)
    assert "deepseek-v4-pro" in sent[-1][0] and "deepseek-flash" not in sent[-1][0]
    await setup.on_button(tg_update(data="su:mf:x")[0])       # saring $0 → tak ada yang cocok
    upd, sent, _ = tg_update(data="su:ms:x")
    await setup.on_button(upd)
    assert "urut termurah" in sent[-1][0] and "$0" in sent[-1][0]

    await setup.on_button(tg_update(data="su:mf:x")[0])       # matikan lagi
    upd, sent, _ = tg_update(data="su:mdl:0")
    await setup.on_button(upd)
    assert "✅ deepseek-v4-pro siap" in sent[-1][0]
    assert app.settings.key("DEEPSEEK_API_KEY") == "sk-rahasia" and not setup.is_waiting("1")
    assert app.agent.llm.active == "deepseek-v4-pro"          # langsung aktif tanpa restart


async def test_tg_wrong_key_then_rekey_retries_same_model(tg, tested):
    app, setup = tg
    tested.result = lambda spec: (True, "OK") if spec["api_key"] == "sk-benar" else (False, "401 Unauthorized")
    await setup.on_button(tg_update(data="su:prov:deepseek")[0])
    await setup.on_text(tg_update(text="sk-salah")[0])
    upd, sent, _ = tg_update(data="su:mdl:0")
    await setup.on_button(upd)
    assert "gagal dites" in sent[-1][0] and "su:rekey:x" in buttons(sent[-1][1])
    assert not app.settings.key("DEEPSEEK_API_KEY")
    await setup.on_button(tg_update(data="su:rekey:x")[0])
    upd, sent, deleted = tg_update(text="sk-benar")
    await setup.on_text(upd)
    assert deleted and "✅" in sent[-1][0] and app.settings.key("DEEPSEEK_API_KEY") == "sk-benar"


async def test_tg_wrong_model_offers_other_model_and_error_explained(tg, tested):
    app, setup = tg
    tested.result = lambda spec: (False, BAD_MODEL)
    app.settings.set_key("DEEPSEEK_API_KEY", "k")
    await setup.on_button(tg_update(data="su:prov:deepseek")[0])
    upd, sent, _ = tg_update(data="su:mdl:0")
    await setup.on_button(upd)
    assert "nama model itu tidak ada" in sent[-1][0] and "su:ml:x" in buttons(sent[-1][1])
    upd, sent, _ = tg_update(data="su:ml:x")
    await setup.on_button(upd)
    assert "deepseek-flash" in sent[-1][0]                    # daftar terbuka lagi


async def test_tg_provider_search_custom_and_ttl(tg, tested, monkeypatch):
    app, setup = tg
    await setup.on_button(tg_update(data="su:provq")[0])
    upd, sent, _ = tg_update(text="lm studio")
    await setup.on_text(upd)
    assert "su:prov:lmstudio" in buttons(sent[-1][1])
    upd, sent, _ = tg_update(data="su:prov:lmstudio")         # lokal: tanpa key → langsung daftar model
    await setup.on_button(upd)
    assert "qwen/qwen3" in sent[-1][0]

    await setup.on_button(tg_update(data="su:custom")[0])
    upd, sent, deleted = tg_update(text="http://localhost:11434/v1 qwen2.5:7b")
    await setup.on_text(upd)
    assert deleted and "✅ custom-qwen2.5-7b siap" in sent[-1][0]

    await setup.on_button(tg_update(data="su:provq")[0])      # status menunggu kedaluwarsa → obrolan biasa
    assert setup.is_waiting("1")
    import time
    real = time.time()
    monkeypatch.setattr(time, "time", lambda: real + 700)
    assert not setup.is_waiting("1")


async def test_add_opencode_paid_model_uses_its_own_route(tmp_path, cat, tested):
    s = Settings(tmp_path)
    oc = cat.get("opencode")
    ok, _ = await S.add_catalog_model(s, oc, oc.models["claude-haiku-4-5"], key="sk-zen")
    spec = s.get("models.list")["opencode-claude-haiku-4-5"]
    assert ok and spec["model"] == "anthropic/claude-haiku-4-5" and spec["api_base"] == "https://opencode.ai/inference/anthropic"
    assert spec["api_key"] == "${OPENCODE_API_KEY}" and s.key("OPENCODE_API_KEY") == "sk-zen"
    assert tested.specs[0]["api_key"] == "sk-zen"
