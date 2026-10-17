# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Promptaro HTTP server. Author: Akshay Dubey

Runs locally (127.0.0.1). Serves the UI, the Smart Router's sign-in/connections
endpoints, and the chat/agent API with a live event stream (SSE).
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

HERE = Path(__file__).resolve().parent
ROUTER_ROOT = Path(os.environ.get("SMART_ROUTER_ROOT", HERE.parent.parent / "smart-router")).resolve()
if str(ROUTER_ROOT) not in sys.path:
    sys.path.insert(0, str(ROUTER_ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from router.connect import api as connect_api  # noqa: E402
from router.engine import Router  # noqa: E402
from router.gateway import api as gateway_api  # noqa: E402

from . import gitops  # noqa: E402
from .build import build  # noqa: E402
from .desk import Desk  # noqa: E402
from .store import Store  # noqa: E402

from router import migrate  # noqa: E402

STARTED_BUILD = build()        # code version this process loaded
DATA = migrate.home()
desk: Desk | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global desk
    _private_dir(DATA)                                   # sessions, worktrees, caches — keep to this user
    _private_dir(gitops.HOME / "worktrees")
    router = Router(Path(os.environ.get("SMART_ROUTER_ROOT", ROUTER_ROOT)),
                    db_path=os.environ.get("ROUTER_DB") or DATA / "router.db")
    app.state.router = router
    desk = Desk(router, Store(os.environ.get("PROMPTARO_DB") or os.environ.get("TILLWRIGHT_DB")
                              or DATA / "desk.db"))
    if "PROMPTARO_DB" not in os.environ and "TILLWRIGHT_DB" not in os.environ:
        _private_file(DATA / "desk.db")
    yield
    for t in list(desk.tasks.values()):
        t.cancel()
    await router.aclose()


app = FastAPI(title="Promptaro", version="0.1.0", lifespan=lifespan)
_hosts = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in _hosts if h.strip()])


def _private_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(p, 0o700)


def _private_file(p: Path) -> None:
    if os.name == "posix" and p.exists():
        os.chmod(p, 0o600)


