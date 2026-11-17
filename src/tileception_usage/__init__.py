"""tileception-usage: Claude, Codex en OpenCode Go limieten als Tileception-plugin (Linux, stdlib)."""
from __future__ import annotations
import argparse, base64, json, os, shutil, sys, threading, time, urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple

HOME = Path.home()
xdg = lambda var, fallback: Path(os.environ.get(var) or HOME / fallback)
CFG = xdg("XDG_CONFIG_HOME", ".config") / "tileception-usage" / "config.json"
STATE = xdg("XDG_STATE_HOME", ".local/state") / "tileception-usage" / "snapshots.json"
MANIFEST = xdg("XDG_DATA_HOME", ".local/share") / "tileception" / "plugins" / "usage.toml"
URIS = ("tileception://bar", "tileception://panel")
TTL, POLL, MIN_GAP, BACKOFF_MAX = 600, 300, 60, 1800

@dataclass(frozen=True)
class Win:
    provider: str
    label: str
    used: float
    resets: float | None  # epoch seconds

# ---------- pure: parsing ----------
def iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def jwt_exp(tok: str) -> float:
    try:
        p = tok.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))["exp"])
    except Exception:
        return 0.0

def parse_claude(d: dict) -> list[Win]:
    names = {"five_hour": "5 uur", "seven_day": "week",
             "seven_day_opus": "week Opus", "seven_day_sonnet": "week Sonnet"}
    return [Win("claude", names[k], float(v["utilization"]),
                iso(v["resets_at"]) if v.get("resets_at") else None)
            for k, v in d.items()
            if k in names and isinstance(v, dict) and v.get("utilization") is not None]

def parse_codex(d: dict, now: float = 0.0) -> list[Win]:
    label = lambda s: ({18000: "5 uur", 604800: "week"}.get(s)
                       or (f"{s // 86400} dagen" if s >= 86400 else f"{s // 3600} uur"))
    def resets(w):
        if w.get("reset_at") is not None:
            return float(w["reset_at"])
        after = w.get("reset_after_seconds")
        return (now or time.time()) + after if after is not None else None
    rl = d.get("rate_limit") or {}
    return [Win("codex", label(w.get("limit_window_seconds") or 0),
                float(w["used_percent"]), resets(w))
            for w in (rl.get("primary_window"), rl.get("secondary_window"))
            if w and w.get("used_percent") is not None]

def parse_go(d: dict, now: float) -> list[Win]:
    names = {"rolling": "5 uur", "weekly": "week", "monthly": "maand"}
    pick = lambda m, ks: next((m[k] for k in ks if m.get(k) is not None), None)
    out = []
    for k, label in names.items():
        m = (d.get("usage") or d).get(k)
        if not isinstance(m, dict):
            continue
        pct = pick(m, ("usedPercent", "used_percent", "usagePercent", "usage_percent", "percent"))
        r = pick(m, ("resetsAt", "resets_at"))
        secs = pick(m, ("resetInSec", "resets_in_seconds"))
        resets = iso(r) if isinstance(r, str) else (now + secs if secs is not None else None)
        if pct is not None:
            out.append(Win("go", label, float(pct), resets))
    return out

# ---------- pure: rendering ----------
tint = lambda p: "danger" if p >= 90 else "warm" if p >= 70 else "accent"
NAMES = {"claude": "Claude", "codex": "Codex", "go": "Go"}

def fmt_delta(s: float) -> str:
    s = max(int(s), 0)
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}u" if d else f"{h}u {m}m" if h else f"{m}m"

def view(w: Win, taken: float, now: float, ready: bool) -> dict:
    age = now - taken
    if w.resets and w.resets <= now:
        return dict(state="reset", text="reset", pct=0.0, tint="muted",
                    detail="venster gereset, stand onbekend")
    stale = not ready or age >= TTL
    rst = f"reset over {fmt_delta(w.resets - now)}" if w.resets else ""
    if stale:
        return dict(state="stale", text=f"~{w.used:.0f}%", pct=w.used, tint="muted",
                    detail=" · ".join(x for x in (f"{fmt_delta(age)} oud", rst) if x))
    return dict(state="fresh", text=f"{w.used:.0f}%", pct=w.used, tint=tint(w.used), detail=rst)

