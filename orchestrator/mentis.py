#!/usr/bin/env python3
"""
mentis — orchestratore agentico agnostico rispetto al provider LLM.

Non ha cervello né tool propri: è un REGISTA. Prende gli agenti neutri
(../agents/*.md, con tier + reasoning), li traduce nel modello e nei flag del
provider scelto (via ../config/mentis.toml) e li lancia come CLI headless
(Claude Code, Codex, ...). Ogni CLI porta i propri tool; il passaggio di
consegne tra agenti avviene tramite file su disco nel progetto.

Funzioni chiave:
  • fallback     — prova i provider in ordine; al primo rate-limit passa oltre
  • compare      — stesso task su tutti i provider, output affiancati (challenge)
  • review incrociata — il reviewer gira SEMPRE su un provider ≠ implementer
  • resumability — stato per-unità in .mentis/state.json, salvato dopo ogni unità;
                   al rilancio salta le unità già 'done' e riprende dall'interrotta
  • handoff      — ogni unità mantiene una nota di progresso su disco: chi subentra
                   (fallback o resume) riparte da lì senza perdere il contesto durevole
  • fan-out per-issue — 'developer' si espande in un'unità per issue (da un *_DEPS.json)

Uso:
    mentis.py <comando> <descrizione...>  [--project DIR] [--compare] [--dry-run]

Esempi:
    mentis.py build  "app TODO con login"
    mentis.py tad    "microservizio pagamenti"       --compare
    mentis.py review "PR #12"                          --project ~/dev/foo

Senza provider attivi (nessun abbonamento loggato) parte in --dry-run: stampa
il PIANO — chi farebbe cosa, su quale provider/modello, con quale comando —
senza eseguire nulla. Utile per capire il comportamento prima di attivare le CLI.

Dipendenze: nessuna. Solo stdlib. Python ≥ 3.7.
"""
from __future__ import annotations
import sys, os, re, json, shlex, subprocess, argparse, time, hashlib, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_CALL_SEQ = 0                 # sequenza globale per nomi-file transcript univoci
_STATE_LOCK = threading.Lock()  # protegge state.json in esecuzione parallela

ROOT = Path(__file__).resolve().parent.parent          # .../mentis
AGENTS_DIR = ROOT / "agents"
CONFIG_PATH = ROOT / "config" / "mentis.toml"

# Step che PRODUCONO codice: il reviewer deve girare su un provider diverso da
# quello che ha eseguito l'ultimo di questi.
IMPLEMENTING_STEPS = {"developer", "devops-engineer", "qa-engineer", "mvp-builder"}

# Ladder ordinata per il clamping del reasoning quando un provider non ha un
# gradino esatto.
REASONING_LADDER = ["none", "low", "medium", "high", "max"]

# Pattern che indicano esaurimento/limite → trigger di fallback.
RATE_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|quota|usage limit|too many requests|\b429\b|overloaded|"
    r"insufficient.?quota|resource.?exhausted",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
#  Mini-parser TOML (sottoinsieme sufficiente al nostro config, zero deps)
# --------------------------------------------------------------------------- #
def load_toml(path: Path) -> dict:
    data: dict = {}
    cur = data
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = _strip_comment(raw).strip()
        i += 1
        if not line:
            continue
        if line.startswith("["):                       # header di sezione
            name = line.strip("[]").strip()
            cur = data
            for part in name.split("."):
                cur = cur.setdefault(part, {})
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        # array eventualmente multilinea: accumula finché le [] bilanciano
        if val.startswith("[") and val.count("[") > val.count("]"):
            while val.count("[") > val.count("]") and i < len(lines):
                val += " " + _strip_comment(lines[i]).strip()
                i += 1
        cur[key] = _parse_value(val)
    return data


def _strip_comment(line: str) -> str:
    """Rimuove un commento '#' che non sia dentro una stringa."""
    out, in_str = [], False
    for ch in line:
        if ch == '"':
            in_str = not in_str
        if ch == "#" and not in_str:
            break
        out.append(ch)
    return "".join(out)


def _parse_value(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(x) for x in _split_array(inner)]
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        return v


def _split_array(inner: str):
    items, buf, in_str = [], [], False
    for ch in inner:
        if ch == '"':
            in_str = not in_str
        if ch == "," and not in_str:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        items.append("".join(buf).strip())
    return items


# --------------------------------------------------------------------------- #
#  Agenti
# --------------------------------------------------------------------------- #
class Agent:
    def __init__(self, name, description, tier, reasoning, body):
        self.name = name
        self.description = description
        self.tier = tier
        self.reasoning = reasoning
        self.body = body


def load_agent(name: str) -> Agent:
    path = AGENTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"agente non trovato: {path}")
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        raise ValueError(f"frontmatter mancante in {path}")
    fm, body = m.group(1), m.group(2)

    def field(key, default=None):
        mm = re.search(rf"^{key}:\s*(.*?)\s*(?:#.*)?$", fm, re.MULTILINE)
        return mm.group(1).strip() if mm else default

    return Agent(
        name=name,
        description=field("description", ""),
        tier=field("tier", "balanced"),
        reasoning=field("reasoning", "medium"),
        body=body,
    )


# --------------------------------------------------------------------------- #
#  Risoluzione provider → modello + reasoning
# --------------------------------------------------------------------------- #
def clamp_reasoning(level: str, provider_map: dict) -> str:
    """Se il provider non ha esattamente `level`, scende al gradino più vicino."""
    if level in provider_map:
        return level
    idx = REASONING_LADDER.index(level) if level in REASONING_LADDER else 2
    for step in range(len(REASONING_LADDER)):
        for cand in (idx - step, idx + step):
            if 0 <= cand < len(REASONING_LADDER):
                cand_level = REASONING_LADDER[cand]
                if cand_level in provider_map:
                    return cand_level
    return level


def resolve(agent: Agent, provider: str, cfg: dict):
    model = cfg["model_map"][provider][agent.tier]
    rmap = cfg["reasoning_map"][provider]
    level = clamp_reasoning(agent.reasoning, rmap)
    reasoning_value = rmap[level]
    return model, str(reasoning_value), level


