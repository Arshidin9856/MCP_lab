"""test_inspect.py — узкие тесты диагностики tools/inspect_tools.py.

McpClient подменяется фикстурой — браузер, LLM и сеть не нужны;
состояние сценариев/политики инъецируется через mock.patch.dict.
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "tools"))

import inspect_tools                                      # noqa: E402
import scenarios as scenarios_mod                         # noqa: E402
import student_policy                                     # noqa: E402
from scenarios import Scenario                            # noqa: E402


class FakeClient:
    """Подмена McpClient: фиксированный каталог без subprocess."""
    def __init__(self, **kw):
        pass

    def list_tools(self):
        return [{"name": "browser_navigate",
                 "annotations": {"readOnlyHint": True},
                 "description": "nav", "inputSchema": {}},
                {"name": "browser_snapshot",
                 "annotations": {"readOnlyHint": True},
                 "description": "snap", "inputSchema": {}},
                {"name": "browser_click",
                 "annotations": {"readOnlyHint": False},
                 "description": "click", "inputSchema": {}},
                {"name": "browser_type",
                 "annotations": {"readOnlyHint": False},
                 "description": "type", "inputSchema": {}}]

    def close(self):
        pass


def run_main(argv):
    out = io.StringIO()
    with mock.patch.object(inspect_tools, "McpClient", FakeClient):
        with mock.patch.object(sys, "argv", ["inspect_tools.py"] + argv):
            with redirect_stdout(out):
                code = inspect_tools.main()
    return code, out.getvalue()


def line_for(out, tool):
    return next(ln for ln in out.splitlines() if ln.startswith(tool))


class TestInspectTools(unittest.TestCase):
    def test_web_empty_policies_read_only(self):
        # Пустые реестры (гарантированно): web = read_only — чтение
        # allow, click deny (а не «всё deny», как печатала старая
        # таблица).
        with mock.patch.dict(scenarios_mod.SCENARIOS, {}, clear=True):
            with mock.patch.dict(student_policy.POLICIES, {}, clear=True):
                code, out = run_main(["--variant", "web"])
        self.assertEqual(code, 0)
        self.assertIn("deny", line_for(out, "browser_click"))
        self.assertIn("allow", line_for(out, "browser_navigate"))
        self.assertIn("allow", line_for(out, "browser_snapshot"))

    def test_unknown_variant_clear_error(self):
        with self.assertRaises(ValueError):
            run_main(["--variant", "no_such_variant"])

    def test_named_scenario_policy_table(self):
        sc = Scenario(name="mysite", url="https://x.example",
                      domains=["x.example"], example_task="",
                      read_only=False)
        with mock.patch.dict(scenarios_mod.SCENARIOS, {"mysite": sc}):
            with mock.patch.dict(
                    student_policy.POLICIES,
                    {"mysite": {"browser_navigate": "allow",
                                "browser_click": "confirm"}}):
                code, out = run_main(["--variant", "mysite"])
        self.assertEqual(code, 0)
        self.assertIn("allow", line_for(out, "browser_navigate"))
        self.assertIn("confirm", line_for(out, "browser_click"))
        self.assertIn("deny", line_for(out, "browser_type"))


if __name__ == "__main__":
    unittest.main()