def views(snaps: dict, ready: dict, now: float):
    for name, s in snaps.items():
        for raw in s.get("wins", []):
            w = Win(**raw)
            yield name, w, view(w, s["taken"], now, ready.get(name, False))

def chips(snaps, ready, now) -> dict:
    best: dict[str, tuple[Win, dict]] = {}
    for name, w, v in views(snaps, ready, now):
        if name not in best or v["pct"] > best[name][1]["pct"]:
            best[name] = (w, v)
    return {"chips": [{"label": f"{NAMES[n]} {v['text']}", "icon": "glyph:clock", "tint": v["tint"]}
                      for n, (w, v) in best.items()]}

def panel(snaps, ready, now, issues: dict | None = None) -> dict:
    vs = list(views(snaps, ready, now))
    rows = [{"type": "row", "id": f"issue-{n}", "title": NAMES[n], "detail": why_not,
             "icon": "glyph:clock", "tint": "muted", "dim": True}
            for n, why_not in (issues or {}).items() if why_not]
    if not vs and not rows:
        return {"title": "AI-usage", "icon": "glyph:clock", "summary": "geen data",
                "notice": {"text": "Geen bron gevonden: log in met claude of codex, of stel een OpenCode Go-key in."}}
    cards = [{"type": "card", "id": f"{n}-{w.label}", "title": f"{NAMES[n]} · {w.label}",
              "value": v["text"], "detail": v["detail"], "icon": "glyph:clock",
              "tint": v["tint"], "progress": {"value": v["pct"]}} for n, w, v in vs]
    live = [v["pct"] for _, _, v in vs if v["state"] == "fresh"]
    return {"title": "AI-usage", "icon": "glyph:clock",
            "summary": f"{max(live):.0f}%" if live else "verouderd",
            "active": sum(p >= 80 for p in live),
            "views": [{"title": "Limieten", "items": cards + rows}]}

# ---------- I/O: providers ----------
def http_json(url: str, headers: dict) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5) as r:
        return json.load(r)

def read_json(p: Path) -> dict:
    return json.loads(p.read_text())

class Provider(NamedTuple):
    name: str
    watch: Callable[[], Path | None]
    ready: Callable[[dict], bool]
    fetch: Callable[[dict], list[Win]]

claude_file = lambda: Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / ".credentials.json"
codex_file = lambda: Path(os.environ.get("CODEX_HOME") or HOME / ".codex") / "auth.json"
go_key = lambda cfg: os.environ.get("OPENCODE_API_KEY") or cfg.get("opencode_key") or ""

def claude_ready(cfg):
    try:
        return read_json(claude_file())["claudeAiOauth"]["expiresAt"] / 1000 > time.time()
    except Exception:
        return False

def claude_fetch(cfg):
    tok = read_json(claude_file())["claudeAiOauth"]["accessToken"]
    return parse_claude(http_json("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0", "Accept": "application/json"}))

def codex_ready(cfg):
    try:
        return jwt_exp(read_json(codex_file())["tokens"]["access_token"]) > time.time()
    except Exception:
        return False

def codex_fetch(cfg):
    t = read_json(codex_file())["tokens"]
    h = {"Authorization": f"Bearer {t['access_token']}", "Accept": "application/json",
         "User-Agent": "tileception-usage/0.1"}
    if t.get("account_id"):
        h["ChatGPT-Account-Id"] = t["account_id"]
    return parse_codex(http_json("https://chatgpt.com/backend-api/wham/usage", h))

go_ready = lambda cfg: bool(go_key(cfg))
go_fetch = lambda cfg: parse_go(http_json("https://opencode.ai/zen/go/v1/usage", {
    "Authorization": f"Bearer {go_key(cfg)}", "User-Agent": "tileception-usage/0.1"}), time.time())

PROVIDERS = (
    Provider("claude", claude_file, claude_ready, claude_fetch),
    Provider("codex", codex_file, codex_ready, codex_fetch),
    Provider("go", lambda: None, go_ready, go_fetch),
)