def build_command(cmd_template: str, prompt: str, model: str, reasoning: str):
    """
    Costruisce l'argv per subprocess SENZA passare dalla shell: il prompt (che
    può contenere virgolette/backtick/newline) entra come singolo elemento,
    così non serve alcun escaping.
    """
    tokens = shlex.split(cmd_template)
    out = []
    for t in tokens:
        if t == "{prompt}":
            out.append(prompt)
        elif "{" in t:
            out.append(t.format(prompt=prompt, model=model, reasoning=reasoning))
        else:
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
#  Stato / checkpoint — resumability + provenienza per la review incrociata
#  (unico store: .mentis/state.json, salvato DOPO OGNI unità)
# --------------------------------------------------------------------------- #
# Artefatto atteso per step: usato per VERIFICARE l'output e per considerare
# un'unità davvero "done" al resume. None = nessun singolo file deterministico.
EXPECTED_ARTIFACT = {
    "business-analyst":       "business-analysis/*.md",
    "tech-architect":         "tech-analysis/*.md",
    "implementation-planner": "implementation-plans/*.md",
}

# Istruzione iniettata nel prompt: l'agente tiene una nota di progresso su disco,
# aggiornata in continuo, così un altro provider può subentrare senza perdere il
# contesto durevole (l'effimero — il ragionamento vivo — non è trasferibile).
HANDOFF_INSTRUCTION = (
    "\n\n---\n[mentis handoff] Mentre lavori tieni aggiornata una nota di "
    "progresso nel file `{handoff}` con tre sezioni: `Fatto:`, `Da fare:`, "
    "`Decisioni:`. Aggiornala DOPO OGNI passo significativo (ogni file, ogni "
    "sotto-task), non solo alla fine: se vieni interrotto un altro modello "
    "riprenderà da quella nota. Sii conciso e sincero sullo stato reale."
)

# Contratto di neutralità iniettato in OGNI chiamata: rende i corpi degli agenti
# (scritti per Claude Code) eseguibili su qualsiasi provider, senza forkarli.
NEUTRALITY_PREAMBLE = (
    "[mentis — contratto di esecuzione] Giri sotto un orchestratore agnostico "
    "rispetto al provider, tramite una CLI generica. Regole che sovrascrivono "
    "eventuali istruzioni contrarie nel testo sotto:\n"
    "• NON invocare tool specifici di Claude Code (es. un tool 'Agent' o "
    "'subagent'): NON puoi spawnare altri agenti. Se il testo ti chiede di "
    "spawnare un agente, invece elenca nel tuo output finale COSA andrebbe "
    "dispacciato (agente + argomenti) — ci penserà l'orchestratore.\n"
    "• NON usare tool Linear/MCP o servizi esterni: persisti TUTTO come file "
    "LOCALI sotto `tasks/` e `implementation-plans/` (incluso, per i piani, un "
    "`*_DEPS.json` con id/titolo/dipendenze delle issue).\n"
    "• Usa i normali strumenti di lettura/scrittura file ed esecuzione comandi "
    "del tuo runtime, comunque si chiamino.\n"
    "• Nei commit firma come `mentis`, non con il nome di un modello specifico.\n"
    "---\n"
)


def state_path(project: Path) -> Path:
    return project / ".mentis" / "state.json"


def load_state(project: Path) -> dict:
    p = state_path(project)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"units": {}}


def save_state(project: Path, state: dict):
    p = state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


def mark_unit(project: Path, state: dict, uid: str, **fields):
    """Aggiorna un'unità e persiste SUBITO (checkpoint incrementale, thread-safe)."""
    with _STATE_LOCK:
        u = state["units"].setdefault(uid, {})
        u.update(fields)
        u["ts"] = int(time.time())
        save_state(project, state)