@app.middleware("http")
async def require_app_header(request: Request, call_next):
    """Blocks cross-site requests: a web page can't add this header without a
    CORS preflight, which this server never approves. Both the current
    (x-promptaro) and legacy (x-tillwright) names are accepted during migration."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith(("/api/", "/connect/")) \
            and request.headers.get("x-tillwright") != "1" and request.headers.get("x-promptaro") != "1":
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Missing X-Promptaro header"}, status_code=403)
    return await call_next(request)


app.include_router(connect_api)
app.include_router(gateway_api)          # coding tools: /v1/messages, /v1/chat/completions (token required)
app.mount("/static", StaticFiles(directory=HERE / "ui"), name="static")


def _s(sid: str) -> dict:
    s = desk.store.get(sid)
    if not s:
        raise HTTPException(404, "No such conversation")
    return s


def _git_error(e: gitops.GitError):
    if str(e) == "not_a_repo":
        raise HTTPException(409, {"code": "not_a_repo", "message": "That folder isn't a git repository yet."})
    raise HTTPException(400, str(e))


# ------------------------------------------------------------------ pages
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(HERE / "ui" / "index.html", headers={"cache-control": "no-store"})


@app.get("/healthz")
async def health():
    usable = [m.id for m in desk.router.registry.usable() if m.provider != "offline"]
    current = build()
    return {"ok": True, "ai_available": bool(usable), "models": len(usable), "running": len(desk.tasks),
            "build": STARTED_BUILD, "current": current, "update_ready": current != STARTED_BUILD}


def _reexec() -> None:
    """Replace this process with a fresh copy of itself (same Terminal window, same port)."""
    args = [sys.executable, "-m", "uvicorn", *sys.argv[1:]]
    if platform.system() == "Windows":           # no exec on Windows: start the new copy, then leave
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        os._exit(0)
    os.execv(sys.executable, args)


@app.post("/api/restart")
async def restart():
    """Load updated code without closing anything. Running agents are stopped first."""
    for t in list(desk.tasks.values()):
        t.cancel()
    asyncio.get_running_loop().call_later(0.5, _reexec)
    return {"restarting": True}


@app.post("/api/quit")
async def quit_app():
    """Stop Promptaro (it otherwise keeps running in the background after the window closes)."""
    for t in list(desk.tasks.values()):
        t.cancel()
    asyncio.get_running_loop().call_later(0.3, os._exit, 0)
    return {"quitting": True}


# --------------------------------------------------------------- sessions
@app.get("/api/sessions")
async def sessions():
    return desk.store.list()


class NewChat(BaseModel):
    title: str = "New chat"
    prefer: Literal["balanced", "cheap", "fast", "best"] = "balanced"
    model: str = ""


@app.post("/api/sessions")
async def new_chat(b: NewChat):
    return desk.store.create("chat", b.title, prefer=b.prefer, model=b.model)


@app.get("/api/sessions/{sid}")
async def get_session(sid: str, after: int = 0):
    s = _s(sid)
    s.pop("state", None)
    pending = desk.approvals.get(sid)
    return {"session": s, "events": desk.store.events(sid, after), "running": sid in desk.tasks,
            "pending_approval": pending[0] if pending else None}


class Patch(BaseModel):
    title: str | None = None
    prefer: Literal["balanced", "cheap", "fast", "best"] | None = None
    model: str | None = None
    approval: Literal["ask_risky", "always_ask", "never_ask"] | None = None
    max_cost: float | None = Field(None, gt=0, le=100)


@app.patch("/api/sessions/{sid}")
async def patch_session(sid: str, b: Patch):
    s = _s(sid)
    fields = {k: v for k, v in b.model_dump().items() if v is not None and k != "max_cost"}
    if b.max_cost is not None:
        st = s["state"]
        st["max_cost"] = b.max_cost
        fields["state"] = st
    if fields:
        desk.store.update(sid, **fields)
    return {"ok": True}


@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    s = _s(sid)
    desk.stop(sid)
    if s["kind"] == "agent" and s.get("worktree"):
        await gitops.discard(s["repo"], s["worktree"], s["branch"])
    desk.store.delete(sid)
    return {"ok": True}


class Msg(BaseModel):
    text: str = Field(..., min_length=1, max_length=200_000)


@app.post("/api/sessions/{sid}/messages")
async def send(sid: str, b: Msg):
    s = _s(sid)
    if s["kind"] == "chat":
        if s["status"] == "running":
            raise HTTPException(409, "Still answering the last message.")
        desk.store.emit(sid, "user", {"text": b.text})
        if s["title"] == "New chat":
            desk.store.update(sid, title=b.text.strip().splitlines()[0][:60])
        asyncio.create_task(desk.chat(sid, b.text))
        return {"ok": True}
    return {"ok": True, "delivery": desk.message_agent(sid, b.text)}


class Feedback(BaseModel):
    seq: int
    good: bool


@app.post("/api/sessions/{sid}/feedback")
async def feedback(sid: str, b: Feedback):
    _s(sid)
    try:
        return desk.feedback(sid, b.seq, b.good)
    except KeyError:
        raise HTTPException(404, "No such answer")


class Retry(BaseModel):
    seq: int


@app.post("/api/sessions/{sid}/retry")
async def retry(sid: str, b: Retry):
    s = _s(sid)
    if s["kind"] != "chat":
        raise HTTPException(400, "Only chat answers can be retried.")
    if s["status"] == "running":
        raise HTTPException(409, "Still answering the last message.")
    try:
        desk.retry_stronger(sid, b.seq)
    except KeyError:
        raise HTTPException(404, "No question to retry")
    return {"ok": True}


@app.get("/api/insights")
async def insights():
    """Savings and what the router has learned, for the Savings view."""
    from router import catalog, secrets
    from .workers import subscriptions
    d = desk.insights()
    d["free_quota"] = catalog.FREE.snapshot() if secrets.has("OPENROUTER_API_KEY") else None
    d["subscriptions"] = await subscriptions()
    return d


# ------------------------------------------------------------------ agents
class NewAgent(BaseModel):
    project: str = Field(..., min_length=1)
    task: str = Field(..., min_length=3)
    prefer: Literal["balanced", "cheap", "fast", "best"] = "balanced"
    model: str = ""
    approval: Literal["ask_risky", "always_ask", "never_ask"] = "ask_risky"
    max_cost: float = Field(2.0, gt=0, le=100)


@app.post("/api/agents")
async def new_agent(b: NewAgent):
    try:
        return await desk.start_agent(b.project, b.task, prefer=b.prefer, model=b.model, approval=b.approval,
                                      max_cost=b.max_cost)
    except gitops.GitError as e:
        _git_error(e)


class PathBody(BaseModel):
    path: str


@app.post("/api/repos/init")
async def init_repo(b: PathBody):
    try:
        await gitops.init_repo(b.path)
    except gitops.GitError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/sessions/{sid}/stop")
async def stop(sid: str):
    _s(sid)
    desk.stop(sid)
    return {"ok": True}


class Approval(BaseModel):
    id: str
    approved: bool


@app.post("/api/sessions/{sid}/approve")
async def approve(sid: str, b: Approval):
    if not desk.approve(sid, b.id, b.approved):
        raise HTTPException(409, "Nothing waiting for approval.")
    return {"ok": True}


@app.get("/api/sessions/{sid}/diff")
async def diff(sid: str):
    _s(sid)
    return await desk.review(sid)


class CommitBody(BaseModel):
    message: str = Field("", max_length=2000)


def _agent(s) -> None:
    """Git endpoints only make sense for agent sessions with a worktree."""
    if s["kind"] != "agent" or not s.get("worktree"):
        raise HTTPException(400, "This conversation has no project folder.")


@app.post("/api/sessions/{sid}/commit")
async def commit(sid: str, b: CommitBody):
    s = _s(sid); _agent(s)
    try:
        sha = await gitops.commit(s["worktree"], b.message or f"{s['title']}\n\nMade with Promptaro")
    except gitops.GitError as e:
        raise HTTPException(400, str(e))
    desk.store.emit(sid, "git", {"text": f"Committed {sha} on {s['branch']}"})
    return {"sha": sha}


@app.post("/api/sessions/{sid}/merge")
async def merge(sid: str):
    s = _s(sid); _agent(s)
    try:
        await gitops.commit(s["worktree"], f"{s['title']}\n\nMade with Promptaro")
        sha = await gitops.merge_into_base(s["repo"], s["branch"], s["base_branch"])
    except gitops.GitError as e:
        raise HTTPException(400, str(e))
    desk.store.emit(sid, "git", {"text": f"Merged {s['branch']} into {s['base_branch']} ({sha})"})
    desk.store.update(sid, status="done")
    desk.learn_from_agent(sid, True)
    return {"sha": sha}


@app.post("/api/sessions/{sid}/pr")
async def pr(sid: str):
    s = _s(sid); _agent(s)
    summary = next((e["data"].get("summary", "") for e in reversed(desk.store.events(sid)) if e["kind"] == "finish"), "")
    try:
        await gitops.commit(s["worktree"], f"{s['title']}\n\nMade with Promptaro")
        url = await gitops.open_pr(s["worktree"], s["branch"], s["title"], summary or s["title"])
    except gitops.GitError as e:
        raise HTTPException(400, str(e))
    desk.store.emit(sid, "git", {"text": f"Opened pull request: {url}", "url": url})
    return {"url": url}


@app.post("/api/sessions/{sid}/discard")
async def discard(sid: str):
    s = _s(sid); _agent(s)
    finished = s["status"] == "review" and any(e["kind"] == "finish" for e in desk.store.events(sid))
    desk.stop(sid)
    await gitops.discard(s["repo"], s["worktree"], s["branch"])
    if finished:
        desk.learn_from_agent(sid, False)
    desk.store.emit(sid, "git", {"text": f"Discarded {s['branch']} and its changes."})
    desk.store.update(sid, status="discarded")
    return {"ok": True}


class RunBody(BaseModel):
    command: str = Field(..., min_length=1, max_length=4000)


@app.post("/api/sessions/{sid}/run")
async def manual_run(sid: str, b: RunBody):
    s = _s(sid)
    if s["kind"] != "agent" or not Path(s.get("worktree") or "/nonexistent").exists():
        raise HTTPException(400, "This conversation has no project folder.")
    return await desk.manual_run(sid, b.command)


class OpenBody(BaseModel):
    app: Literal["code", "cursor", "finder", "terminal"] = "code"


@app.post("/api/sessions/{sid}/open")
async def open_in(sid: str, b: OpenBody):
    s = _s(sid)
    path = s.get("worktree")
    if not path or not Path(path).exists():
        raise HTTPException(400, "The project copy no longer exists.")
    mac = platform.system() == "Darwin"
    if b.app in ("code", "cursor"):
        exe = shutil.which(b.app)
        if exe:
            subprocess.Popen([exe, path])
        elif mac:
            subprocess.Popen(["open", "-a", "Visual Studio Code" if b.app == "code" else "Cursor", path])
        else:
            raise HTTPException(400, f"`{b.app}` command not found.")
    elif b.app == "terminal" and mac:
        subprocess.Popen(["open", "-a", "Terminal", path])
    elif platform.system() == "Windows":
        os.startfile(path)                                   # noqa: S606 — opens Explorer on the folder
    else:
        subprocess.Popen(["open" if mac else "xdg-open", path])
    return {"ok": True}


# --------------------------------------------------------------- helpers
@app.get("/api/subscriptions")
async def subscriptions(refresh: bool = False):
    from .workers import subscriptions as subs
    return await subs(force=refresh)


@app.get("/api/models")
async def models():
    from .workers import subscriptions as subs
    reg = desk.router.registry
    usable = {m.id for m in reg.usable()}
    st = await subs()
    extra = [{"id": mid, "name": name, "via": "subscription", "tier": 3, "free": False, "subscription": True,
              "price_in": 0, "price_out": 0}
             for mid, key, name in (("claude-code", "claude_code", "Claude Code (your Claude subscription)"),
                                    ("codex", "codex", "Codex (your ChatGPT subscription)"))
             if st[key]["subscription"]]
    return extra + [{"id": m.id, "name": m.extra.get("name", m.id), "via": m.extra.get("via", ""), "tier": m.tier,
             "free": bool(m.extra.get("free")), "vendor": m.extra.get("vendor", ""),
             "price_in": round(m.price_in, 3), "price_out": round(m.price_out, 3)}
            for m in sorted(reg.models.values(), key=lambda m: (m.tier, m.price_in))
            if m.id in usable and m.provider != "offline"]


# ----------------------------------------------------------- coding tools
@app.get("/api/gateway")
async def gateway_info(request: Request):
    from router import catalog
    from . import launchers
    from .workers import find_claude
    port = request.url.port or 8770
    base = f"http://127.0.0.1:{port}"
    st = catalog.get_settings()
    d = launchers.bin_dir()
    return {"base_url": base, "openai_base_url": f"{base}/v1", "model": "promptaro-auto",
            "spend": st.get("gateway_spend", "free"), "free_models": st.get("free_models", "chat"),
            "launchers": {"installed": launchers.installed(), "dir": str(d), "on_path": launchers.on_path(d)},
            "claude_installed": bool(find_claude()), "aider_installed": bool(shutil.which("aider")),
            "recent": list(desk.gateway_log)[-15:][::-1], "os": platform.system()}


@app.post("/api/gateway/token")
async def gateway_token_reveal():
    """The gateway token, for this computer's own programs (the app header keeps web pages out)."""
    from router.gateway import gateway_token
    return {"token": gateway_token()}