def why_not(name: str, cfg: dict) -> str:
    """Waarom er geen verse data is, zonder tokens te tonen. Leeg = bron is gereed."""
    try:
        if name == "go":
            return "" if go_key(cfg) else "geen API-key: kies 'plugins configure'"
        f = claude_file() if name == "claude" else codex_file()
        if not f.exists():
            return f"{f} ontbreekt: log in met {name}"
        d = read_json(f)
        if name == "claude":
            exp = d["claudeAiOauth"]["expiresAt"] / 1000
        else:
            t = d.get("tokens")
            if not t:
                return "auth.json heeft geen tokens (API-key login?): geen limietdata beschikbaar"
            exp = jwt_exp(t["access_token"])
        if exp <= 0:
            return "token onleesbaar"
        left = exp - time.time()
        return "" if left > 0 else f"token {fmt_delta(-left)} geleden verlopen: gebruik {name} om te verversen"
    except Exception as e:
        return f"login-bestand onleesbaar ({type(e).__name__})"

# ---------- I/O: config, state, poll loop ----------
def load(p: Path, default):
    try:
        return read_json(p)
    except Exception:
        return default

def save(p: Path, obj, mode=0o600):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)  # nooit even world-readable
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(obj))
    os.chmod(tmp, mode)  # ook als .tmp al bestond met andere rechten
    tmp.replace(p)

mtime = lambda p: p.stat().st_mtime if p and p.exists() else None
log = lambda *a: print("tileception-usage:", *a, file=sys.stderr, flush=True)

def poll_loop(cfg: dict, snaps: dict, health: dict, kick: threading.Event, emit: Callable[[], None]):
    due = {p.name: 0.0 for p in PROVIDERS}
    gap = {p.name: 0.0 for p in PROVIDERS}
    backoff = {p.name: MIN_GAP for p in PROVIDERS}
    seen = {p.name: mtime(p.watch()) for p in PROVIDERS}
    last_emit = 0.0
    while True:
        now = time.time()
        for p in PROVIDERS:
            m = mtime(p.watch())
            changed, seen[p.name] = m != seen[p.name], m
            if not p.ready(cfg):
                continue
            if now >= due[p.name] or (changed and now >= gap[p.name]):
                try:
                    wins = p.fetch(cfg)
                    snaps[p.name] = {"taken": now, "wins": [asdict(w) for w in wins]}
                    save(STATE, snaps)
                    due[p.name], backoff[p.name] = now + POLL, MIN_GAP
                    health[p.name] = ""
                except Exception as e:
                    log(p.name, "poll mislukt:", repr(e))
                    health[p.name] = f"poll mislukt: {type(e).__name__}: {e}"
                    due[p.name] = now + backoff[p.name]
                    backoff[p.name] = min(backoff[p.name] * 2, BACKOFF_MAX)
                gap[p.name] = now + MIN_GAP
                emit()
        if now - last_emit >= 60:
            emit(); last_emit = now
        if kick.wait(5):
            kick.clear(); due = {k: 0.0 for k in due}; gap = {k: 0.0 for k in gap}

# ---------- MCP ----------
SETUP = {"title": "OpenCode Go instellen", "icon": "glyph:clock", "steps": [
    {"id": "key", "kind": "fields", "tool": "setup", "timeout": 10, "submit": "Opslaan",
     "text": "Leeg laten om de huidige key te houden. Claude en Codex gebruiken hun eigen login.",
     "fields": [{"name": "opencode_key", "label": "OpenCode Go API-key", "secret": True}]}]}

TOOLS = [{"name": n, "description": d, "inputSchema": {"type": "object"}} for n, d in
         (("launcher_info", "metadata"), ("refresh", "poll nu"), ("setup", "bewaar instellingen"))]

