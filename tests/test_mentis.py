#!/usr/bin/env python3
"""
Suite di test di mentis — stdlib `unittest`, zero dipendenze.

Esegui:   python3 -m unittest discover -s tests
   oppure: python3 tests/test_mentis.py

Copre la logica pura (parser, routing, balancer, contratti) e un test di
integrazione del flusso HITL. NON esegue provider reali.
"""
import sys, os, json, time, tempfile, shutil, subprocess, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator"))
import mentis  # noqa: E402


_HOME_BACKUP = {}


def setUpModule():
    """NESSUN test deve toccare il bilancio reale in ~/.mentis: la suite addebita
    davvero (percorsi di fallimento, retry…) e sporcherebbe la quota vera."""
    _HOME_BACKUP["prev"] = os.environ.get("MENTIS_HOME")
    _HOME_BACKUP["dir"] = tempfile.mkdtemp(prefix="mentis-test-home-")
    os.environ["MENTIS_HOME"] = _HOME_BACKUP["dir"]


def tearDownModule():
    if _HOME_BACKUP.get("prev") is None:
        os.environ.pop("MENTIS_HOME", None)
    else:
        os.environ["MENTIS_HOME"] = _HOME_BACKUP["prev"]
    shutil.rmtree(_HOME_BACKUP.get("dir", ""), ignore_errors=True)


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


class _SharedBalanceCase(unittest.TestCase):
    """Il bilancio vive in ~/.mentis: nei test lo si dirotta su una cartella temporanea."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self._prev = os.environ.get("MENTIS_HOME")
        os.environ["MENTIS_HOME"] = str(self.home)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("MENTIS_HOME", None)
        else:
            os.environ["MENTIS_HOME"] = self._prev
        shutil.rmtree(self.home, ignore_errors=True)

    def _set(self, **costs):
        with mentis.open_balance(write=True) as bal:
            for p, c in costs.items():
                bal["providers"][p] = {"cost": c, "window_start": int(time.time())}


class TestBalancer(_SharedBalanceCase):
    def test_static_keeps_order(self):
        cfg = _cfg(); cfg["routing"] = "static"
        self._set(claude=99, codex=0)
        self.assertEqual(mentis.ordered_candidates(["claude", "codex"], cfg), ["claude", "codex"])

    def test_balanced_least_loaded_first(self):
        cfg = _cfg(); cfg["routing"] = "balanced"
        self._set(claude=99, codex=0)
        self.assertEqual(mentis.ordered_candidates(["claude", "codex"], cfg), ["codex", "claude"])

    def test_window_reset_zeroes_cost(self):
        cfg = _cfg()
        with mentis.open_balance(write=True) as bal:
            bal["providers"]["claude"] = {"cost": 50.0, "window_start": 0}   # scaduta (1970)
        self.assertEqual(mentis.current_cost("claude", cfg), 0.0)


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
        box = {"out": "", "writes": False}
        orig = mentis.run_on_provider

        def fake(a, p, prompt, pr, c, d, cwd=None):
            if box["writes"]:                       # l'agente scrive il suo artefatto, come nel reale
                (proj / "business-analysis" / "BAD.md").write_text("# BAD")
            return {"ok": True, "output": box["out"], "provider": p, "_prompt": prompt}

        mentis.run_on_provider = fake
        try:
            u = mentis.Unit("business-analyst", "business-analyst")
            box["out"] = '[[MENTIS-RESULT]]{"status":"needs_input","questions":["Budget?"]}[[/MENTIS-RESULT]]'
            self.assertEqual(mentis.process_unit(ctx, u)["status"], "awaiting_input")
            self.assertTrue(mentis.questions_path(proj, "business-analyst").exists())

            # senza risposte → resta in pausa
            self.assertEqual(mentis.process_unit(ctx, u)["status"], "awaiting_input")

            # con risposte + artefatto scritto dall'agente → done
            ap = mentis.answers_path(proj, "business-analyst")
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text("10k")
            box["out"] = '[[MENTIS-RESULT]]{"status":"done"}[[/MENTIS-RESULT]]'
            box["writes"] = True
            self.assertEqual(mentis.process_unit(ctx, u)["status"], "done")
            # le risposte consumate vengono archiviate: un rilancio non le ri-inietta
            self.assertFalse(ap.exists())
            self.assertTrue(ap.with_suffix(".md.used").exists())
        finally:
            mentis.run_on_provider = orig
            shutil.rmtree(proj)

    def test_artefatto_di_run_precedente_non_vale_come_done(self):
        """Un BAD già presente da un run precedente non deve far passare per 'done'
        un'unità che non ha scritto nulla."""
        proj = Path(tempfile.mkdtemp()); (proj / "business-analysis").mkdir()
        (proj / "business-analysis" / "BAD.md").write_text("# vecchio")
        time.sleep(0.01)
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        ctx = {"project": proj, "cfg": cfg, "state": {"units": {}}, "breaker": {},
               "budget": {"left": 6}, "dry_run": False, "user_input": "app",
               "quality_override": "off"}
        orig = mentis.run_on_provider
        mentis.run_on_provider = lambda *a, **k: {"ok": True, "output": "", "provider": "claude"}
        try:
            u = mentis.Unit("business-analyst", "business-analyst")
            self.assertEqual(mentis.process_unit(ctx, u)["status"], "failed")
        finally:
            mentis.run_on_provider = orig
            shutil.rmtree(proj)