@app.post("/api/gateway/rotate")
async def gateway_rotate():
    from router.gateway import rotate_token
    rotate_token()
    return {"ok": True}


@app.post("/api/gateway/launchers")
async def gateway_launchers(request: Request):
    from . import launchers
    return launchers.install(request.url.port or 8770)


@app.get("/api/free")
async def free_status():
    """Free providers: connected or not, limits, privacy, and what's been used today."""
    from router import quota, secrets
    from router.free_providers import FREE_PROVIDERS
    reg = desk.router.registry
    return {"providers": [{"id": p.id, "label": p.label, "connected": secrets.has(p.key_env),
                           "get_key_url": p.get_key_url, "trains": p.trains, "limits": p.limits, "note": p.note,
                           "checked": p.checked, "source": p.source} for p in FREE_PROVIDERS.values()],
            "usage": quota.LEDGER.snapshot(list(reg.usable()))}


@app.get("/api/projects")
async def projects():
    seen, out = set(), []
    for s in desk.store.list(500):
        p = s.get("project")
        if p and p not in seen and Path(p).exists():
            seen.add(p)
            out.append(p)
    return out[:12]


@app.post("/api/pick-folder")
async def pick_folder():
    """The system's own folder chooser (macOS, Windows, Linux with zenity/kdialog)."""
    system = platform.system()
    if system == "Darwin":
        cmd = ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a project folder")']
    elif system == "Windows":
        cmd = ["powershell", "-NoProfile", "-STA", "-Command",
               "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
               "$d.Description = 'Choose a project folder'; if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"]
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--directory", "--title=Choose a project folder"]
    elif shutil.which("kdialog"):
        cmd = ["kdialog", "--getexistingdirectory", str(Path.home())]
    else:
        raise HTTPException(501, "Type or paste the folder path.")
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        return {"path": ""}
    p = out.decode().strip()
    return {"path": p.rstrip("/\\") if len(p) > 3 else p}


_stream_keys: dict[str, float] = {}       # short-lived SSE keys (single use, ~60s)


@app.post("/api/stream-key")
async def stream_key():
    """A short-lived token for the SSE stream — NOT the gateway token, which stays out of URLs."""
    import secrets as _sec
    now = time.time()
    for k in [k for k, exp in _stream_keys.items() if exp <= now]:
        _stream_keys.pop(k, None)
    key = "sk-" + _sec.token_urlsafe(24)
    _stream_keys[key] = now + 60
    return {"key": key}


@app.get("/api/stream")
async def stream(request: Request, key: str = ""):
    """One live stream for everything; the UI filters by session. EventSource can't send
    headers, so the stream key rides in the URL — it expires in a minute and is single-use."""
    exp = _stream_keys.get(key)                 # reusable within its minute so EventSource auto-reconnects
    if exp is None or exp < time.time():
        raise HTTPException(401, "The event stream needs a stream key.")
    q = desk.store.subscribe("*")

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), 15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            desk.store.unsubscribe("*", q)
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"cache-control": "no-store"})
