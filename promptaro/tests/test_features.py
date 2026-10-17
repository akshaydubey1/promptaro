# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Subscription model picks, project-bound chats, export, and custom endpoints."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import Scripted, act, drain_catalog_refresh, wait_task
from desk import workers
from router import catalog

H = {"x-promptaro": "1"}
FAKE = str(Path(__file__).with_name("fake_claude.py"))
HARD = ("Architect and implement a production-grade, thread-safe retry layer with exponential backoff, "
        "jitter, circuit breaking and full test coverage for edge cases and concurrency")


@pytest.fixture
def client(router_root):
    from desk import server
    with TestClient(server.app) as c:
        drain_catalog_refresh(c)
        fake = Scripted()
        server.desk.router.providers = {k: fake for k in ("anthropic", "openai", "openai_compatible", "offline")}
        c.fake, c.desk = fake, server.desk
        yield c


@pytest.fixture
def claude(monkeypatch, tmp_path):
    monkeypatch.setenv("PROMPTARO_CLAUDE_BIN", FAKE)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude_call.json"))
    monkeypatch.setattr(workers, "_status_cache", {})
    return tmp_path / "claude_call.json"


def wait_status(c, sid, done=("idle", "review"), timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/sessions/{sid}").json()
        if s["session"]["status"] in done and not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("timed out")


def wait_answer(c, sid, n=1, timeout=10):
    """Wait for the nth assistant event — a settled 'idle' can predate a just-queued turn."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/sessions/{sid}").json()
        if len([e for e in s["events"] if e["kind"] == "assistant"]) >= n and not s["running"]:
            return s
        time.sleep(0.05)
    raise AssertionError("timed out")


def _run(desk, repo, task, **kw):
    async def go():
        s = await desk.start_agent(str(repo), task, prefer=kw.get("prefer", "balanced"), model=kw.get("model", ""),
                                   approval="ask_risky", max_cost=2)
        await wait_task(desk, s["id"])
        return s["id"]
    return asyncio.run(go())


# ------------------------------------------------- subscription model picks
def test_engine_of_maps_variants():
    assert workers.engine_of("claude-code") == ("claude_code", "")
    assert workers.engine_of("claude-code:opus") == ("claude_code", "opus")
    assert workers.engine_of("codex:gpt-5-codex") == ("codex", "gpt-5-codex")
    assert workers.engine_of("gpt-4o") == ("", "")
    assert workers.engine_of("") == ("", "")


def test_models_endpoint_lists_plan_models(client, claude):
    models = client.get("/api/models").json()
    ids = [m["id"] for m in models if m.get("subscription")]
    assert "claude-code" in ids and "claude-code:opus" in ids and "claude-code:sonnet" in ids
    expected = {f"claude-code:{v}" for v in workers.MODEL_CHOICES["claude_code"]} | {"claude-code"}
    assert expected <= set(ids)                                            # every version the plan knows is offered
    assert not any(i.startswith("codex") for i in ids)                     # codex isn't installed here


def test_claude_variant_reaches_cli(claude, desk_factory, repo):
    desk = desk_factory(Scripted())
    sid = _run(desk, repo, HARD, model="claude-code:opus")
    argv = json.loads(claude.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    ev = desk.store.events(sid)
    assert any(e["kind"] == "model" and e["data"]["name"] == "Claude Opus (latest)"
               and e["data"]["subscription"] for e in ev)


def test_fallback_ignores_other_plans_pick(desk_factory):
    """claude-code:opus must not become `codex -m opus` if Codex takes over."""
    run = workers.CodexRun(desk_factory(Scripted()), "x")
    eng, var = workers.engine_of("claude-code:opus")
    run.variant = var if eng == run.engine else ""                        # same line run() uses
    assert run.variant == "" and run.engine == "codex"


def test_chat_pick_uses_the_plan(client, claude, repo):
    """A subscription pick in a chat answers through the official CLI, read-only."""
    sid = client.post("/api/sessions", json={"project": str(repo), "model": "claude-code:opus"},
                      headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    s = wait_answer(client, sid, 1)
    a = [e for e in s["events"] if e["kind"] == "assistant"][0]["data"]
    assert a["subscription"] and a["model"] == "claude-code:opus" and a["cost"] == 0.0
    argv = json.loads(claude.read_text())["argv"]
    assert argv[argv.index("--model") + 1] == "opus"
    allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
    assert allowed == ["Read(./**)", "Grep(./**)", "Glob(./**)", "LS(./**)"]
    denied = argv[argv.index("--disallowedTools") + 1:]
    assert "Read(**/.env*)" in denied and "Read(~/.ssh/**)" in denied


def test_chat_plan_failure_falls_back_with_note(client, claude, monkeypatch):
    """When the plan tool can't answer, a normal model does — and the chat says so."""
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    client.fake.shared = ["router answer"]
    sid = client.post("/api/sessions", json={"model": "claude-code:opus"}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    s = wait_answer(client, sid, 1)
    a = [e for e in s["events"] if e["kind"] == "assistant"][0]["data"]
    assert a["model"] in ("small", "medium", "large") and not a.get("subscription")
    notes = [e["data"]["text"] for e in s["events"] if e["kind"] == "note"]
    assert any("regular model" in n for n in notes)


# --------------------------------------------------------------- project chat
def test_project_chat_reads_but_cannot_write(client, repo):
    client.fake.shared = [act("read_file", path="calc.py"),
                          act("finish", summary="It adds two numbers."),
                          act("write_file", path="evil.py", content="x=1"),
                          act("run", command="rm -rf ."),
                          act("finish", summary="Only reads — nothing changed.")]
    sid = client.post("/api/sessions", json={"project": str(repo)}, headers=H).json()["id"]
    assert client.post(f"/api/sessions/{sid}/messages", json={"text": "what does calc do?"}, headers=H).status_code == 200
    s = wait_answer(client, sid, 1)
    a = [e for e in s["events"] if e["kind"] == "assistant"][0]["data"]
    assert "adds two numbers" in a["text"]
    assert any(e["kind"] == "tool_result" and e["data"]["action"] == "read_file" and e["data"]["ok"]
               for e in s["events"])
    # second turn tries to write and to run a command — both refused, nothing changed
    client.post(f"/api/sessions/{sid}/messages", json={"text": "now change it"}, headers=H)
    s = wait_answer(client, sid, 2)
    refused = [e for e in s["events"] if e["kind"] == "tool_result" and not e["data"]["ok"]]
    assert len(refused) == 2 and all("read-only" in e["data"]["output"].lower() for e in refused)
    assert not (repo / "evil.py").exists() and "a - b" in (repo / "calc.py").read_text()
    assert [e["kind"] for e in s["events"]][-1] == "assistant"


def test_project_chat_needs_a_real_folder(client):
    assert client.post("/api/sessions", json={"project": "/no/such/folder"}, headers=H).status_code == 400


def test_chat_without_project_is_unchanged(client):
    client.fake.shared = ["plain"]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    wait_status(client, sid)
    assert not client.desk.store.get(sid).get("project")


# ------------------------------------------------------------------- export
def test_export_markdown_and_json(client):
    client.fake.shared = ["Hello there!"]
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi there"}, headers=H)
    wait_answer(client, sid, 1)
    md = client.get(f"/api/sessions/{sid}/export")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert md.headers["content-disposition"].endswith('.md"')
    assert "# hi there" in md.text and "## You" in md.text and "Hello there!" in md.text
    j = client.get(f"/api/sessions/{sid}/export?format=json")
    assert j.headers["content-disposition"].endswith('.json"')
    d = j.json()
    assert d["session"]["id"] == sid and any(e["kind"] == "assistant" for e in d["events"])
    assert client.get("/api/sessions/nope/export").status_code == 404


def test_export_includes_project_label(client, repo):
    client.fake.shared = [act("finish", summary="answer")]
    sid = client.post("/api/sessions", json={"project": str(repo)}, headers=H).json()["id"]
    client.post(f"/api/sessions/{sid}/messages", json={"text": "hi"}, headers=H)
    wait_answer(client, sid, 1)
    md = client.get(f"/api/sessions/{sid}/export").text
    assert f"Project: {repo}" in md


# ----------------------------------------------------------- custom endpoints
def test_custom_endpoint_round_trip(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_endpoint_models",
                        lambda base, key_env="", client=None: ["llama3.1:8b", "qwen3:14b"])
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "My Ollama", "base_url": "http://localhost:11434/v1", "private": True})
    assert r.status_code == 200 and r.json()["models"] == 2
    eid = r.json()["endpoint"]["id"]
    models = client.get("/api/models").json()
    got = [m["id"] for m in models if m["id"].startswith(eid)]
    assert sorted(got) == [f"{eid}/llama3.1:8b", f"{eid}/qwen3:14b"]
    ep = client.get("/connect/status").json()["endpoints"][0]
    assert ep["private"] is True and ep["label"] == "My Ollama" and "key_env" not in ep
    assert client.delete(f"/connect/endpoints/{eid}", headers=H).status_code == 200
    assert not [m for m in client.get("/api/models").json() if m["id"].startswith(eid)]
    assert not catalog.custom_endpoints()