class TestLabelRouting(unittest.TestCase):
    """DevOps label → devops-engineer (rende l'agente raggiungibile)."""

    def test_expand_step_routes_by_label(self):
        d = Path(tempfile.mkdtemp()); (d / "implementation-plans").mkdir()
        (d / "implementation-plans" / "X_DEPS.json").write_text(json.dumps({"issues": [
            {"id": "A", "title": "api", "label": "Backend", "deps": []},
            {"id": "B", "title": "ci", "label": "DevOps", "deps": []}]}))
        by = {u.issue["id"]: u.step for u in mentis.expand_step("developer", d)}
        shutil.rmtree(d)
        self.assertEqual(by["A"], "developer")
        self.assertEqual(by["B"], "devops-engineer")

    def test_agent_for_label(self):
        self.assertEqual(mentis.agent_for_label("DevOps"), "devops-engineer")
        self.assertEqual(mentis.agent_for_label("Frontend"), "developer")
        self.assertEqual(mentis.agent_for_label(None), "developer")


class TestWorktree(unittest.TestCase):
    """Isolamento git worktree per --parallel (git reale)."""

    def _git_repo(self):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "-C", str(d), "init", "-q"])
        (d / "f").write_text("x")
        subprocess.run(["git", "-C", str(d), "add", "-A"])
        subprocess.run(["git", "-C", str(d), "-c", "user.email=x@x", "-c", "user.name=x",
                        "commit", "-q", "-m", "base"])
        return d

    def test_make_and_remove_worktree(self):
        d = self._git_repo()
        wt = mentis.make_worktree(d, "developer::A")
        ok = wt is not None and wt.exists() and (wt / ".git").exists()
        mentis.remove_worktree(d, wt)
        gone = not wt.exists()
        br = subprocess.run(["git", "-C", str(d), "branch"], capture_output=True, text=True).stdout
        shutil.rmtree(d)
        self.assertTrue(ok)                    # worktree creato e isolato
        self.assertTrue(gone)                  # rimosso dopo il cleanup
        self.assertIn("mentis/developer__A", br)  # il branch persiste (deliverable)

    def test_non_git_returns_none(self):
        d = Path(tempfile.mkdtemp())
        self.assertIsNone(mentis.make_worktree(d, "x"))
        shutil.rmtree(d)


class TestRobustness(unittest.TestCase):
    def test_atomic_save_state(self):
        d = Path(tempfile.mkdtemp())
        mentis.save_state(d, {"units": {"a": {"status": "done"}}})
        reloaded = mentis.load_state(d)
        tmp_gone = not (d / ".mentis" / "state.json.tmp").exists()
        shutil.rmtree(d)
        self.assertEqual(reloaded["units"]["a"]["status"], "done")
        self.assertTrue(tmp_gone)                   # nessun file .tmp lasciato indietro

    def test_deps_cap(self):
        d = Path(tempfile.mkdtemp()); (d / "implementation-plans").mkdir()
        (d / "implementation-plans" / "X_DEPS.json").write_text(json.dumps(
            {"issues": [{"id": f"I{i}", "title": "t", "deps": []} for i in range(10)]}))
        old = mentis.MAX_ISSUES
        mentis.MAX_ISSUES = 3
        try:
            iss = mentis.load_issues(d)
        finally:
            mentis.MAX_ISSUES = old
        shutil.rmtree(d)
        self.assertEqual(len(iss), 3)               # troncate al tetto

    def test_strip_comment_handles_escaped_quote(self):
        line = 'k = "a \\" b # non è un commento"'
        self.assertIn("non è un commento", mentis._strip_comment(line))


