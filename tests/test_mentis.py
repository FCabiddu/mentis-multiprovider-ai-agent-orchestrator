#!/usr/bin/env python3
"""
Suite di test di mentis — stdlib `unittest`, zero dipendenze.

Esegui:   python3 -m unittest discover -s tests
   oppure: python3 tests/test_mentis.py

Copre la logica pura (parser, routing, balancer, contratti) e un test di
integrazione del flusso HITL. NON esegue provider reali.
"""
import sys, os, json, time, tempfile, shutil, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))
import mentis  # noqa: E402


def _cfg():
    return mentis.load_toml(mentis.CONFIG_PATH)


class TestToml(unittest.TestCase):
    def test_floats_ints_bools(self):
        cfg = _cfg()
        self.assertEqual(cfg["balance"]["weights"]["implement"], 1.0)
        self.assertIsInstance(cfg["balance"]["weights"]["implement"], float)
        self.assertEqual(cfg["reliability"]["retry_budget"], 6)
        self.assertTrue(cfg["review"]["cross_provider"])

    def test_multiline_array(self):
        cfg = _cfg()
        self.assertIn("business-analyst", cfg["pipeline"]["build"]["steps"])
        self.assertEqual(len(cfg["pipeline"]["build"]["steps"]), 7)

    def test_inline_comment_stripped(self):
        cfg = _cfg()
        self.assertEqual(cfg["model_map"]["codex"]["frontier"], "gpt-5.6-sol")


class TestLoadIssues(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); (self.d / "implementation-plans").mkdir()

    def tearDown(self):
        shutil.rmtree(self.d)

    def _write(self, obj):
        (self.d / "implementation-plans" / "X_DEPS.json").write_text(json.dumps(obj))

    def test_canonical_schema_with_label(self):
        self._write({"issues": [
            {"id": "A", "title": "Auth", "label": "Backend", "deps": []},
            {"id": "B", "title": "UI", "label": "Frontend", "deps": ["A"]}]})
        iss = mentis.load_issues(self.d)
        self.assertEqual([i["id"] for i in iss], ["A", "B"])   # toposort: A prima di B
        self.assertEqual(iss[1]["label"], "Frontend")

    def test_legacy_issuemap_converted_no_crash(self):
        # il formato che PRIMA crashava (issueMap + dependencies)
        self._write({"project": "app", "issueMap": {"A": {"title": "Auth"}},
                     "dependencies": [{"blockedPlanId": "A", "blockerPlanId": "A"}]})
        iss = mentis.load_issues(self.d)
        self.assertEqual(iss[0]["id"], "A")

    def test_garbage_returns_none_no_crash(self):
        self._write({"garbage": True})
        self.assertIsNone(mentis.load_issues(self.d))

    def test_string_values_do_not_crash(self):
        # il crash originale: .get() su una stringa
        self._write({"project": "app", "issues": "not a list"})
        self.assertIsNone(mentis.load_issues(self.d))


class TestReasoningAndModel(unittest.TestCase):
    def test_resolve_tier_and_reasoning(self):
        cfg = _cfg()
        ta = mentis.load_agent("tech-architect")             # frontier / high
        model, rv, level = mentis.resolve(ta, "codex", cfg)
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertEqual(level, "high")

    def test_clamp_reasoning(self):
        self.assertEqual(mentis.clamp_reasoning("max", {"low": 1, "high": 3}), "high")


class TestBalancer(unittest.TestCase):
    def test_static_keeps_order(self):
        cfg = _cfg(); cfg["routing"] = "static"
        st = {"budget": {"claude": {"cost": 99, "window_start": 9e9},
                         "codex": {"cost": 0, "window_start": 9e9}}}
        self.assertEqual(mentis.ordered_candidates(["claude", "codex"], cfg, st), ["claude", "codex"])

    def test_balanced_least_loaded_first(self):
        cfg = _cfg(); cfg["routing"] = "balanced"
        st = {"budget": {"claude": {"cost": 99, "window_start": 9e9},
                         "codex": {"cost": 0, "window_start": 9e9}}}
        self.assertEqual(mentis.ordered_candidates(["claude", "codex"], cfg, st), ["codex", "claude"])

    def test_window_reset_zeroes_cost(self):
        cfg = _cfg()
        st = {"budget": {"claude": {"cost": 50.0, "window_start": 0}}}  # finestra scaduta (1970)
        self.assertEqual(mentis.current_cost(st, "claude", cfg), 0.0)


