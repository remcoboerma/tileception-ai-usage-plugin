import json, os, shutil, subprocess, sys, unittest
from pathlib import Path

from tileception_usage import (Win, chips, fmt_delta, iso, panel, parse_claude, parse_codex,
                               parse_go, view)

ROOT = Path(__file__).resolve().parent
NOW = 1_800_000_000.0


class Parsers(unittest.TestCase):
    def test_claude(self):
        d = {"five_hour": {"utilization": 42.0, "resets_at": "2027-01-15T08:00:00.123+00:00"},
             "seven_day": {"utilization": 10, "resets_at": None},
             "seven_day_opus": None, "extra": {"utilization": 99}}
        w = parse_claude(d)
        self.assertEqual([x.label for x in w], ["5 uur", "week"])
        self.assertEqual(w[0].used, 42.0)
        self.assertIsNone(w[1].resets)

    def test_codex_label_from_duration(self):
        d = {"rate_limit": {"primary_window": {"used_percent": 5, "limit_window_seconds": 604800, "reset_at": 1},
                            "secondary_window": {"used_percent": 7, "limit_window_seconds": 18000,
                                                 "reset_after_seconds": 60}}}
        w = parse_codex(d, now=NOW)
        self.assertEqual([x.label for x in w], ["week", "5 uur"])
        self.assertEqual(w[1].resets, NOW + 60)

    def test_codex_empty(self):
        self.assertEqual(parse_codex({}), [])

    def test_go_variants(self):
        d = {"usage": {"rolling": {"usedPercent": 12, "resetInSec": 100},
                       "weekly": {"used_percent": 3, "resetsAt": "2027-01-15T08:00:00Z"},
                       "monthly": {"percent": 1}}}
        w = parse_go(d, NOW)
        self.assertEqual([x.label for x in w], ["5 uur", "week", "maand"])
        self.assertEqual(w[0].resets, NOW + 100)
        self.assertEqual(w[1].resets, iso("2027-01-15T08:00:00Z"))
        self.assertIsNone(w[2].resets)


class Rendering(unittest.TestCase):
    def test_fresh_stale_reset(self):
        w = Win("claude", "5 uur", 85.0, NOW + 3600)
        self.assertEqual(view(w, NOW, NOW, True)["tint"], "warm")
        self.assertEqual(view(Win("claude", "x", 95.0, None), NOW, NOW, True)["tint"], "danger")
        s = view(w, NOW - 3600, NOW, True)           # ouder dan TTL
        self.assertEqual((s["state"], s["text"], s["tint"]), ("stale", "~85%", "muted"))
        s = view(w, NOW, NOW, False)                 # token verlopen
        self.assertEqual(s["state"], "stale")
        r = view(Win("claude", "x", 85.0, NOW - 1), NOW, NOW, True)
        self.assertEqual((r["state"], r["pct"]), ("reset", 0.0))

    def test_chip_is_max_window(self):
        snaps = {"claude": {"taken": NOW, "wins": [
            {"provider": "claude", "label": "5 uur", "used": 10.0, "resets": NOW + 60},
            {"provider": "claude", "label": "week", "used": 55.0, "resets": NOW + 60}]}}
        c = chips(snaps, {"claude": True}, NOW)["chips"]
        self.assertEqual([x["label"] for x in c], ["Claude 55%"])

    def test_panel_active_counts_over_80(self):
        snaps = {"codex": {"taken": NOW, "wins": [
            {"provider": "codex", "label": "5 uur", "used": 81.0, "resets": None},
            {"provider": "codex", "label": "week", "used": 20.0, "resets": None}]}}
        p = panel(snaps, {"codex": True}, NOW)
        self.assertEqual(p["active"], 1)
        self.assertEqual(len(p["views"][0]["items"]), 2)
        self.assertEqual(p["views"][0]["items"][0]["progress"], {"value": 81.0})

    def test_panel_empty_and_all_stale(self):
        self.assertIn("notice", panel({}, {}, NOW))
        snaps = {"codex": {"taken": NOW - 5000, "wins": [
            {"provider": "codex", "label": "week", "used": 20.0, "resets": None}]}}
        p = panel(snaps, {"codex": False}, NOW)
        self.assertEqual((p["summary"], p["active"]), ("verouderd", 0))

    def test_panel_issue_rows_next_to_data(self):
        snaps = {"claude": {"taken": NOW, "wins": [
            {"provider": "claude", "label": "week", "used": 20.0, "resets": None}]}}
        p = panel(snaps, {"claude": True}, NOW, {"claude": "", "go": "geen API-key"})
        items = p["views"][0]["items"]
        self.assertEqual([i["type"] for i in items], ["card", "row"])
        self.assertEqual(items[1]["detail"], "geen API-key")

    def test_fmt_delta(self):
        self.assertEqual(fmt_delta(59), "0m")
        self.assertEqual(fmt_delta(3700), "1u 1m")
        self.assertEqual(fmt_delta(90000), "1d 1u")