def serve():
    cfg, snaps, kick = load(CFG, {}), load(STATE, {}), threading.Event()
    health: dict[str, str] = {}
    out_lock = threading.Lock()

    def send(o):
        with out_lock:
            sys.stdout.write(json.dumps(o, separators=(",", ":")) + "\n"); sys.stdout.flush()

    up = threading.Event()
    emit = lambda: up.is_set() and [send({"jsonrpc": "2.0", "method": "notifications/resources/updated",
                                          "params": {"uri": u}}) for u in URIS]
    ready = lambda: {p.name: p.ready(cfg) for p in PROVIDERS}
    issues = lambda: {p.name: why_not(p.name, cfg) or health.get(p.name, "") for p in PROVIDERS}
    render = {"tileception://bar": lambda s, r, n: chips(s, r, n),
              "tileception://panel": lambda s, r, n: panel(s, r, n, issues())}

    def call(name, args):
        if name == "launcher_info":
            return {"name": "AI-usage", "description": "Limieten", "icon": "glyph:clock", "prefixes": []}
        if name == "setup":
            if args.get("opencode_key"):
                cfg["opencode_key"] = args["opencode_key"].strip(); save(CFG, cfg)
        elif name != "refresh":
            raise ValueError(f"onbekende tool: {name}")
        kick.set()
        return {"ok": True}

    def handle(m):
        method, p = m.get("method"), m.get("params") or {}
        if method == "initialize":
            return {"protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}, "resources": {"subscribe": True}},
                    "serverInfo": {"name": "tileception-usage", "version": "0.1.0"}}
        if method in ("ping", "resources/subscribe", "resources/unsubscribe"):
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            try:
                r = call(p.get("name"), p.get("arguments") or {})
            except Exception as e:  # tool-fouten zijn resultaten met isError, geen protocolfouten
                return {"isError": True, "content": [{"type": "text", "text": str(e)}]}
            return {"structuredContent": r, "content": [{"type": "text", "text": json.dumps(r)}]}
        if method == "resources/list":
            return {"resources": [{"uri": u, "name": u.rsplit("/", 1)[-1], "mimeType": "application/json"}
                                  for u in URIS]}
        if method == "resources/read":
            u = p.get("uri")
            if u not in render:
                raise ValueError(f"onbekende resource: {u}")
            body = render[u](dict(snaps), ready(), time.time())  # kopie: poll-thread schrijft mee
            return {"contents": [{"uri": u, "mimeType": "application/json", "text": json.dumps(body)}]}
        raise LookupError(method)

    threading.Thread(target=poll_loop, args=(cfg, snaps, health, kick, emit), daemon=True).start()
    for line in sys.stdin:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if "id" not in m:
            if m.get("method") == "notifications/initialized":
                up.set()
            continue
        try:
            send({"jsonrpc": "2.0", "id": m["id"], "result": handle(m)})
        except LookupError as e:
            send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": f"method not found: {e}"}})
        except ValueError as e:
            send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32602, "message": str(e)}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": repr(e)}})
    os._exit(0)

# ---------- installable-plugin contract ----------
def launch() -> tuple[str, list[str]]:
    """Command + args voor het manifest: het script dat nu draait, dan PATH, dan `python -m`."""
    me = Path(sys.argv[0]).absolute()
    if me.name == "tileception-usage" and me.is_file():
        return str(me), ["serve"]
    found = shutil.which("tileception-usage")
    if found:
        return found, ["serve"]
    return sys.executable, ["-m", "tileception_usage", "serve"]

def status():
    cfg = load(CFG, {})
    avail = [p.name for p in PROVIDERS if p.ready(cfg) or (p.watch() and p.watch().exists())]
    detail = "; ".join(f"{p.name}: {why_not(p.name, cfg) or 'ok'}" for p in PROVIDERS)
    print(json.dumps({"configured": bool(avail),
                      "detail": detail,
                      "setup": SETUP}))

def install():
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    cmd, args = launch()
    MANIFEST.write_text(f"command = {json.dumps(cmd)}\nargs = {json.dumps(args)}\n[bar]\n")

def uninstall(purge: bool):
    MANIFEST.unlink(missing_ok=True)
    if purge:
        shutil.rmtree(CFG.parent, ignore_errors=True); shutil.rmtree(STATE.parent, ignore_errors=True)

def configure():
    from getpass import getpass
    cfg = load(CFG, {})
    have = "ingesteld" if cfg.get("opencode_key") else "niet ingesteld"
    key = getpass(f"OpenCode Go API-key ({have}; leeg = ongewijzigd, invoer is verborgen): ").strip()
    if key:
        cfg["opencode_key"] = key; save(CFG, cfg)
        print(f"opgeslagen in {CFG} (rechten 0600)")
    print("Claude en Codex gebruiken hun eigen login; daar is niets in te stellen.")

def main():
    ap = argparse.ArgumentParser(prog="tileception-usage")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("serve"); sp.add_parser("status"); sp.add_parser("configure")
    sp.add_parser("install").add_argument("--non-interactive", action="store_true")
    sp.add_parser("uninstall").add_argument("--purge", action="store_true")
    a = ap.parse_args()
    {"serve": serve, "status": status, "configure": configure, "install": install,
     "uninstall": lambda: uninstall(a.purge)}[a.cmd]()

if __name__ == "__main__":
    main()
