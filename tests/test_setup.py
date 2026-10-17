import json
import os
import stat
from types import SimpleNamespace

import httpx
import pytest

from inspire import build
from inspire import setup as S
from inspire.config import from_settings
from inspire.llm import LLM, AllModelsFailed
from inspire.settings import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "GROQ_API_KEY", "GEMINI_API_KEY",
              "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "SANDBOX_URL", "SANDBOX_TOKEN",
              "INSPIRE_INBOX", "ALLOWED_USER_IDS", "INSPIRE_CONFIG"):
        monkeypatch.delenv(k, raising=False)


def test_settings_file_secret_and_env(tmp_path):
    s = Settings(tmp_path)
    s.set_key("GROQ_API_KEY", "gsk_1")
    assert os.environ["GROQ_API_KEY"] == "gsk_1"
    assert stat.S_IMODE(os.stat(s.path).st_mode) == 0o600
    assert Settings(tmp_path).key("GROQ_API_KEY") == "gsk_1"
    s.set_key("GROQ_API_KEY", "")
    assert "GROQ_API_KEY" not in os.environ and not s.key("GROQ_API_KEY")


def test_raw_layers_and_owners(tmp_path):
    s = Settings(tmp_path)
    s.set("timezone", "Asia/Makassar")
    s.add_owner("telegram", 42)
    (tmp_path / "config.yaml").write_text("agent: {max_iterations: 3}\n")
    s.set_key("TELEGRAM_BOT_TOKEN", "123:abc")
    cfg = from_settings(s)
    assert cfg.timezone == "Asia/Makassar"
    assert cfg.max_iterations == 3                   # overlay
    assert cfg.allowed_user_ids == [42]
    assert cfg.telegram_token == "123:abc"           # ${TELEGRAM_BOT_TOKEN} dari keys
    assert cfg.models == {} and cfg.default_model == ""
    assert cfg.sandbox_url == ""                     # tanpa Docker


async def test_llm_without_model_gives_friendly_error():
    with pytest.raises(AllModelsFailed, match="/setup"):
        await LLM({}, "", []).complete([{"role": "user", "content": "hi"}])


def test_pairing_code(tmp_path):
    s = Settings(tmp_path)
    code = s.pairing_code()
    assert len(code) == 6 and code.isdigit() and s.pairing_code() == code
    s.add_owner("telegram", 1)
    assert "pairing" not in s.data


def test_migrate_legacy(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".env").write_text("TELEGRAM_BOT_TOKEN=9:x\nALLOWED_USER_IDS=11,22\nDEEPSEEK_API_KEY=sk\nSANDBOX_TOKEN=t\n")
    (proj / "config.yaml").write_text(
        "data_dir: data\ntelegram: {token: '${TELEGRAM_BOT_TOKEN}', allowed_user_ids: '${ALLOWED_USER_IDS}'}\n"
        "models: {default: d, list: {d: {model: deepseek/deepseek-chat}}}\n"
    )
    s = Settings(tmp_path / "data")
    assert s.migrate_legacy(proj)
    assert s.owners("telegram") == [11, 22]
    assert s.key("DEEPSEEK_API_KEY") == "sk" and "SANDBOX_TOKEN" not in s.data["keys"]
    cfg = from_settings(s)
    assert cfg.telegram_token == "9:x" and cfg.allowed_user_ids == [11, 22]
    assert cfg.default_model == "d"
    assert not s.migrate_legacy(proj)  # sekali saja


async def test_save_key_tests_before_saving(tmp_path, monkeypatch):
    s = Settings(tmp_path)

    async def fake(key):
        return (key == "good", "cek")

    monkeypatch.setattr(S.KEY_SECTIONS["suara"], "check", fake)
    ok, _ = await S.save_key(s, "suara", "bad")
    assert not ok and not s.key("GROQ_API_KEY")
    s.skip("suara")
    ok, _ = await S.save_key(s, "suara", "good")
    assert ok and s.key("GROQ_API_KEY") == "good" and not s.is_skipped("suara")