class TestResumeHash(unittest.TestCase):
    """L'hash di uno step non deve dipendere dal PROPRIO output né dai downstream:
    altrimenti ogni rilancio lo trova 'cambiato' e rifà tutta la pipeline."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        for sub in mentis.ALL_ARTIFACT_DIRS:
            (self.d / sub).mkdir()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_step_non_invalidato_dal_proprio_output(self):
        ba = mentis.load_agent("business-analyst")
        h1 = mentis.input_hash(ba, "x", self.d)
        (self.d / "business-analysis" / "BAD.md").write_text("# BAD")   # output SUO
        self.assertEqual(h1, mentis.input_hash(ba, "x", self.d))

    def test_step_non_invalidato_dai_downstream(self):
        ta = mentis.load_agent("tech-architect")
        h1 = mentis.input_hash(ta, "x", self.d)
        (self.d / "implementation-plans" / "IPD.md").write_text("piano")  # a VALLE
        self.assertEqual(h1, mentis.input_hash(ta, "x", self.d))

    def test_step_invalidato_dagli_upstream(self):
        ta = mentis.load_agent("tech-architect")
        h1 = mentis.input_hash(ta, "x", self.d)
        (self.d / "business-analysis" / "BAD.md").write_text("# BAD")    # a MONTE
        self.assertNotEqual(h1, mentis.input_hash(ta, "x", self.d))


class TestRateLimitAndEnv(unittest.TestCase):
    def test_riconosce_i_messaggi_reali_delle_cli(self):
        for msg in ("You've hit your monthly spend limit",
                    "Claude usage limit reached",
                    "Error 429: too many requests",
                    "You have exceeded your quota"):
            self.assertTrue(mentis.RATE_LIMIT_PATTERNS.search(msg), msg)

    def test_testo_innocuo_non_scambiato_per_limite(self):
        self.assertIsNone(mentis.RATE_LIMIT_PATTERNS.search(
            "Il TAD descrive il retry sugli errori di rete"))

    def test_clean_env_toglie_le_api_key(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        try:
            self.assertNotIn("ANTHROPIC_API_KEY", mentis.clean_env())
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_timeout_piu_alto_per_gli_step_implementanti(self):
        cfg = _cfg()
        self.assertGreater(mentis.timeout_for(cfg, "developer"),
                           mentis.timeout_for(cfg, "business-analyst"))


class TestPreflight(unittest.TestCase):
    def test_segnala_cli_mancante(self):
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["providers"]["claude"]["cmd"] = "non-esiste-questa-cli -p {prompt}"
        self.assertTrue(any("non-esiste-questa-cli" in p for p in mentis.preflight(cfg)))

    def test_segnala_segnaposto_non_supportato(self):
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["providers"]["claude"]["cmd"] = "python3 {prompt} --wd {workdir}"
        self.assertTrue(any("workdir" in p for p in mentis.preflight(cfg)))

    def test_nessun_provider_attivo_nessun_problema(self):
        self.assertEqual(mentis.preflight(_cfg()), [])


class TestReviewerLoopDegrado(unittest.TestCase):
    """Con un solo provider la pipeline non deve fermarsi (policy warn), e senza
    implementer noto il rework non deve crashare."""

    def _run(self, cfg, implementer, output):
        proj = Path(tempfile.mkdtemp())
        orig = mentis.run_on_provider
        mentis.run_on_provider = lambda *a, **k: {"ok": True, "output": output, "provider": "x"}
        try:
            return mentis.reviewer_loop(mentis.load_agent("reviewer"),
                                        mentis.Unit("reviewer", "reviewer"), implementer,
                                        proj, cfg, False, {}, {"units": {}})
        finally:
            mentis.run_on_provider = orig
            shutil.rmtree(proj)

    def test_warn_prosegue_marcando_non_indipendente(self):
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True     # UN solo provider
        cfg["review"]["on_no_alternative"] = "warn"
        res = self._run(cfg, "claude", "tutto ok\nVERDICT: APPROVED")
        self.assertEqual(res["status"], "approved-not-independent")

    def test_block_scala_allutente(self):
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["review"]["on_no_alternative"] = "block"
        self.assertEqual(self._run(cfg, "claude", "VERDICT: APPROVED")["status"], "escalated")

    def test_implementer_sconosciuto_non_crasha(self):
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["providers"]["codex"]["enabled"] = True
        res = self._run(cfg, None, "problemi\nVERDICT: NEEDS WORK")   # prima: KeyError
        self.assertEqual(res["status"], "escalated")
        self.assertEqual(res["reason"], "unknown-implementer")


class TestFallbackOutput(unittest.TestCase):
    def test_il_retry_riuscito_propaga_loutput(self):
        """Sul percorso di retry l'output DEVE arrivare: è lì che vive il contratto
        [[MENTIS-RESULT]] (needs_input/failed dichiarati dall'agente)."""
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["reliability"]["backoff_seconds"] = 0.01
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": False, "rate_limited": False, "returncode": 1}
            return {"ok": True, "output": '[[MENTIS-RESULT]]{"status":"failed"}[[/MENTIS-RESULT]]'}

        proj = Path(tempfile.mkdtemp())
        orig = mentis.run_on_provider
        mentis.run_on_provider = flaky
        try:
            res = mentis.run_unit_with_fallback(
                mentis.load_agent("business-analyst"), mentis.Unit("ba", "business-analyst"),
                "x", proj, cfg, None, False, False, {}, {"left": 6}, {"units": {}})
        finally:
            mentis.run_on_provider = orig
            shutil.rmtree(proj)
        self.assertTrue(res["ok"])
        self.assertEqual(mentis.parse_result(res.get("output", ""))["status"], "failed")

    def test_timeout_non_ritenta(self):
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["providers"]["codex"]["enabled"] = True
        calls = {"n": 0}

        def timing_out(*a, **k):
            calls["n"] += 1
            return {"ok": False, "rate_limited": False, "timeout": True, "error": "timeout"}

        proj = Path(tempfile.mkdtemp())
        orig = mentis.run_on_provider
        mentis.run_on_provider = timing_out
        try:
            res = mentis.run_unit_with_fallback(
                mentis.load_agent("developer"), mentis.Unit("dev", "developer"),
                "x", proj, cfg, None, False, False, {}, {"left": 6}, {"units": {}})
        finally:
            mentis.run_on_provider = orig
            shutil.rmtree(proj)
        self.assertFalse(res["ok"])
        self.assertEqual(calls["n"], 1)              # nessun retry, nessun fallback


class TestMisc(unittest.TestCase):
    def test_verdict_prende_lultimo_tag(self):
        out = ("Devo concludere con `VERDICT: APPROVED` oppure `VERDICT: NEEDS WORK`.\n"
               "Ho trovato 3 problemi.\nVERDICT: NEEDS WORK")
        self.assertEqual(mentis.parse_verdict(out, False)[0], "NEEDS WORK")

    def test_review_mirata_non_fa_fanout(self):
        d = Path(tempfile.mkdtemp()); (d / "implementation-plans").mkdir()
        (d / "implementation-plans" / "X_DEPS.json").write_text(json.dumps({"issues": [
            {"id": "A", "title": "a", "deps": []}, {"id": "B", "title": "b", "deps": []}]}))
        n_build = len(mentis.expand_step("reviewer", d, fanout=True))
        n_review = len(mentis.expand_step("reviewer", d, fanout=False))
        shutil.rmtree(d)
        self.assertEqual((n_build, n_review), (2, 1))

    def test_deps_null_non_crasha(self):
        d = Path(tempfile.mkdtemp()); (d / "implementation-plans").mkdir()
        (d / "implementation-plans" / "X_DEPS.json").write_text(json.dumps(
            {"issues": [{"id": "A", "title": "a", "deps": None},
                        {"id": "B", "title": "b", "deps": "A"}]}))
        iss = mentis.load_issues(d)
        shutil.rmtree(d)
        self.assertEqual(len(iss), 2)
        self.assertEqual(iss[0]["id"], "A")          # B dipende da A → A prima

    def test_gitignore_non_conta_come_lavoro_prodotto(self):
        d = Path(tempfile.mkdtemp())
        subprocess.run(["git", "-C", str(d), "init", "-q"])
        (d / "f").write_text("x")
        subprocess.run(["git", "-C", str(d), "add", "-A"])
        subprocess.run(["git", "-C", str(d)] + mentis.GIT_ID + ["commit", "-q", "-m", "base"])
        mentis.ensure_gitignore(d)                   # è mentis stesso a sporcare il tree
        dirty = mentis.git_dirty_set(d)
        shutil.rmtree(d)
        self.assertEqual(dirty, set())

    def test_charge_non_addebita_in_dry_run(self):
        d = Path(tempfile.mkdtemp())
        st = {"units": {}}
        mentis.charge(st, "claude", "implement", _cfg(), d, dry_run=True)
        shutil.rmtree(d)
        self.assertEqual(st.get("budget", {}), {})

    def test_branch_per_issue(self):
        u = mentis.Unit("developer::LIN-42", "developer",
                        {"id": "LIN-42", "title": "User Auth API", "deps": []})
        self.assertEqual(mentis.branch_for(u), "feat/lin-42-user-auth-api")


class TestSpesaEContratto(unittest.TestCase):
    def test_tetto_di_chiamate_ferma_il_run(self):
        """Il tetto per-run è la protezione contro il bruciare la quota in una build."""
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["providers"]["claude"]["cmd"] = "/usr/bin/true {prompt}"
        cfg["reliability"]["max_calls_per_run"] = 2
        proj = Path(tempfile.mkdtemp())
        agent = mentis.load_agent("business-analyst")
        old = mentis._CALLS_THIS_RUN
        mentis._CALLS_THIS_RUN = 0
        try:
            oks = [mentis.run_on_provider(agent, "claude", "p", proj, cfg, False).get("ok")
                   for _ in range(3)]
        finally:
            mentis._CALLS_THIS_RUN = old
            shutil.rmtree(proj)
        self.assertEqual(oks, [True, True, False])   # la terza è oltre il tetto

    def test_lesempio_del_contratto_non_e_un_esito(self):
        """Se una CLI ri-echeggia il prompt, l'ESEMPIO del contratto è JSON valido:
        non deve essere scambiato per il risultato dell'agente."""
        echo = ('[[MENTIS-RESULT]]{"status": "done|needs_input|failed", "artifacts": [], '
                '"questions": [], "note": "una riga"}[[/MENTIS-RESULT]]')
        self.assertIsNone(mentis.parse_result(echo))
        real = echo + '\nlavoro svolto\n[[MENTIS-RESULT]]{"status":"failed"}[[/MENTIS-RESULT]]'
        self.assertEqual(mentis.parse_result(real)["status"], "failed")

    def test_handoff_scritta_nel_worktree_torna_nel_progetto(self):
        proj = Path(tempfile.mkdtemp()); wt = Path(tempfile.mkdtemp())
        src = mentis.handoff_path(proj, "developer::A", wt)
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("Fatto: metà lavoro")
        mentis.sync_handoff(proj, "developer::A", wt)
        durable = mentis.handoff_path(proj, "developer::A")
        ok = durable.exists() and durable.read_text() == "Fatto: metà lavoro"
        shutil.rmtree(proj); shutil.rmtree(wt)
        self.assertTrue(ok)

    def test_risposte_iniettate_anche_se_lunita_era_fallita(self):
        """Se un tentativo fallisce dopo l'iniezione lo stato non torna awaiting_input:
        legandosi a quello, le risposte dell'utente resterebbero orfane."""
        proj = Path(tempfile.mkdtemp()); (proj / "business-analysis").mkdir()
        ap = mentis.answers_path(proj, "business-analyst")
        ap.parent.mkdir(parents=True, exist_ok=True)
        ap.write_text("budget 10k")
        cfg = _cfg(); cfg["providers"]["claude"]["enabled"] = True
        ctx = {"project": proj, "cfg": cfg, "breaker": {}, "budget": {"left": 6},
               "state": {"units": {"business-analyst": {"status": "failed"}}},
               "dry_run": False, "user_input": "app", "quality_override": "off"}
        seen = {}
        orig = mentis.run_on_provider

        def fake(a, p, prompt, pr, c, d, cwd=None):
            seen["prompt"] = prompt
            (proj / "business-analysis" / "BAD.md").write_text("# BAD")
            return {"ok": True, "output": '[[MENTIS-RESULT]]{"status":"done"}[[/MENTIS-RESULT]]'}

        mentis.run_on_provider = fake
        try:
            mentis.process_unit(ctx, mentis.Unit("business-analyst", "business-analyst"))
        finally:
            mentis.run_on_provider = orig
            shutil.rmtree(proj)
        self.assertIn("budget 10k", seen["prompt"])


class TestContabilitaDelConsumo(_SharedBalanceCase):
    """Il balancer deve stimare il CONSUMO, non contare le operazioni."""

    def setUp(self):
        super().setUp()
        self.d = Path(tempfile.mkdtemp())
        self.cfg = _cfg()

    def tearDown(self):
        shutil.rmtree(self.d)
        super().tearDown()

    def _cost(self, kind, tier):
        st = {"units": {}}
        mentis.charge(st, "claude", kind, self.cfg, self.d, False, tier)
        return st["budget"]["claude"]["cost"]

    def test_una_chiamata_frontier_pesa_piu_di_una_balanced(self):
        frontier, balanced = self._cost("implement", "frontier"), self._cost("implement", "balanced")
        self.assertGreater(frontier, balanced)
        self.assertAlmostEqual(frontier / balanced,
                               mentis.tier_multiplier(self.cfg, "frontier"), places=3)

    def test_il_reviewer_non_e_piu_contato_come_una_critica_breve(self):
        # misurato: il prompt del reviewer è il più grosso della pipeline
        self.assertEqual(mentis.role_weight_kind("reviewer"), "review")
        w = self.cfg["balance"]["weights"]
        self.assertGreater(w["review"], w["evaluate"])

    def test_tier_sconosciuto_non_falsa_il_conto(self):
        self.assertEqual(mentis.tier_multiplier(self.cfg, None), 1.0)
        self.assertEqual(mentis.tier_multiplier(self.cfg, "inventato"), 1.0)

    def _run_with(self, result):
        """Esegue un'unità con un provider che restituisce `result`, e ritorna il costo."""
        cfg = _cfg()
        cfg["providers"]["claude"]["enabled"] = True
        cfg["reliability"]["backoff_seconds"] = 0        # niente retry: un tentativo solo
        state = {"units": {}}
        orig = mentis.run_on_provider
        mentis.run_on_provider = lambda *a, **k: dict(result)
        try:
            mentis.run_unit_with_fallback(
                mentis.load_agent("developer"), mentis.Unit("developer", "developer"),
                "x", self.d, cfg, None, False, False, {}, {"left": 6}, state)
        finally:
            mentis.run_on_provider = orig
        return state.get("budget", {}).get("claude", {}).get("cost", 0.0)

    def test_un_tentativo_fallito_ha_comunque_consumato_quota(self):
        self.assertGreater(self._run_with({"ok": False, "returncode": 1}), 0)

    def test_un_timeout_si_paga(self):
        # 90 minuti bruciati: il provider NON deve risultare scarico dopo
        self.assertGreater(self._run_with({"ok": False, "timeout": True}), 0)

    def test_un_rate_limit_non_si_paga(self):
        # la richiesta è respinta alla porta: non ha consumato nulla
        self.assertEqual(self._run_with({"ok": False, "rate_limited": True}), 0.0)


class TestQuotaCondivisa(_SharedBalanceCase):
    """La quota è dell'ACCOUNT: due progetti diversi devono vedere lo stesso conto."""

    def test_due_progetti_condividono_il_consumo(self):
        cfg = _cfg()
        a, b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        try:
            mentis.charge({"units": {}}, "claude", "implement", cfg, a, False, "balanced")
            mentis.charge({"units": {}}, "claude", "implement", cfg, b, False, "balanced")
            # il secondo progetto NON riparte da zero
            self.assertAlmostEqual(mentis.current_cost("claude", cfg), 2.0, places=3)
        finally:
            shutil.rmtree(a); shutil.rmtree(b)

    def test_un_rate_limit_reale_vale_piu_della_stima(self):
        cfg = _cfg()
        self._set(claude=0.0, codex=50.0)
        # senza segnale esterno claude è il più scarico e verrebbe scelto
        self.assertEqual(mentis.ordered_candidates(["claude", "codex"], cfg)[0], "claude")
        mentis.mark_exhausted("claude", cfg)         # ma la CLI ci ha appena detto: quota finita
        self.assertEqual(mentis.ordered_candidates(["claude", "codex"], cfg)[0], "codex")
        self.assertIn("claude", mentis.exhausted_providers(cfg))

    def test_lesaurimento_scade_con_la_finestra(self):
        cfg = _cfg()
        mentis.mark_exhausted("claude", cfg)
        with mentis.open_balance(write=True) as bal:   # finge che sia passata la ricarica
            bal["providers"]["claude"]["exhausted_at"] = 0
        self.assertNotIn("claude", mentis.exhausted_providers(cfg))

    def test_migrazione_dal_vecchio_contatore_di_progetto(self):
        cfg = _cfg()
        proj = Path(tempfile.mkdtemp())
        state = {"units": {}, "budget": {"claude": {"cost": 7.0, "window_start": int(time.time())}}}
        try:
            mentis.migrate_project_budget(proj, state)
            self.assertAlmostEqual(mentis.current_cost("claude", cfg), 7.0, places=3)
            mentis.migrate_project_budget(proj, state)      # idempotente: non raddoppia
            self.assertAlmostEqual(mentis.current_cost("claude", cfg), 7.0, places=3)
        finally:
            shutil.rmtree(proj)


class TestTokenReali(_SharedBalanceCase):
    """Con `usage_json` le CLI danno il conteggio token vero: da lì nasce una
    percentuale di quota che ha un senso."""

    def test_envelope_claude(self):
        out = json.dumps({"result": "ciao", "usage": {"input_tokens": 100, "output_tokens": 40}})
        self.assertEqual(mentis.parse_usage_envelope(out), ("ciao", 140))

    def test_envelope_codex_jsonl(self):
        lines = [json.dumps({"payload": {"type": "item", "text": "ciao"}}),
                 json.dumps({"payload": {"type": "token_count", "input_tokens": 90,
                                         "output_tokens": 30, "reasoning_tokens": 10}})]
        text, tok = mentis.parse_usage_envelope("\n".join(lines))
        self.assertEqual(tok, 130)
        self.assertIn("ciao", text)

    def test_output_non_json_non_rompe_nulla(self):
        self.assertEqual(mentis.parse_usage_envelope("solo testo\nVERDICT: APPROVED"), (None, 0))
        self.assertEqual(mentis.parse_usage_envelope(""), (None, 0))

    def test_la_percentuale_usa_i_token_quando_ci_sono(self):
        cfg = _cfg()
        d = Path(tempfile.mkdtemp())
        try:
            mentis.charge({"units": {}}, "claude", "implement", cfg, d, False, "balanced", tokens=5000)
            self.assertIsNone(mentis.quota_used_pct("claude", cfg))   # tetto ancora ignoto
            mentis.mark_exhausted("claude", cfg)                      # ora il tetto è 5000
            pct, unit = mentis.quota_used_pct("claude", cfg)
            self.assertEqual(unit, "token")
            self.assertAlmostEqual(pct, 100.0, places=1)
        finally:
            shutil.rmtree(d)

    def test_reset_non_fa_disimparare_il_tetto(self):
        cfg = _cfg()
        d = Path(tempfile.mkdtemp())
        try:
            mentis.charge({"units": {}}, "claude", "implement", cfg, d, False, "balanced", tokens=5000)
            mentis.mark_exhausted("claude", cfg)
            mentis.cmd_balance(cfg, reset=True, provider=None, add=None)
            with mentis.open_balance(write=False) as bal:
                b = bal["providers"]["claude"]
            self.assertEqual(b["cost"], 0.0)              # il consumo riparte
            self.assertEqual(b["limit_tokens"], 5000)     # la calibrazione no
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