def log_call(project: Path, entry: dict, prompt: str = "", output: str = ""):
    """Registra una chiamata a un provider: indice JSONL + transcript completo su file."""
    global _CALL_SEQ
    with _STATE_LOCK:
        _CALL_SEQ += 1
        seq = _CALL_SEQ
    d = project / ".mentis" / "logs"
    (d / "transcripts").mkdir(parents=True, exist_ok=True)
    entry = {"seq": seq, **entry}
    with open(d / "calls.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if prompt or output:
        fn = d / "transcripts" / f"{seq:05d}-{entry.get('provider')}-{entry.get('agent')}.txt"
        fn.write_text(f"=== PROMPT ===\n{prompt}\n\n=== OUTPUT ===\n{output}")


def input_hash(agent: "Agent", unit_input: str) -> str:
    h = hashlib.sha1()
    h.update(agent.body.encode()); h.update(b"\0"); h.update(unit_input.encode())
    return h.hexdigest()[:12]


def handoff_path(project: Path, uid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", uid)
    return project / ".mentis" / "handoff" / f"{safe}.md"


def artifact_ok(project: Path, step: str, dry_run: bool) -> bool:
    """Check LENIENTE per il resume (basta l'artefatto atteso, se previsto)."""
    glob = EXPECTED_ARTIFACT.get(step)
    if not glob or dry_run:
        return True
    return any(project.glob(glob))


def git_head(project: Path):
    try:
        r = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def git_dirty(project: Path) -> bool:
    try:
        r = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def produced_output(project: Path, unit, dry_run: bool, pre_head, started_at) -> bool:
    """Check STRETTO post-esecuzione: l'unità ha davvero prodotto qualcosa?"""
    if dry_run:
        return True
    glob = EXPECTED_ARTIFACT.get(unit.step)
    if glob:
        return any(project.glob(glob))
    if unit.step in IMPLEMENTING_STEPS:                 # developer/qa/devops/mvp
        head = git_head(project)
        if head is not None:                           # repo git: HEAD avanzato o tree sporco
            return head != pre_head or git_dirty(project)
        for p in project.rglob("*"):                   # non-git: qualche file toccato dopo l'avvio
            try:
                if p.is_file() and p.stat().st_mtime >= started_at:
                    return True
            except Exception:
                pass
        return False
    return True                                        # step senza artefatto verificabile (reviewer)


# --------------------------------------------------------------------------- #
#  Fan-out per-issue (Orchestrator-Worker) — checkpoint a grana fine
# --------------------------------------------------------------------------- #
class Unit:
    def __init__(self, uid: str, step: str, issue: dict | None = None):
        self.id = uid
        self.step = step
        self.issue = issue


def load_issues(project: Path):
    """Issue + dipendenze da implementation-plans/*_DEPS.json (o None se assente)."""
    files = sorted(project.glob("implementation-plans/*_DEPS.json"))
    if not files:
        return None
    try:
        data = json.loads(files[0].read_text())
    except Exception:
        return None
    raw = data.get("issues", data) if isinstance(data, dict) else data
    issues, order = {}, []
    if isinstance(raw, dict):
        items = raw.items()
    else:
        items = [(it.get("id"), it) for it in raw if isinstance(it, dict)]
    for iid, v in items:
        if not iid:
            continue
        v = v or {}
        issues[iid] = {"id": iid, "title": v.get("title", iid),
                       "deps": v.get("deps", v.get("dependencies", []))}
        order.append(iid)
    return _toposort(issues, order)


def _toposort(issues: dict, order: list):
    result, visiting, done = [], set(), set()

    def visit(iid):
        if iid in done or iid not in issues or iid in visiting:
            return                                   # ciclo/ignoto → salta l'arco
        visiting.add(iid)
        for d in issues[iid]["deps"]:
            visit(d)
        visiting.discard(iid); done.add(iid); result.append(issues[iid])

    for iid in order:
        visit(iid)
    return result


# Step che si espandono per-issue (se esiste un *_DEPS.json). Il reviewer va
# per-issue perché ogni issue può essere stata implementata da un provider diverso
# (balancing) e va revisionata dall'altro.
FANOUT_STEPS = {"developer", "reviewer"}


def expand_units(steps: list, project: Path) -> list:
    """Espande gli step in unità: developer/reviewer → una per issue (se c'è un DEPS)."""
    units = []
    for step in steps:
        issues = load_issues(project) if step in FANOUT_STEPS else None
        if issues:
            for iss in issues:
                units.append(Unit(f"{step}::{iss['id']}", step, iss))
        else:
            units.append(Unit(step, step))
    return units


# --------------------------------------------------------------------------- #
#  Selezione del provider
# --------------------------------------------------------------------------- #
def available_providers(cfg: dict) -> list:
    return [p for p in cfg["preference"] if cfg["providers"].get(p, {}).get("enabled")]


# --------------------------------------------------------------------------- #
#  Balancer load-aware — costo pesato per provider, con reset a finestra
# --------------------------------------------------------------------------- #
def current_cost(state: dict, provider: str, cfg: dict) -> float:
    """Costo accumulato del provider nella finestra corrente; azzera se la finestra è scaduta."""
    b = state.get("budget", {}).get(provider)
    if not b:
        return 0.0
    win = cfg.get("balance", {}).get("window", {})
    hours = win.get(provider, win.get("default", 5))
    if time.time() - b.get("window_start", 0) >= hours * 3600:
        b["cost"] = 0.0
        b["window_start"] = int(time.time())     # la quota si è sbloccata → serbatoio pieno
    return b.get("cost", 0.0)


def charge(state: dict, provider: str, kind: str, cfg: dict, project: Path):
    """Addebita al provider il peso dell'operazione e persiste subito."""
    if not provider:
        return
    weight = cfg.get("balance", {}).get("weights", {}).get(kind, 1.0)
    budget = state.setdefault("budget", {})
    b = budget.setdefault(provider, {"cost": 0.0, "window_start": int(time.time())})
    b["cost"] = round(b.get("cost", 0.0) + weight, 3)
    save_state(project, state)


def ordered_candidates(candidates: list, cfg: dict, state: dict) -> list:
    """In routing 'balanced' ordina i candidati dal MENO carico al più carico."""
    if cfg.get("routing", "static") != "balanced":
        return candidates
    return sorted(candidates, key=lambda p: current_cost(state, p, cfg))


def role_weight_kind(step: str) -> str:
    """Che tipo di costo genera l'esecuzione principale di questo step."""
    return "evaluate" if step == "reviewer" else "implement"


def last_implementer_from_state(state: dict):
    """Provider dell'ultima unità implementante registrata nello stato."""
    best_ts, best = -1, None
    for u in state.get("units", {}).values():
        if u.get("step") in IMPLEMENTING_STEPS and u.get("provider") and u.get("ts", 0) >= best_ts:
            best_ts, best = u.get("ts", 0), u["provider"]
    return best


def pick_provider(agent_name: str, candidates: list, cfg: dict, implementer: str | None):
    """
    Regola di routing:
      • reviewer con cross_provider → primo candidato ≠ implementer
      • tutti gli altri            → primo candidato disponibile (fallback list)
    """
    if agent_name == "reviewer" and cfg.get("review", {}).get("cross_provider"):
        alt = [p for p in candidates if p != implementer]
        if alt:
            return alt[0], None
        # nessuna alternativa: dipende dalla policy
        policy = cfg.get("review", {}).get("on_no_alternative", "warn")
        if policy == "block":
            return None, "ANTI-BIAS BLOCK: nessun provider diverso dall'implementer disponibile."
        return (candidates[0] if candidates else None), \
               "ANTI-BIAS WARN: un solo provider attivo — review NON indipendente."
    return (candidates[0] if candidates else None), None


# --------------------------------------------------------------------------- #
#  Esecuzione di uno step
# --------------------------------------------------------------------------- #
def compose_prompt(agent: Agent, unit_input: str, project: Path, uid: str,
                   extra: str = "", resuming: bool = False) -> str:
    """Inietta l'input + l'istruzione di handoff; se si riprende, rilegge la nota."""
    body = agent.body.replace("{{ARGUMENTS}}", unit_input)
    hp = handoff_path(project, uid)
    body += HANDOFF_INSTRUCTION.format(handoff=hp)
    if resuming and hp.exists():
        body += ("\n\n[mentis] STAI RIPRENDENDO un lavoro interrotto. Nota di "
                 "progresso precedente:\n---\n" + hp.read_text() +
                 "\n---\nRiparti da lì: NON rifare ciò che è già sotto `Fatto:`.")
    if extra:
        body += "\n\n" + extra
    return body


def run_on_provider(agent, provider, prompt, project, cfg, dry_run):
    prompt = NEUTRALITY_PREAMBLE + prompt          # contratto di neutralità in testa
    model, reasoning_value, level = resolve(agent, provider, cfg)
    template = cfg["providers"][provider]["cmd"]
    argv = build_command(template, prompt, model, reasoning_value)
    printable = " ".join(shlex.quote(a) if a != prompt else "<PROMPT>" for a in argv)

    print(f"    → provider={provider}  model={model}  reasoning={level}({reasoning_value})")
    print(f"      cmd: {printable}")
    base = {"ts": int(time.time()), "provider": provider, "model": model,
            "reasoning": level, "agent": agent.name, "chars_in": len(prompt)}

    if dry_run:
        print("      [dry-run] non eseguito")
        log_call(project, {**base, "dry_run": True})
        return {"ok": True, "dry_run": True}

    try:
        r = subprocess.run(argv, cwd=str(project), capture_output=True,
                           text=True, timeout=cfg.get("timeout", 1800))
    except subprocess.TimeoutExpired:
        log_call(project, {**base, "error": "timeout"}, prompt, "TIMEOUT")
        return {"ok": False, "rate_limited": False, "error": "timeout"}
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    rate_limited = bool(RATE_LIMIT_PATTERNS.search(combined))
    ok = (r.returncode == 0) and not rate_limited
    if r.stdout:
        print(r.stdout.rstrip())
    log_call(project, {**base, "chars_out": len(combined), "returncode": r.returncode,
                       "rate_limited": rate_limited, "ok": ok}, prompt, combined)
    return {"ok": ok, "rate_limited": rate_limited,
            "returncode": r.returncode, "output": combined}


def run_unit_with_fallback(agent, unit, unit_input, project, cfg, implementer,
                           dry_run, resuming, breaker, budget, state, force_provider=None):
    all_cand = available_providers(cfg) or list(cfg["preference"])  # dry-run: piano
    # routing balanced: ordina dal meno carico; escludi i circuiti aperti
    candidates = ordered_candidates([p for p in all_cand if not breaker.get(p)], cfg, state)
    if force_provider and force_provider in candidates:            # parallelismo: provider pre-assegnato
        candidates = [force_provider] + [p for p in candidates if p != force_provider]
    if not candidates:
        print("    ✗ tutti i provider in circuito aperto (esauriti) — unità non eseguibile")
        return {"provider": None, "ok": False, "exhausted": True}

    provider, note = pick_provider(agent.name, candidates, cfg, implementer)
    if note:
        print(f"    ⚠ {note}")
    if provider is None:
        print("    ✗ nessun provider eleggibile — unità saltata")
        return {"provider": None, "ok": False}

    # ordine di tentativi: il provider scelto, poi gli altri come fallback.
    order = [provider] + [p for p in candidates if p != provider]
    if agent.name == "reviewer" and cfg.get("review", {}).get("cross_provider") and implementer:
        order = [p for p in order if p != implementer] or order

    rel = cfg.get("reliability", {})
    for prov in order:
        extra = ""
        if agent.name == "reviewer" and implementer:
            extra = f"Implemented-by: {implementer}\nReviewer-provider: {prov}"
        prompt = compose_prompt(agent, unit_input, project, unit.id, extra, resuming)
        res = run_on_provider(agent, prov, prompt, project, cfg, dry_run)
        if res.get("ok"):
            return {"provider": prov, "ok": True}

        # --- layered fallback ---
        if res.get("rate_limited"):
            if rel.get("circuit_open_for_run", True):
                breaker[prov] = True                     # CIRCUITO APERTO per il resto del run
            print(f"    ↯ {prov} esaurito/limite → circuito APERTO, fallback")
            print("      (contesto durevole su disco preservato; chi subentra legge l'handoff)")
            resuming = True                              # chi subentra riprende dall'handoff
        else:
            # errore transitorio: backoff + 1 retry sullo stesso provider
            wait = rel.get("backoff_seconds", 2)
            if budget["left"] > 0 and not dry_run and wait:
                print(f"    ⧖ {prov} errore transitorio → backoff {wait}s e 1 retry")
                time.sleep(wait); budget["left"] -= 1
                if run_on_provider(agent, prov, prompt, project, cfg, dry_run).get("ok"):
                    return {"provider": prov, "ok": True}
            print(f"    ✗ {prov} errore (rc={res.get('returncode')}) → fallback")

        budget["left"] -= 1
        if budget["left"] <= 0:
            print(f"    ⛔ budget di retry esaurito ({rel.get('retry_budget')}) → STOP")
            return {"provider": prov, "ok": False, "budget_exhausted": True}
    return {"provider": order[-1] if order else None, "ok": False}


def run_unit_compare(agent, unit_input, project, cfg, dry_run, uid):
    """Challenge: stesso agente su ogni provider, cartelle separate."""
    providers = available_providers(cfg) or list(cfg["preference"])
    for prov in providers:
        wd = project / f".mentis/compare/{agent.name}/{prov}"
        wd.mkdir(parents=True, exist_ok=True)
        print(f"  ── {agent.name} @ {prov} (out: {wd})")
        prompt = compose_prompt(agent, unit_input, wd, uid)
        run_on_provider(agent, prov, prompt, wd, cfg, dry_run)
    print(f"  ✎ confronto: output in {project}/.mentis/compare/{agent.name}/<provider>/  → diffali")


# --------------------------------------------------------------------------- #
#  Quality — Reflection (stesso provider) + Evaluator-Optimizer (cross-provider)
# --------------------------------------------------------------------------- #
def quality_level(agent_name, cfg, session_override):
    if session_override:
        return session_override
    return cfg.get("quality", {}).get("profile", {}).get(agent_name, "off")


def rubric_for(agent_name, cfg):
    r = cfg.get("quality", {}).get("rubric", {})
    return r.get(agent_name, r.get("default", "completezza; coerenza; correttezza"))


def reflection_pass(agent, unit, author_provider, project, cfg, dry_run, breaker, state):
    """Auto-critica sullo STESSO provider. Forte solo con feedback esterno (test)."""
    if breaker.get(author_provider):
        print("    ⤿ reflection saltata: provider autore in circuito aperto")
        return
    artifact = EXPECTED_ARTIFACT.get(unit.step, "il tuo output")
    prompt = (f"[mentis reflection] Rivedi criticamente il TUO output ({artifact}) dello step "
              f"'{unit.step}'. Criteri: {rubric_for(agent.name, cfg)}. Se hai prodotto codice, "
              f"esegui prima i test/build disponibili e correggi i fallimenti. Correggi i problemi "
              f"direttamente nei file; se è già solido non cambiare nulla e dichiaralo. Sii onesto: "
              f"non razionalizzare i tuoi errori.")
    print(f"    ↻ reflection @ {author_provider}")
    run_on_provider(agent, author_provider, prompt, project, cfg, dry_run)
    charge(state, author_provider, "reflect", cfg, project)


def parse_verdict(output, dry_run):
    """Robusto: cerca il tag `VERDICT: ...`; poi i token ovunque; ambiguo → NEEDS WORK."""
    if dry_run:
        return "APPROVED", ""
    text = output or ""
    up = text.upper()
    m = re.search(r"VERDICT\s*:\s*(APPROVED|NEEDS[ _]WORK)", up)      # 1) tag esplicito
    if m:
        v = "NEEDS WORK" if "NEEDS" in m.group(1) else "APPROVED"
        return v, (text if v == "NEEDS WORK" else "")
    has_needs = ("NEEDS WORK" in up) or ("NEEDS_WORK" in up)          # 2) token ovunque
    has_appr = "APPROVED" in up
    if has_needs and not has_appr:
        return "NEEDS WORK", text
    if has_appr and not has_needs:
        return "APPROVED", ""
    return "NEEDS WORK", text                                         # 3) ambiguo → conservativo


def evaluator_loop(agent, unit, author_provider, project, cfg, dry_run, breaker, state):
    """Evaluator su provider ≠ autore (il più scarico); loop optimizer fino a cap, poi escalation."""
    cap = cfg.get("quality", {}).get("loop_cap", 2)
    artifact = EXPECTED_ARTIFACT.get(unit.step, "l'artefatto")
    rubric = rubric_for(agent.name, cfg)
    all_cand = available_providers(cfg) or list(cfg["preference"])
    indep = ordered_candidates([p for p in all_cand if p != author_provider and not breaker.get(p)],
                               cfg, state)
    if not indep:
        print(f"    ⚠ EVALUATOR: nessun provider indipendente (autore={author_provider}, gli altri "
              f"giù/in circuito) → degrado a reflection-only + escalation.")
        reflection_pass(agent, unit, author_provider, project, cfg, dry_run, breaker, state)
        return {"status": "escalated", "reason": "no-independent-provider"}
    evaluator = indep[0]                          # evaluator = più scarico tra gli indipendenti

    for rnd in range(cap + 1):
        print(f"    ⇄ evaluator @ {evaluator} (autore={author_provider}) — valutazione {rnd + 1}")
        eval_prompt = (f"[mentis evaluator] Sei un valutatore INDIPENDENTE, su un modello DIVERSO "
                       f"dall'autore. Valuta criticamente {artifact} dello step '{unit.step}'. "
                       f"Criteri: {rubric}. Se ci sono problemi, elencali numerati e azionabili. "
                       f"Concludi con UNA riga esattamente in questo formato: "
                       f"`VERDICT: APPROVED` oppure `VERDICT: NEEDS WORK`.")
        res = run_on_provider(agent, evaluator, eval_prompt, project, cfg, dry_run)
        charge(state, evaluator, "evaluate", cfg, project)
        verdict, critique = parse_verdict(res.get("output", ""), dry_run)
        if verdict == "APPROVED":
            print(f"    ✓ evaluator APPROVED (indipendente: {evaluator} ≠ {author_provider})")
            return {"status": "approved", "evaluator": evaluator}
        if rnd >= cap:
            print(f"    ⛔ ancora NEEDS WORK dopo {cap} revisioni → STOP, escalation all'utente")
            return {"status": "escalated", "reason": "max-rounds", "critique": critique}
        if breaker.get(author_provider):
            print(f"    ⚠ autore {author_provider} non disponibile per la revisione → escalation")
            return {"status": "escalated", "reason": "author-unavailable"}
        print(f"    ✎ optimizer @ {author_provider}: revisione {rnd + 1}/{cap}")
        opt_prompt = (f"[mentis optimizer] Un valutatore indipendente (modello diverso) ha rivisto "
                      f"{artifact} e ha sollevato:\n{critique}\nRivedi l'artefatto per risolvere "
                      f"tutti i punti, senza introdurre regressioni.")
        run_on_provider(agent, author_provider, opt_prompt, project, cfg, dry_run)
        charge(state, author_provider, "optimize", cfg, project)
    return {"status": "approved"}


def apply_quality(agent, unit, author_provider, project, cfg, level, dry_run, breaker, state):
    if level in ("reflect", "full"):
        reflection_pass(agent, unit, author_provider, project, cfg, dry_run, breaker, state)
    if level in ("evaluate", "full"):
        return evaluator_loop(agent, unit, author_provider, project, cfg, dry_run, breaker, state)
    return {"status": "approved"}


def reviewer_loop(reviewer_agent, unit, implementer, project, cfg, dry_run, breaker, state):
    """Lo step reviewer come evaluator-optimizer: review → se NEEDS WORK ri-dispaccia il
    developer sull'implementer → re-review, fino a cap, poi escalation."""
    cap = cfg.get("quality", {}).get("loop_cap", 2)
    all_cand = available_providers(cfg) or list(cfg["preference"])
    indep = ordered_candidates([p for p in all_cand if p != implementer and not breaker.get(p)],
                               cfg, state)
    if not indep:
        print(f"    ⚠ REVIEWER: nessun provider indipendente (implementer={implementer}) → escalation")
        return {"status": "escalated", "reason": "no-independent-provider", "provider": None}
    rp = indep[0]                                  # reviewer = indipendente più scarico
    dev_agent = load_agent("developer")
    issue_txt = f"Issue: {unit.issue['id']} — {unit.issue['title']}\n" if unit.issue else ""

    for rnd in range(cap + 1):
        print(f"    ⇄ review @ {rp} (implementer={implementer}) — giro {rnd + 1}")
        rv_input = (f"{issue_txt}Rivedi la PR/il codice di questo lavoro come reviewer indipendente. "
                    f"Concludi con UNA riga: `VERDICT: APPROVED` oppure `VERDICT: NEEDS WORK` "
                    f"(+ problemi numerati se NEEDS WORK).")
        prompt = compose_prompt(reviewer_agent, rv_input, project, unit.id,
                                extra=f"Implemented-by: {implementer}\nReviewer-provider: {rp}")
        res = run_on_provider(reviewer_agent, rp, prompt, project, cfg, dry_run)
        charge(state, rp, "evaluate", cfg, project)
        verdict, critique = parse_verdict(res.get("output", ""), dry_run)
        if verdict == "APPROVED":
            print(f"    ✓ review APPROVED (indipendente: {rp} ≠ {implementer})")
            return {"status": "approved", "provider": rp}
        if rnd >= cap:
            print(f"    ⛔ ancora NEEDS WORK dopo {cap} rework → STOP, escalation all'utente")
            return {"status": "escalated", "reason": "max-rounds", "provider": rp}
        if breaker.get(implementer):
            print(f"    ⚠ implementer {implementer} non disponibile per il rework → escalation")
            return {"status": "escalated", "reason": "implementer-unavailable", "provider": rp}
        print(f"    ✎ rework @ {implementer} (developer): giro {rnd + 1}/{cap}")
        fix_input = (f"{issue_txt}Un reviewer indipendente ha chiesto modifiche:\n{critique}\n"
                     f"Applica le correzioni al codice, senza introdurre regressioni.")
        dev_prompt = compose_prompt(dev_agent, fix_input, project, unit.id, resuming=True)
        run_on_provider(dev_agent, implementer, dev_prompt, project, cfg, dry_run)
        charge(state, implementer, "optimize", cfg, project)
    return {"status": "approved", "provider": rp}


# --------------------------------------------------------------------------- #
#  doctor — controlla se i modelli mappati sono ancora quelli giusti
# --------------------------------------------------------------------------- #
def version_of(model: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", model)
    return float(m.group(1)) if m else 0.0


def get_available_models(cfg, provider, manual):
    """Lista modelli: da --models (a mano) o interrogando la CLI. None se non ottenibile."""
    if manual:
        return [m.strip() for m in manual.split(",") if m.strip()]
    lmc = cfg["providers"].get(provider, {}).get("list_models_cmd", "")
    if not lmc:
        return None
    try:
        r = subprocess.run(shlex.split(lmc), capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    kws = [v.lower() for v in cfg.get("tier_keywords", {}).get(provider, {}).values()]
    toks = re.findall(r"[A-Za-z][A-Za-z0-9._-]{2,}", r.stdout or "")
    found = [t for t in toks if any(k in t.lower() for k in kws)] or toks
    return sorted(set(found))


def suggest_for_tier(available, keyword):
    cands = [m for m in available if keyword.lower() in m.lower()]
    return max(cands, key=version_of) if cands else None


def apply_model_map(provider, changes):
    """Riscrive solo i valori tier nella sezione [model_map.<provider>], preservando i commenti."""
    lines = CONFIG_PATH.read_text().splitlines(keepends=True)
    in_section = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_section = (stripped == f"[model_map.{provider}]")
        if in_section:
            m = re.match(r'^(\s*)(frontier|balanced|fast)(\s*=\s*)"[^"]*"(.*)$', line)
            if m and m.group(2) in changes:
                line = f'{m.group(1)}{m.group(2)}{m.group(3)}"{changes[m.group(2)]}"{m.group(4)}\n'
        out.append(line)
    CONFIG_PATH.write_text("".join(out))


def cmd_doctor(cfg, provider_filter, manual, apply):
    if manual and not provider_filter:
        print("errore: --models richiede anche --provider <nome>")
        sys.exit(2)
    providers = [provider_filter] if provider_filter else list(cfg["providers"])
    print("🩺 mentis doctor — verifica mappatura modelli\n")
    total_changes = {}

    for p in providers:
        keywords = cfg.get("tier_keywords", {}).get(p, {})
        current_map = cfg.get("model_map", {}).get(p, {})
        print(f"── provider: {p}")
        available = get_available_models(cfg, p, manual if p == provider_filter else None)
        if available is None:
            print("   ⚠ impossibile interrogare la CLI (list_models_cmd vuoto o fallito).")
            print("     Passa la lista a mano:  doctor --provider {0} --models \"a,b,c\"\n".format(p))
            continue
        print(f"   modelli disponibili: {', '.join(available)}")
        changes = {}
        for tier in ("frontier", "balanced", "fast"):
            current = current_map.get(tier)
            kw = keywords.get(tier)
            best = suggest_for_tier(available, kw) if kw else None
            if current in available:
                if best and best != current and version_of(best) > version_of(current):
                    print(f"   • {tier:8} {current:16} ✓ attivo — più recente: {best}  (aggiornabile)")
                    changes[tier] = best
                else:
                    print(f"   • {tier:8} {current:16} ✓ ok")
            else:
                if best:
                    print(f"   • {tier:8} {current:16} ✗ NON più disponibile → propongo {best}")
                    changes[tier] = best
                else:
                    print(f"   • {tier:8} {current:16} ✗ NON disponibile e nessun candidato con keyword '{kw}'")
        if changes:
            total_changes[p] = changes
        print()

    if not total_changes:
        print("✔ tutto allineato, niente da fare.")
        return
    if apply:
        for p, changes in total_changes.items():
            apply_model_map(p, changes)
            for tier, newm in changes.items():
                print(f"✎ {p}.{tier} → {newm}")
        print(f"\n✔ config aggiornato: {CONFIG_PATH}")
    else:
        print("Proposte di aggiornamento sopra. Applica con:  doctor" +
              (f" --provider {provider_filter}" if provider_filter else "") +
              (f" --models \"{manual}\"" if manual else "") + " --apply")


# --------------------------------------------------------------------------- #
#  status — mostra carico per provider, finestre, stato delle unità
# --------------------------------------------------------------------------- #
def cmd_status(project, cfg):
    from collections import Counter
    state = load_state(project)
    print(f"🧭 mentis status — progetto: {project}")
    if state.get("command"):
        print(f"   ultimo comando: {state['command']}")
    budget = state.get("budget", {})
    if budget:
        print("   carico per provider (finestra corrente):")
        win = cfg.get("balance", {}).get("window", {})
        for p in cfg.get("preference", list(budget)):
            b = budget.get(p)
            if not b:
                continue
            cost = current_cost(state, p, cfg)
            hours = win.get(p, win.get("default", 5))
            remaining = max(0, hours * 3600 - (time.time() - b.get("window_start", 0))) / 3600
            print(f"     {p:8} costo={cost:5.1f}   ~{remaining:.1f}h alla ricarica della quota")
    units = state.get("units", {})
    if units:
        c = Counter(u.get("status", "?") for u in units.values())
        print("   unità:", "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
        for tag, mark in (("escalated", "⛔ escalation (attendono te)"), ("failed", "✗ fallite")):
            ids = [uid for uid, u in units.items() if u.get("status") == tag]
            if ids:
                print(f"   {mark}: {', '.join(ids)}")
    logs = project / ".mentis" / "logs" / "calls.jsonl"
    if logs.exists():
        n = sum(1 for _ in open(logs))
        print(f"   chiamate registrate: {n}   (log: {logs})")
    if not budget and not units:
        print("   (nessuno stato: nessun run ancora in questo progetto)")


# --------------------------------------------------------------------------- #
#  Esecuzione di una unità (estratta per supportare le wave parallele)
# --------------------------------------------------------------------------- #
def process_unit(ctx, unit, force_provider=None):
    project, cfg, state = ctx["project"], ctx["cfg"], ctx["state"]
    breaker, budget, dry_run = ctx["breaker"], ctx["budget"], ctx["dry_run"]
    agent = load_agent(unit.step)
    unit_input = ctx["user_input"]
    if unit.issue:
        unit_input = (f"Issue: {unit.issue['id']} — {unit.issue['title']}\n"
                      f"Label: Backend\n{unit_input}")
    h = input_hash(agent, unit_input)
    prev = state["units"].get(unit.id)
    label = unit.id + (f"  ({unit.issue['title']})" if unit.issue else "")
    print(f"▸ {label}  (tier={agent.tier}, reasoning={agent.reasoning})")

    # RESUME: salta se già completata, input invariato e artefatto presente
    if (prev and prev.get("status") == "done" and prev.get("input_hash") == h
            and artifact_ok(project, unit.step, dry_run)):
        print(f"    ⤿ già completata su {prev.get('provider')} — salto (resume)")
        return {"status": "skipped", "provider": prev.get("provider")}

    resuming = bool(prev and prev.get("status") == "running")
    if resuming:
        print("    ↻ unità interrotta in un run precedente — riprendo dalla nota di handoff")

    # implementer per l'anti-bias del reviewer (per-issue dallo stato)
    implementer = None
    if unit.step == "reviewer":
        if unit.issue:
            dev = state["units"].get(f"developer::{unit.issue['id']}")
            implementer = dev.get("provider") if dev else None
        implementer = implementer or last_implementer_from_state(state)

    pre_head = git_head(project) if not dry_run else None
    started_at = time.time()
    mark_unit(project, state, unit.id, status="running", step=unit.step, input_hash=h)

    # --- REVIEWER: loop review→rework (evaluator-optimizer) ---
    if unit.step == "reviewer":
        rev = reviewer_loop(agent, unit, implementer, project, cfg, dry_run, breaker, state)
        status = "escalated" if rev["status"] == "escalated" else ("planned" if dry_run else "done")
        mark_unit(project, state, unit.id, status=status, provider=rev.get("provider"),
                  quality_status=rev["status"])
        if rev["status"] == "escalated":
            print(f"    ⛔ ESCALATION ({rev.get('reason')}) su '{unit.id}' — mi fermo e scalo a te.")
        return {"status": status, "provider": rev.get("provider")}

    # --- step normale: implementazione + quality ---
    res = run_unit_with_fallback(agent, unit, unit_input, project, cfg, implementer,
                                 dry_run, resuming, breaker, budget, state, force_provider)
    if res.get("ok"):
        charge(state, res.get("provider"), role_weight_kind(unit.step), cfg, project)

    author = res.get("provider")
    level = quality_level(unit.step, cfg, ctx["quality_override"])
    if res.get("ok") and level != "off":
        print(f"    ◇ quality: {level}")
        qres = apply_quality(agent, unit, author, project, cfg, level, dry_run, breaker, state)
        mark_unit(project, state, unit.id, quality=level, quality_status=qres["status"])
        if qres.get("status") == "escalated":
            print(f"    ⛔ ESCALATION ({qres.get('reason')}) su '{unit.id}' — mi fermo e scalo a te.")
            mark_unit(project, state, unit.id, status="escalated")
            return {"status": "escalated", "provider": author}

    produced = produced_output(project, unit, dry_run, pre_head, started_at)
    if dry_run:
        mark_unit(project, state, unit.id, status="planned", provider=author)
        return {"status": "planned", "provider": author}
    if res.get("ok") and produced:
        mark_unit(project, state, unit.id, status="done", provider=author,
                  artifact=EXPECTED_ARTIFACT.get(unit.step))
        return {"status": "done", "provider": author}
    reason = "artefatto atteso assente" if res.get("ok") and not produced else "esecuzione fallita"
    mark_unit(project, state, unit.id, status="failed", provider=author, reason=reason)
    print(f"    ✗ unità fallita ({reason})")
    return {"status": "failed", "provider": author, "reason": reason}


def build_waves(units, cfg, parallel):
    """Raggruppa in wave parallele solo le unità developer indipendenti (deps non nella wave)."""
    if not parallel:
        return [[u] for u in units]
    nprov = len(cfg.get("preference", [])) or 1
    waves, i = [], 0
    while i < len(units):
        u = units[i]
        if u.step != "developer" or not u.issue:
            waves.append([u]); i += 1; continue
        wave, ids, j = [u], {u.issue["id"]}, i + 1
        while (j < len(units) and len(wave) < nprov and units[j].step == "developer"
               and units[j].issue and not (set(units[j].issue.get("deps", [])) & ids)):
            wave.append(units[j]); ids.add(units[j].issue["id"]); j += 1
        waves.append(wave); i = j
    return waves


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(prog="mentis", add_help=True)
    ap.add_argument("command", help="pipeline: build | tad | bad | review | mvp | doctor | status")
    ap.add_argument("description", nargs="*", help="descrizione/argomenti passati agli agenti")
    ap.add_argument("--project", default=".", help="cartella del progetto (default: corrente)")
    ap.add_argument("--compare", action="store_true", help="modalità challenge (tutti i provider)")
    ap.add_argument("--dry-run", action="store_true", help="stampa il piano senza eseguire")
    ap.add_argument("--fresh", action="store_true", help="ignora lo stato salvato e riparte da zero")
    ap.add_argument("--parallel", action="store_true", help="wave di issue developer indipendenti su provider distinti")
    ap.add_argument("--quality", choices=["off", "reflect", "evaluate", "full"],
                    help="override globale del controllo qualità (default: profilo per-agente)")
    # opzioni del comando `doctor`
    ap.add_argument("--provider", help="doctor: limita a un provider (es. codex)")
    ap.add_argument("--models", help="doctor: lista modelli a mano, es. \"gpt-5.7-sol,gpt-5.7-terra,gpt-5.7-luna\"")
    ap.add_argument("--apply", action="store_true", help="doctor: applica gli aggiornamenti al config")
    args = ap.parse_args()

    cfg = load_toml(CONFIG_PATH)

    if args.command == "doctor":
        cmd_doctor(cfg, args.provider, args.models, args.apply)
        return

    project = Path(args.project).resolve()
    user_input = " ".join(args.description)

    if args.command == "status":
        cmd_status(project, cfg)
        return

    pipelines = cfg.get("pipeline", {})
    if args.command not in pipelines:
        print(f"comando sconosciuto: '{args.command}'. Disponibili: {', '.join(pipelines)}")
        sys.exit(2)
    steps = pipelines[args.command]["steps"]

    enabled = available_providers(cfg)
    dry_run = args.dry_run or not enabled
    mode = "compare" if args.compare or cfg.get("mode") == "compare" else "fallback"

    print(f"🧠 mentis — progetto: {project}")
    print(f"   comando: {args.command}   modalità: {mode}")
    print(f"   provider attivi: {enabled or '— nessuno —'}   "
          f"(preferenza: {cfg['preference']})")
    if dry_run:
        print("   ⓘ DRY-RUN: mostro il piano, non eseguo (attiva un provider in config per eseguire)")
    print(f"   pipeline '{args.command}': {' → '.join(steps)}\n")

    units = expand_units(steps, project)
    state = {"units": {}} if args.fresh else load_state(project)
    state["command"] = args.command
    if len(units) != len(steps):
        print(f"   fan-out: {len(steps)} step → {len(units)} unità (developer/reviewer per-issue)")
    print()

    # --- COMPARE: challenge, nessuno stato/resume (è un confronto, non un build) ---
    if mode == "compare":
        for i, unit in enumerate(units, 1):
            agent = load_agent(unit.step)
            print(f"▸ [{i}/{len(units)}] {unit.id}  (tier={agent.tier}, reasoning={agent.reasoning})")
            run_unit_compare(agent, user_input, project, cfg, dry_run, unit.id)
            print()
        print("✔ fine (compare).")
        return

    # --- FALLBACK pipeline: resumability + checkpoint + quality + balancing + wave ---
    ctx = {"project": project, "cfg": cfg, "state": state, "breaker": {},
           "budget": {"left": cfg.get("reliability", {}).get("retry_budget", 6)},
           "dry_run": dry_run, "user_input": user_input, "quality_override": args.quality}

    parallel = args.parallel and not dry_run and len(available_providers(cfg)) >= 2
    stop = False
    for wave in build_waves(units, cfg, args.parallel):
        if stop:
            break
        if len(wave) > 1:
            print(f"⛓ wave {'parallela' if parallel else '(sequenziale in dry-run)'}: {[u.id for u in wave]}")
        if parallel and len(wave) > 1:
            ordered = ordered_candidates(available_providers(cfg), cfg, state)
            with ThreadPoolExecutor(max_workers=len(wave)) as ex:
                futs = {ex.submit(process_unit, ctx, u, ordered[k % len(ordered)]): u
                        for k, u in enumerate(wave)}
                results = {futs[f].id: f.result() for f in futs}
        else:
            results = {}
            for u in wave:
                results[u.id] = process_unit(ctx, u)
                if results[u.id]["status"] in ("failed", "escalated"):
                    break
        for u in wave:
            r = results.get(u.id)
            if r and r["status"] in ("failed", "escalated"):
                print(f"    ⛔ STOP su {u.id} ({r['status']}) — stato in {state_path(project)};")
                print(f"      rilancia lo stesso comando per riprendere (le 'done' si saltano), --fresh per azzerare.")
                stop = True
        print()

    if cfg.get("routing") == "balanced" and state.get("budget"):
        costs = "  ".join(f"{p}={current_cost(state, p, cfg):.1f}"
                          for p in cfg["preference"] if p in state["budget"])
        print(f"⚖  carico per provider (finestra corrente): {costs}")
    print("✔ fine." + ("  (dry-run: unità marcate 'planned', nessuna 'done')" if dry_run else ""))


if __name__ == "__main__":
    main()