async def test_add_preset_models(tmp_path, monkeypatch):
    s = Settings(tmp_path)
    tested = []

    async def fake_model(spec):
        tested.append(spec)
        return True, "OK"

    monkeypatch.setattr(S.checks, "model", fake_model)
    pre = {p.id: p for p in S.PRESETS}
    await S.add_preset_model(s, pre["openrouter"], key="or-k", model="meta/llama:free")
    await S.add_preset_model(s, pre["gemini"], key="g-k", make_default=False)
    await S.add_preset_model(s, pre["custom"], key="zen-k", model="big-pickle",
                             api_base="https://opencode.ai/zen/v1", make_default=False)
    cfg = from_settings(s)
    assert cfg.default_model == "or-llama"
    assert cfg.models["or-llama"].model == "openrouter/meta/llama:free"
    assert cfg.models["gemini"].vision is True
    assert cfg.models["custom-big-pickle"].api_key == "zen-k"   # ${CUSTOM_…} terisi
    assert cfg.fallback == ["or-llama", "gemini", "custom-big-pickle"]
    assert tested[2]["api_key"] == "zen-k"                      # dites dengan key asli
    assert "zen-k" not in json.dumps(s.data["config"])          # key tak bocor ke config


async def test_model_test_failure_not_saved(tmp_path, monkeypatch):
    s = Settings(tmp_path)

    async def fail(spec):
        return False, "401"

    monkeypatch.setattr(S.checks, "model", fail)
    ok, msg = await S.add_preset_model(s, S.PRESETS[2], key="x")
    assert not ok and msg == "401" and not s.get("models.list")


async def test_status_states(tmp_path):
    s = Settings(tmp_path)
    f = {x.id: x for x in await S.status(s)}
    assert f["telegram"].state == "off" and f["model"].state == "err"
    assert f["shell"].state == "off" and f["zona"].state == "on"
    s.skip("suara")
    s.set_key("TELEGRAM_BOT_TOKEN", "1:a")
    f = {x.id: x for x in await S.status(s)}
    assert f["suara"].icon == "⏸"
    assert f["telegram"].state == "err" and "/pair" in f["telegram"].fix
    s.add_owner("telegram", 5)
    s.set_key("GROQ_API_KEY", "g")
    f = {x.id: x for x in await S.status(s)}
    assert f["telegram"].state == "on" and f["suara"].state == "on"
    assert "✅ Telegram" in S.render(list(f.values()))


def test_app_reload_applies_live(tmp_path):
    s = Settings(tmp_path)
    app = build(s)
    assert app.agent.llm.models == {}
    s.add_model("ds", {"model": "deepseek/deepseek-chat"})
    s.set_key("GROQ_API_KEY", "g2")
    app.reload()
    assert app.agent.llm.active == "ds" and "ds" in app.agent.llm.models
    assert app.agent.media.cfg.stt_api_key == "g2"
    assert app.cfg.default_model == "ds"


def test_app_shell_follows_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_URL", "http://sandbox:8765")
    assert "shell" in build(Settings(tmp_path / "a")).agent.tools.tools
    s = Settings(tmp_path / "b")
    s.set("shell.enabled", False)
    assert "shell" not in build(s).agent.tools.tools


def test_request_restart_sets_notify(tmp_path):
    import asyncio

    app = build(Settings(tmp_path))
    app.stop = asyncio.Event()
    app.request_restart("777")
    assert app.restart_requested and app.stop.is_set()
    assert Settings(tmp_path).data["restart_notify"] == "777"


async def test_wizard_wait_pair(monkeypatch):
    from inspire.setup import wizard

    sent = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/getUpdates"):
            if req.url.params.get("offset") == "0":
                return httpx.Response(200, json={"result": [
                    {"update_id": 1, "message": {"text": "/start 999999", "from": {"id": 8}, "chat": {"id": 8}}},
                    {"update_id": 2, "message": {"text": "/start 123456", "from": {"id": 9, "username": "ikhsan"},
                                                 "chat": {"id": 9}}},
                ]})
            return httpx.Response(200, json={"result": []})
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    user = await wizard._wait_pair("T", "123456")
    assert user["id"] == 9 and sent[0]["chat_id"] == 9

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(409, json={})), **kw))
    assert await wizard._wait_pair("T", "123456") is None