def test_custom_endpoint_key_stored_not_returned(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_endpoint_models", lambda base, key_env="", client=None: ["m1"])
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "Proxy", "base_url": "https://proxy.example.com/v1", "key": "test-key-12345"})
    assert r.status_code == 200 and r.json()["endpoint"]["has_key"]
    assert "test-key-12345" not in client.get("/connect/status").text       # the key never leaves storage


def test_custom_endpoint_unreachable_without_models(client, monkeypatch):
    def boom(base, key_env="", client=None):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(catalog, "discover_endpoint_models", boom)
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "Down", "base_url": "http://localhost:9/v1"})
    assert r.status_code == 400 and "Couldn't reach" in r.json()["detail"]
    # typing the models by hand still works even when /models won't answer
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "Down", "base_url": "http://localhost:9/v1", "models": "m1, m2"})
    assert r.status_code == 200 and r.json()["models"] == 2


def test_custom_endpoint_bad_url(client):
    assert client.post("/connect/endpoints", headers=H,
                       json={"label": "x", "base_url": "localhost:1234"}).status_code == 400


# --------------------------------------------------------------- renaming
def test_rename_chat(client):
    sid = client.post("/api/sessions", json={}, headers=H).json()["id"]
    r = client.patch(f"/api/sessions/{sid}", json={"title": "My research"}, headers=H)
    assert r.status_code == 200
    s = [x for x in client.get("/api/sessions").json() if x["id"] == sid][0]
    assert s["title"] == "My research"