class Cli(unittest.TestCase):
    """Draait de echte CLI met een nep-HOME binnen de repo (nooit het echte HOME)."""

    def setUp(self):
        self.home = ROOT / ".fake-home"
        shutil.rmtree(self.home, ignore_errors=True)
        self.home.mkdir()
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home),
                    "PYTHONPATH": str(ROOT.parent / "src")}

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, "-m", "tileception_usage", *args], env=self.env,
                              input=stdin, capture_output=True, text=True, timeout=20)

    def test_install_status_uninstall(self):
        manifest = self.home / ".local/share/tileception/plugins/usage.toml"
        self.assertEqual(self.run_cli("install", "--non-interactive").returncode, 0)
        text = manifest.read_text()
        self.assertRegex(text, r'(?m)^args = \[.*"serve"\]$')
        self.assertIn("[bar]", text)
        st = json.loads(self.run_cli("status").stdout)
        self.assertFalse(st["configured"])
        self.assertIn("go: geen API-key", st["detail"])
        self.assertIn("claude:", st["detail"])
        self.assertEqual(st["setup"]["steps"][0]["fields"][0]["secret"], True)
        self.assertEqual(self.run_cli("uninstall", "--purge").returncode, 0)
        self.assertFalse(manifest.exists())

    def test_mcp_handshake_and_notification_order(self):
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "tileception://panel"}},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "nope://x"}},
            {"jsonrpc": "2.0", "id": 5, "method": "resources/unsubscribe", "params": {"uri": "tileception://bar"}},
            {"jsonrpc": "2.0", "id": 6, "method": "bogus"},
        ]
        out = self.run_cli("serve", stdin="\n".join(map(json.dumps, msgs)) + "\n")
        lines = [json.loads(l) for l in out.stdout.splitlines()]
        by_id = {l["id"]: l for l in lines if "id" in l}
        self.assertEqual(by_id[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual([r["uri"] for r in by_id[2]["result"]["resources"]],
                         ["tileception://bar", "tileception://panel"])
        items = json.loads(by_id[3]["result"]["contents"][0]["text"])["views"][0]["items"]
        self.assertEqual([i["id"] for i in items], ["issue-claude", "issue-codex", "issue-go"])
        self.assertIn("plugins configure", items[2]["detail"])
        self.assertEqual(by_id[4]["error"]["code"], -32602)
        self.assertEqual(by_id[5]["result"], {})
        self.assertEqual(by_id[6]["error"]["code"], -32601)
        # geen updated-notificatie vóór initialize-respons/initialized
        first_note = next((i for i, l in enumerate(lines) if l.get("method") == "notifications/resources/updated"), None)
        if first_note is not None:
            self.assertGreater(first_note, lines.index(by_id[1]))

    def test_tool_errors_and_setup(self):
        call = lambda i, n, a=None: {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                     "params": {"name": n, "arguments": a or {}}}
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                call(2, "nope"), call(3, "refresh"), call(4, "setup", {"opencode_key": " k "})]
        out = self.run_cli("serve", stdin="\n".join(map(json.dumps, msgs)) + "\n")
        by_id = {l["id"]: l for l in map(json.loads, out.stdout.splitlines()) if "id" in l}
        self.assertTrue(by_id[2]["result"]["isError"])
        self.assertEqual(by_id[3]["result"]["structuredContent"], {"ok": True})
        self.assertEqual(by_id[4]["result"]["structuredContent"], {"ok": True})
        cfg = self.home / ".config/tileception-usage/config.json"
        self.assertEqual(json.loads(cfg.read_text()), {"opencode_key": "k"})
        self.assertEqual(cfg.stat().st_mode & 0o777, 0o600)

    def test_config_permissions(self):
        from tileception_usage import save
        p = self.home / "c" / "config.json"
        save(p, {"opencode_key": "x"})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