# --- /setup Telegram ----------------------------------------------------------

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
    upd = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat, send_message=send_message),
        effective_message=SimpleNamespace(text=text, delete=delete),
        callback_query=q,
    )
    return upd, sent, deleted


async def test_tg_setup_key_flow_deletes_secret(tmp_path, monkeypatch):
    from inspire.gateway.tg_setup import TelegramSetup

    app = build(Settings(tmp_path))
    setup = TelegramSetup(app)

    async def fake(key):
        return (key == "gsk_good", "valid")

    monkeypatch.setattr(S.KEY_SECTIONS["suara"], "check", fake)

    upd, sent, _ = tg_update(data="su:key:suara")
    await setup.on_button(upd)
    assert setup.is_waiting("1") and "Tempel key" in sent[-1][0]

    upd, sent, deleted = tg_update(text="gsk_bad")
    await setup.on_text(upd)
    assert deleted and "tidak disimpan" in sent[-1][0]
    assert not app.settings.key("GROQ_API_KEY")

    await setup.on_button(tg_update(data="su:key:suara")[0])
    upd, sent, deleted = tg_update(text="gsk_good")
    await setup.on_text(upd)
    assert deleted and "aktif" in sent[-1][0]
    assert app.agent.media.cfg.stt_api_key == "gsk_good"   # langsung aktif


async def test_tg_setup_menu_and_skip(tmp_path):
    from inspire.gateway.tg_setup import TelegramSetup

    app = build(Settings(tmp_path))
    setup = TelegramSetup(app)
    upd, sent, _ = tg_update(data="su:menu")
    await setup.on_button(upd)
    labels = [b.text for row in sent[-1][1].inline_keyboard for b in row]
    assert any("Model AI" in t for t in labels) and any("Suara" in t for t in labels)
    await setup.on_button(tg_update(data="su:skip:gambar")[0])
    assert app.settings.is_skipped("gambar")


async def test_tg_setup_zona_and_restart(tmp_path):
    import asyncio

    from inspire.gateway.tg_setup import TelegramSetup

    app = build(Settings(tmp_path))
    app.stop = asyncio.Event()
    setup = TelegramSetup(app)
    await setup.on_button(tg_update(data="su:sec:zona")[0])
    upd, sent, _ = tg_update(text="Mars/Olympus")
    await setup.on_text(upd)
    assert "tak dikenal" in sent[-1][0] and setup.is_waiting("1")
    upd, sent, _ = tg_update(text="Asia/Jayapura")
    await setup.on_text(upd)
    assert app.settings.get("timezone") == "Asia/Jayapura" and "Restart" in str(sent[-1][1].inline_keyboard)
    await setup.on_button(tg_update(data="su:restart:yes")[0])
    assert app.restart_requested


# --- gagal tes model: penjelasan + ulang hanya bagian yang salah -----------------

@pytest.mark.parametrize("raw,field", [
    ("litellm.APIError: OpenAIException - OpenCode's free tier can only be used from within OpenCode.", "provider"),
    ("BadRequestError: OpenAIException - Upstream request failed: Model is unavailable.", "model"),
    ("AuthenticationError: Incorrect API key provided", "key"),
    ("litellm.APIConnectionError: Connection error. getaddrinfo failed", "url"),
    ("RateLimitError: 429 Too Many Requests", "limit"),
    ("Error: sesuatu yang aneh", "other"),
])
def test_explain_model_error(raw, field):
    p = S.explain_model_error(raw, "custom")
    assert p.field == field and p.hint
    if field == "model":
        assert "/models" in p.hint and "URL dan key kemungkinan benar" in p.hint


async def test_failed_key_not_persisted_and_env_restored(tmp_path, monkeypatch):
    s = Settings(tmp_path)

    async def fail(spec):
        assert os.environ["DEEPSEEK_API_KEY"] == "sk-salah"   # litellm harus melihat key yang sedang dites
        return False, "401"

    monkeypatch.setattr(S.checks, "model", fail)
    ok, _ = await S.add_preset_model(s, S.PRESETS[2], key="sk-salah")
    assert not ok and not s.key("DEEPSEEK_API_KEY") and "DEEPSEEK_API_KEY" not in os.environ

    s.set_key("DEEPSEEK_API_KEY", "sk-lama")                 # key lama tak boleh hilang karena tes gagal
    ok, _ = await S.add_preset_model(s, S.PRESETS[2], key="sk-salah")
    assert not ok and os.environ["DEEPSEEK_API_KEY"] == "sk-lama" and s.key("DEEPSEEK_API_KEY") == "sk-lama"