def test_rename_project(client, repo):
    assert client.post("/api/projects/rename", json={"path": str(repo), "name": "x"}).status_code == 403
    r = client.post("/api/projects/rename", json={"path": str(repo), "name": "Calc"}, headers=H)
    assert r.status_code == 200
    assert client.get("/api/project-names").json() == {str(repo): "Calc"}
    client.post("/api/projects/rename", json={"path": str(repo), "name": ""}, headers=H)
    assert client.get("/api/project-names").json() == {}          # empty name restores the folder name


# --------------------------------------------------------------- studio tools
def test_tools_capabilities(client):
    r = client.get("/api/tools/capabilities").json()
    assert "convert" in r and "media" in r
    kinds = {m["id"]: m["kinds"] for m in r["media"]}
    assert kinds["pollinations"] == ["image"]                      # keyless fallback always listed
    assert any("video" in k for k in kinds.values())


def test_generate_saves_file_and_downloads(client, monkeypatch):
    from router import media
    monkeypatch.setattr(media, "generate", lambda kind, prompt, w, h: (b"\x89PNG" + b"0" * 200, "image/png", "test-provider"))
    r = client.post("/api/tools/generate", headers=H, json={"kind": "image", "prompt": "a koi pond"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["provider"] == "test-provider" and d["bytes"] > 100
    got = client.get(d["url"])
    assert got.status_code == 200 and got.content[:4] == b"\x89PNG"


def test_generate_reports_every_failure(client, monkeypatch):
    from router import media
    def boom(kind, prompt, w, h):
        raise RuntimeError("all providers said no")
    monkeypatch.setattr(media, "generate", boom)
    r = client.post("/api/tools/generate", headers=H, json={"kind": "video", "prompt": "waves"})
    assert r.status_code == 400 and "providers said no" in r.json()["detail"]


def test_generate_needs_the_app_header(client):
    assert client.post("/api/tools/generate", json={"kind": "image", "prompt": "x"}).status_code == 403


def test_media_provider_order_and_fallback(monkeypatch):
    """Best-first: a failing provider hands off to the next connected one."""
    from router import media
    monkeypatch.setattr(media.secrets, "has", lambda k: k in ("HF_TOKEN", "TOGETHER_API_KEY"))
    monkeypatch.setitem(media._GENERATORS, ("huggingface", "image"),
                        lambda p, w, h: (_ for _ in ()).throw(RuntimeError("quota")))
    monkeypatch.setitem(media._GENERATORS, ("together", "image"), lambda p, w, h: (b"img" * 100, "image/png"))
    data, mime, who = media.generate("image", "a lighthouse", 512, 512)
    assert who == "together" and mime == "image/png"
    monkeypatch.setattr(media.secrets, "has", lambda k: False)
    monkeypatch.setitem(media._GENERATORS, ("pollinations", "image"), lambda p, w, h: (b"p" * 200, "image/jpeg"))
    assert media.generate("image", "x", 512, 512)[2] == "pollinations"


def test_paid_media_lane_needs_opt_in(monkeypatch):
    """Google's image models may bill once free quota ends — off until the user says so."""
    from router import media
    called = []
    monkeypatch.setattr(media.secrets, "has", lambda k: k == "GEMINI_API_KEY")
    monkeypatch.setitem(media._GENERATORS, ("gemini", "image"),
                        lambda p, w, h: called.append(1) or (b"g" * 200, "image/png"))
    monkeypatch.setitem(media._GENERATORS, ("pollinations", "image"), lambda p, w, h: (b"p" * 200, "image/jpeg"))
    assert media.generate("image", "x", 64, 64)[2] == "pollinations" and not called
    monkeypatch.setattr(media.catalog, "get_settings", lambda: {"paid_media": True})
    assert media.generate("image", "x", 64, 64)[2] == "gemini"
    assert media.status()[0]["enabled"] is True


def test_paid_media_setting_round_trip(client):
    assert client.post("/connect/settings", headers=H, json={"paid_media": True}).status_code == 200
    assert client.get("/api/tools/capabilities").json()["paid_media"] is True


def test_media_video_error_names_the_fix(monkeypatch):
    from router import media
    monkeypatch.setattr(media.secrets, "has", lambda k: False)
    with pytest.raises(RuntimeError, match="Hugging Face token"):
        media.generate("video", "ocean waves", 512, 512)


def test_hf_key_autodetects():
    from router.connect import detect_vendor
    assert detect_vendor("hf_abcdefghijklmnopqrstuvwxyz1234") == "huggingface"
    assert detect_vendor("sk-or-v1-abcdef") == "openrouter"


# --------------------------------------------------------------- conversions
def _png(path):
    from PIL import Image
    Image.new("RGB", (40, 30), (200, 30, 30)).save(path)
    return path


def test_make_and_merge_pdf(client, tmp_path):
    pytest.importorskip("pypdf"); pytest.importorskip("PIL"); pytest.importorskip("reportlab")
    img = _png(tmp_path / "pic.png")
    txt = tmp_path / "notes.txt"; txt.write_text("first line\nsecond line\n" * 40)
    r = client.post("/api/tools/convert", headers=H,
                    json={"action": "make-pdf", "inputs": [str(img), str(txt)], "name": "bundle"})
    assert r.status_code == 200, r.text
    out = r.json()["outputs"][0]
    data = client.get(out["url"]).content
    assert data[:5] == b"%PDF-"
    from pypdf import PdfReader
    client.post("/api/tools/convert", headers=H, json={"action": "make-pdf", "inputs": [str(img)], "name": "a"})
    client.post("/api/tools/convert", headers=H, json={"action": "convert", "inputs": [str(txt)], "format": "pdf"})
    r = client.post("/api/tools/convert", headers=H,
                    json={"action": "merge-pdf", "inputs": [_tools_path(client, "a.pdf"), _tools_path(client, "notes.pdf")],
                          "name": "merged"})
    assert r.status_code == 200, r.text
    merged = client.get(r.json()["outputs"][0]["url"]).content
    assert merged[:5] == b"%PDF-" and len(PdfReader(__import__("io").BytesIO(merged)).pages) >= 2


def _tools_path(client, name):
    s = client.get(f"/api/tools/output/{name}")
    assert s.status_code == 200
    import tempfile, pathlib
    p = pathlib.Path(tempfile.mkdtemp()) / name
    p.write_bytes(s.content)
    return str(p)


def test_convert_image_and_text(client, tmp_path):
    pytest.importorskip("PIL")
    img = _png(tmp_path / "pic.png")
    r = client.post("/api/tools/convert", headers=H, json={"action": "convert", "inputs": [str(img)], "format": "webp"})
    assert r.status_code == 200, r.text
    assert client.get(r.json()["outputs"][0]["url"]).content[:4] == b"RIFF"
    pytest.importorskip("reportlab")
    txt = tmp_path / "doc.md"; txt.write_text("# Title\n\nSome text")
    r = client.post("/api/tools/convert", headers=H, json={"action": "convert", "inputs": [str(txt)], "format": "pdf"})
    assert r.status_code == 200 and client.get(r.json()["outputs"][0]["url"]).content[:5] == b"%PDF-"


def test_convert_audio_via_ffmpeg(client, tmp_path):
    from desk import convert
    if not convert.ffmpeg():
        pytest.skip("no ffmpeg on this machine")
    wav = convert.make_silence_wav(tmp_path / "tone.wav")
    r = client.post("/api/tools/convert", headers=H, json={"action": "convert", "inputs": [str(wav)], "format": "mp3"})
    assert r.status_code == 200, r.text
    assert r.json()["outputs"][0]["bytes"] > 100


def test_convert_pdf_to_text(client, tmp_path):
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    txt = tmp_path / "words.txt"; txt.write_text("the quick brown fox")
    client.post("/api/tools/convert", headers=H, json={"action": "convert", "inputs": [str(txt)], "format": "pdf"})
    pdf_path = _tools_path(client, "words.pdf")
    r = client.post("/api/tools/convert", headers=H, json={"action": "convert", "inputs": [pdf_path], "format": "txt"})
    assert r.status_code == 200 and "quick brown fox" in client.get(r.json()["outputs"][0]["url"]).text


def test_convert_rejects_bad_input(client):
    r = client.post("/api/tools/convert", headers=H, json={"action": "convert", "inputs": ["/nope/gone.pdf"], "format": "txt"})
    assert r.status_code == 400 and "Not found" in r.json()["detail"]
    assert client.get("/api/tools/output/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)
    assert client.get("/api/tools/output/not-a-file.bin").status_code == 404


# ------------------------------------------------------- safety fixes (review)
def test_project_chat_refuses_home_and_drives(client):
    """A project chat can read everything inside its folder — home and drive roots are refused."""
    for bad in (str(Path.home()), str(Path.home().parent), str(Path(Path.home().anchor))):
        r = client.post("/api/sessions", json={"project": bad}, headers=H)
        assert r.status_code == 400, bad


def test_workspace_never_reads_credential_files(tmp_path):
    from desk.tools import ToolError, Workspace
    proj = tmp_path / "proj"
    (proj / ".ssh").mkdir(parents=True)
    (proj / ".env").write_text("TOKEN=abc123")
    (proj / ".env.production").write_text("TOKEN=abc123")
    (proj / ".ssh" / "id_rsa").write_text("key")
    (proj / "certs").mkdir()
    (proj / "certs" / "server.pem").write_text("pem")
    (proj / "ok.py").write_text("print(1)\n")
    ws = Workspace(proj)
    for p in (".env", ".env.production", ".ssh", ".ssh/id_rsa", "certs/server.pem"):
        with pytest.raises(ToolError, match="credentials"):
            asyncio.run(ws.read_file(p))
    assert asyncio.run(ws.read_file("ok.py")).ok
    listing = asyncio.run(ws.list_files()).output
    assert ".env" not in listing and "server.pem" not in listing and "ok.py" in listing


def test_read_tools_redact_secret_values(tmp_path):
    from desk.tools import Workspace
    (tmp_path / "appconfig.py").write_text('API_KEY = "sk-ant-abcdef1234567890abcdef"\n')
    out = asyncio.run(Workspace(tmp_path).read_file("appconfig.py"))
    assert "abcdef1234567890abcdef" not in out.output


def test_output_names_with_spaces_download_and_dedupe(client, tmp_path):
    pytest.importorskip("PIL")
    img = _png(tmp_path / "My Photo.png")
    r = client.post("/api/tools/convert", headers=H,
                    json={"action": "convert", "inputs": [str(img)], "format": "jpg"})
    assert r.status_code == 200, r.text
    out = r.json()["outputs"][0]
    assert " " in out["name"]
    assert client.get(out["url"]).status_code == 200
    again = client.post("/api/tools/convert", headers=H,
                        json={"action": "convert", "inputs": [str(img)], "format": "jpg"})
    other = again.json()["outputs"][0]
    assert other["name"] != out["name"] and client.get(other["url"]).status_code == 200


def test_endpoint_rules_for_remote_and_local(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_endpoint_models", lambda *a, **k: ["m1"])
    # plain http to a remote host would send keys/prompts unencrypted
    r = client.post("/connect/endpoints", headers=H,
                    json={"label": "x", "base_url": "http://llm.example.com/v1"})
    assert r.status_code == 400 and "https" in r.json()["detail"]
    # a remote endpoint isn't a "free" model — it counts as paid so budget caps apply
    client.post("/connect/endpoints", headers=H,
                json={"label": "Corp", "base_url": "https://llm.corp.example/v1", "private": True})
    spec = [s for s in catalog.custom_specs() if "Corp" in s["name"]][0]
    assert spec["free"] is False
    # localhost is private (and free) even without the checkbox
    client.post("/connect/endpoints", headers=H,
                json={"label": "Ollama", "base_url": "http://localhost:11434/v1"})
    ep = [e for e in client.get("/connect/status").json()["endpoints"] if e["label"] == "Ollama"][0]
    assert ep["private"] is True
    spec = [s for s in catalog.custom_specs() if "Ollama" in s["name"]][0]
    assert spec["free"] is True


def test_text_pdf_handles_unicode(tmp_path):
    pytest.importorskip("reportlab")
    pytest.importorskip("pypdf")
    from desk import convert
    txt = tmp_path / "hindi.txt"
    txt.write_text("नमस्ते दुनिया — hello world")
    out = convert.make_pdf([txt], tmp_path / "out.pdf")
    assert out.read_bytes()[:5] == b"%PDF-"
    if convert._uni_font({ord(c) for c in "नमस्ते"}):   # a covering font exists on this machine
        from pypdf import PdfReader
        got = "\n".join(pg.extract_text() or "" for pg in PdfReader(str(out)).pages)
        assert "नमस्ते" in got


# ---------------------------------------------- subscription file protection (review 2)
def test_dotenv_endings_and_unquoted_secrets(tmp_path):
    """`config/prod.env` ends in .env; dotenv values are usually unquoted."""
    from desk.tools import ToolError, Workspace
    proj = tmp_path / "proj"
    (proj / "config").mkdir(parents=True)
    (proj / "config" / "prod.env").write_text("DB_PASSWORD=supersecret123\n")
    (proj / "app.py").write_text("DB_PASSWORD=supersecret123\nprint('x')\n")
    ws = Workspace(proj)
    with pytest.raises(ToolError, match="credentials"):
        asyncio.run(ws.read_file("config/prod.env"))
    assert "prod.env" not in asyncio.run(ws.list_files()).output
    assert "supersecret123" not in asyncio.run(ws.read_file("app.py")).output
    assert "supersecret123" not in asyncio.run(ws.search("supersecret")).output


def test_project_chat_refuses_system_folders(client):
    for bad in ("/etc", "/usr", "/System"):
        if Path(bad).is_dir():
            assert client.post("/api/sessions", json={"project": bad}, headers=H).status_code == 400, bad


def test_unbound_subscription_chat_cant_read_files(desk_factory):
    """With no project bound, Claude sees the whole home dir denied and Codex gets no shell."""
    desk = desk_factory(Scripted())
    a = workers.ClaudeChatRun(desk, "x").args("claude", "hi", {}, {})
    denied = a[a.index("--disallowedTools") + 1:]
    assert "Read" in denied and "Grep" in denied and "Glob" in denied and "--allowedTools" not in a
    c = workers.CodexChatRun(desk, "x").args("codex", "hi", {}, {})
    assert "features.shell_tool=false" in c and "features.unified_exec=false" in c
    # bound to a project: reads stay on (sandboxed), writes still off
    c2 = workers.CodexChatRun(desk, "x").args("codex", "hi", {"project": "/tmp/proj"}, {})
    assert 'sandbox_mode="read-only"' in c2 and not any("shell_tool" in x for x in c2)


def test_chat_deny_rules_cover_credentials(desk_factory):
    for pat in ("Read(**/.env*)", "Read(**/*.env)", "Read(**/id_rsa*)", "Read(**/*.pem)",
                "Read(~/.ssh/**)", "Read(~/.aws/**)", "Read(~/.config/gcloud/**)"):
        assert pat in workers.CHAT_DENY, pat
    a = workers.ClaudeChatRun(desk_factory(Scripted()), "x").args("claude", "hi", {"project": "/tmp/p"}, {})
    denied = a[a.index("--disallowedTools") + 1:]
    assert "Write" in denied and "Edit" in denied and "Bash" in denied


# ----------------------------------------------------------------- legal gate
def test_terms_gate_blocks_until_accepted(client):
    from desk import legal
    catalog.set_settings(terms_version="")
    assert client.post("/api/sessions", json={}, headers=H).status_code == 409
    assert client.get("/api/sessions").status_code == 409
    st = client.get("/api/legal").json()
    assert st["accepted"] is False and "liability" in st["terms"].lower()
    assert client.post("/api/legal/accept", json={"version": "0.0"}, headers=H).status_code == 409
    assert client.post("/api/legal/accept", json={"version": legal.TERMS_VERSION},
                       headers=H).status_code == 200
    assert client.post("/api/sessions", json={}, headers=H).status_code == 200


def test_remote_endpoint_shows_untracked_cost(client, monkeypatch):
    monkeypatch.setattr(catalog, "discover_endpoint_models", lambda *a, **k: ["m1"])
    client.post("/connect/endpoints", headers=H,
                json={"label": "CorpEP", "base_url": "https://llm.corp.example/v1"})
    m = [m for m in client.get("/api/models").json() if m.get("via") == "CorpEP"][0]
    assert m["cost_note"] == "costs not tracked"
