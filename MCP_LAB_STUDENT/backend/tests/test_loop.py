"""test_loop.py — сквозные прогоны run_agent на ScriptedLLM + протокол MCP.

Браузерные прогоны используют реальный MCP-клиент + сервер
(Playwright нужен Chrome; если его нет — тесты e2e скипаются).
"""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import agent as agent_mod                                    # noqa: E402
from llm import ScriptedLLM, normalize_plan                   # noqa: E402
from scenarios import scenario_for_url                       # noqa: E402


def llm(decide, verdict="CAN"):
    s = ScriptedLLM.__new__(ScriptedLLM)
    s.script = {"classify": {"verdict": verdict,
                             "reason": "test"}}
    s._decide = list(decide)
    s.tokens_used = 0
    s.last_call_ms = 0
    s.last_prompt_tokens = 0
    return s


class FakeClient:
    """Подмена McpClient: снапшоты из фикстуры, вызовы — в память.
    Позволяет гонять цикл без Chrome."""
    def __init__(self, states, calls_log):
        self._states = list(states)
        self.calls = calls_log

    def list_tools(self):
        return [{"name": "browser_navigate",
                 "inputSchema": {"type": "object",
                                 "properties": {"url": {"type": "string"}},
                                 "required": ["url"]}},
                {"name": "browser_click",
                 "inputSchema": {"type": "object",
                                 "properties": {"ref": {"type": "string"}},
                                 "required": ["ref"]}},
                {"name": "browser_snapshot",
                 "inputSchema": {"type": "object",
                                 "properties": {}, "required": []}}]

    def call(self, name, args):
        self.calls.append((name, args))
        return f"did {name}", False

    def snapshot(self):
        return self._states[min(1, len(self._states) - 1)] \
            if self._states else {}

    def close(self):
        pass


def run_fake(task, decide, elements=None, confirm=None, **kw):
    """run_agent с подменённым McpClient."""
    calls: list = []
    base = {"url": "http://localhost/x", "title": "t",
            "main_text": "m", "lead_text": "", "hidden_text": "",
            "elements": elements or []}
    orig = agent_mod.McpClient
    agent_mod.McpClient = lambda **k: FakeClient([base, base], calls)
    events: list[dict] = []
    try:
        rep = agent_mod.run_agent(
            task, llm(decide, **{k: v for k, v in kw.items()
                                 if k == 'verdict'}),
            scenario_for_url("http://localhost/x"),
            on_event=events.append, confirm_fn=confirm)
    finally:
        agent_mod.McpClient = orig
    return rep, calls, events


class TestAgentLoop(unittest.TestCase):
    def test_task_done_completes(self):
        rep, calls, _ = run_fake("x", [
            {"calls": [{"tool": "task_done",
                        "args": {"answer": "ok"}}]}])
        self.assertEqual(rep.status, "done")
        self.assertEqual(rep.answer, "ok")
        self.assertFalse(calls)          # task_done не уходит на сервер

    def test_denied_call_not_executed(self):
        # web-сценарий read_only: click → deny, до сервера не доходит.
        rep, calls, ev = run_fake("x", [
            {"calls": [{"tool": "browser_click",
                        "args": {"ref": "e1"}}]},
            {"calls": [{"tool": "task_done", "args": {"answer": "x"}}]}],
            elements=[{"ref": "e1", "name": "x", "role": "a"}])
        self.assertFalse([c for c in calls if c[0] == "browser_click"])
        self.assertTrue(rep.scope_violations >= 1)

    def test_repeated_calls_stop(self):
        rep, _, _ = run_fake("x", [
            {"calls": [{"tool": "browser_navigate",
                        "args": {"url": "http://localhost/x"}}]}] * 3)
        self.assertIn(rep.status, ("stopped", "limit"))

    def test_bad_model_json_safe(self):
        # ScriptedLLM с мусором вместо calls → пустой план × 2 → stop.
        rep, _, _ = run_fake("x", [{"calls": []}, {"calls": []}])
        self.assertEqual(rep.status, "stopped")

    def test_classify_refuse_no_browser(self):
        calls: list = []
        orig = agent_mod.McpClient
        agent_mod.McpClient = lambda **k: FakeClient([{}], calls)
        try:
            rep = agent_mod.run_agent(
                "x", llm([], verdict="REFUSE"),
                scenario_for_url("http://localhost/x"))
        finally:
            agent_mod.McpClient = orig
        self.assertEqual(rep.status, "rejected")
        self.assertFalse(calls)
class TestMcpProtocol(unittest.TestCase):
    """Ручной прогон протокола — как этап B лабораторной."""
    def test_initialize_list_call(self):
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        try:
            def rpc(msg):
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "t", "version": "0"}}})
            self.assertEqual(r["result"]["serverInfo"]["name"],
                             "browser-agent")
            r = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = [t["name"] for t in r["result"]["tools"]]
            for t in ("browser_navigate", "browser_snapshot",
                      "browser_get_text"):
                self.assertIn(t, names)
            r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "no_such_tool", "arguments": {}}})
            self.assertIn("error", r)
        finally:
            proc.terminate()


class TestScriptedLlm(unittest.TestCase):
    def test_normalize_plan(self):
        p = normalize_plan({"calls": [{"tool": "a"}]}, 8)
        self.assertEqual(len(p["calls"]), 1)
        p = normalize_plan({"tool": "b", "args": {}}, 8)
        self.assertEqual(p["calls"][0]["tool"], "b")


if __name__ == "__main__":
    unittest.main()