class TestVerdictAndResult(unittest.TestCase):
    def test_verdict_tag(self):
        self.assertEqual(mentis.parse_verdict("x\nVERDICT: APPROVED", False)[0], "APPROVED")
        self.assertEqual(mentis.parse_verdict("x\nVERDICT: NEEDS WORK", False)[0], "NEEDS WORK")

    def test_verdict_ambiguous_is_conservative(self):
        self.assertEqual(mentis.parse_verdict("nessun verdetto qui", False)[0], "NEEDS WORK")

    def test_parse_result(self):
        out = '[[MENTIS-RESULT]]{"status":"needs_input","questions":["Q"]}[[/MENTIS-RESULT]]'
        self.assertEqual(mentis.parse_result(out)["status"], "needs_input")
        self.assertIsNone(mentis.parse_result("niente"))


class TestGraphAndCommand(unittest.TestCase):
    def test_toposort_orders_and_survives_cycle(self):
        issues = {"A": {"id": "A", "deps": ["B"]}, "B": {"id": "B", "deps": ["A"]}}  # ciclo
        out = mentis._toposort(issues, ["A", "B"])
        self.assertEqual(len(out), 2)                        # non va in loop infinito

    def test_build_command_no_shell_injection(self):
        argv = mentis.build_command('codex exec {prompt} --model {model}',
                                    'rm -rf / ; echo "hack"', "m", "high")
        self.assertIn('rm -rf / ; echo "hack"', argv)        # il prompt resta UN singolo argv


class TestWavesAndHash(unittest.TestCase):
    def test_build_waves_groups_independent(self):
        cfg = _cfg()
        units = [mentis.Unit("developer::A", "developer", {"id": "A", "deps": []}),
                 mentis.Unit("developer::B", "developer", {"id": "B", "deps": []}),
                 mentis.Unit("developer::C", "developer", {"id": "C", "deps": ["A", "B"]})]
        waves = mentis.build_waves(units, cfg, parallel=True)
        self.assertEqual([len(w) for w in waves], [2, 1])    # A,B insieme; C dopo

    def test_upstream_hash_changes(self):
        d = Path(tempfile.mkdtemp()); (d / "tech-analysis").mkdir()
        (d / "tech-analysis" / "TAD.md").write_text("v1")
        ag = mentis.load_agent("developer")
        h1 = mentis.input_hash(ag, "x", d)
        (d / "tech-analysis" / "TAD.md").write_text("v2")
        h2 = mentis.input_hash(ag, "x", d)
        shutil.rmtree(d)
        self.assertNotEqual(h1, h2)


class TestHITLIntegration(unittest.TestCase):
    """needs_input → pausa → risposte → done, con run_on_provider finto."""

    def test_hitl_flow(self):
        proj = Path(tempfile.mkdtemp()); (proj / "business-analysis").mkdir()
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["providers"]["codex"]["enabled"] = True
        ctx = {"project": proj, "cfg": cfg, "state": {"units": {}}, "breaker": {},
               "budget": {"left": 6}, "dry_run": False, "user_input": "app",
               "quality_override": "off"}
        box = {"out": ""}
        orig = mentis.run_on_provider
        mentis.run_on_provider = lambda a, p, prompt, pr, c, d: {
            "ok": True, "output": box["out"], "provider": p, "_prompt": prompt}
        try:
            u = mentis.Unit("business-analyst", "business-analyst")
            box["out"] = '[[MENTIS-RESULT]]{"status":"needs_input","questions":["Budget?"]}[[/MENTIS-RESULT]]'
            self.assertEqual(mentis.process_unit(ctx, u)["status"], "awaiting_input")
            self.assertTrue(mentis.questions_path(proj, "business-analyst").exists())

            # senza risposte → resta in pausa
            self.assertEqual(mentis.process_unit(ctx, u)["status"], "awaiting_input")

            # con risposte + artefatto → done
            ap = mentis.answers_path(proj, "business-analyst")
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text("10k")
            (proj / "business-analysis" / "BAD.md").write_text("# BAD")
            box["out"] = '[[MENTIS-RESULT]]{"status":"done"}[[/MENTIS-RESULT]]'
            self.assertEqual(mentis.process_unit(ctx, u)["status"], "done")
        finally:
            mentis.run_on_provider = orig
            shutil.rmtree(proj)


if __name__ == "__main__":
    unittest.main(verbosity=2)