async def test_custom_without_key_uses_placeholder(tmp_path, monkeypatch):
    s = Settings(tmp_path)
    tested = []

    async def fake(spec):
        tested.append(spec)
        return True, "OK"

    monkeypatch.setattr(S.checks, "model", fake)
    ok, _ = await S.add_preset_model(s, S.PRESETS[3], model="qwen2.5:7b", api_base="http://localhost:11434/v1")
    assert ok and tested[0]["api_key"] == "none"
    assert from_settings(s).models["custom-qwen2.5:7b"].api_key == "none"   # tanpa ini litellm error di runtime


def test_wizard_retries_only_the_wrong_part(tmp_path, monkeypatch):
    from inspire.setup import wizard

    s = Settings(tmp_path)
    asked = []
    answers = {
        "URL API (OpenAI-compatible), mis. https://api.contoh.com/v1": ["https://zen.example/v1"],
        "Nama model": ["opencode"],
        "Nama model yang benar": ["deepseek-v4"],
    }

    def fake_ask(prompt, default=""):
        asked.append(prompt)
        return answers[prompt].pop(0)

    seen = []

    async def fake_model(spec):
        seen.append(spec["model"])
        if spec["model"] == "openai/opencode":
            return False, "BadRequestError: OpenAIException - Upstream request failed: Model is unavailable."
        return True, "OK"

    monkeypatch.setattr(wizard, "ask", fake_ask)
    monkeypatch.setattr(wizard, "ask_secret", lambda prompt: "")
    monkeypatch.setattr(S.checks, "model", fake_model)
    assert wizard._setup_one(s, S.PRESETS[3]) is True
    assert seen == ["openai/opencode", "openai/deepseek-v4"]
    # URL dan key TIDAK ditanya lagi, hanya nama model
    assert asked.count("Nama model yang benar") == 1
    assert asked.count("URL API (OpenAI-compatible), mis. https://api.contoh.com/v1") == 1
    assert s.get("models.list")["custom-deepseek-v4"]["api_base"] == "https://zen.example/v1"


def test_wizard_provider_error_goes_back_to_list(tmp_path, monkeypatch):
    from inspire.setup import wizard

    s = Settings(tmp_path)
    prompts = []
    monkeypatch.setattr(wizard, "ask", lambda p, d="": prompts.append(p) or {"Nama model": "x"}.get(p, "https://z/v1"))
    monkeypatch.setattr(wizard, "ask_secret", lambda p: "")

    async def fail(spec):
        return False, "OpenAIException - OpenCode's free tier can only be used from within OpenCode."

    monkeypatch.setattr(S.checks, "model", fail)
    assert wizard._setup_one(s, S.PRESETS[3]) is False          # tak ada gunanya mengulang: ganti penyedia
    assert prompts.count("Nama model") == 1 and not s.get("models.list")


async def test_openrouter_list_shows_all_free_tool_models(monkeypatch):
    models = [{"id": f"vendor/m{i}:free", "context_length": 1000 * i, "supported_parameters": ["tools"]} for i in range(1, 16)]
    models += [
        {"id": "vendor/notools:free", "context_length": 9_000_000, "supported_parameters": ["temperature"]},
        {"id": "vendor/paid", "context_length": 9_000_000, "supported_parameters": ["tools"]},
    ]

    async def fake_get(url, **kw):
        return SimpleNamespace(json=lambda: {"data": models})

    monkeypatch.setattr(S.checks, "_get", fake_get)
    listing = await S.checks.openrouter_free_list()
    assert len(listing) == 15                                  # semua, bukan 6 teratas
    assert listing[0] == ("vendor/m15:free", 15000) and listing[-1][0] == "vendor/m1:free"
    assert "vendor/notools:free" not in dict(listing) and "vendor/paid" not in dict(listing)
