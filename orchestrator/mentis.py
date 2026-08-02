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
import sys, os, re, json, shlex, shutil, subprocess, argparse, time, hashlib, threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
try:
    import fcntl                # lock fra processi (POSIX); assente altrove → si degrada
except ImportError:                # pragma: no cover
    fcntl = None
from pathlib import Path

_CALL_SEQ = 0                 # sequenza globale per nomi-file transcript univoci
_CALLS_THIS_RUN = 0           # sessioni CLI davvero eseguite in questo run (tetto di spesa)
_STATE_LOCK = threading.Lock()  # protegge state.json in esecuzione parallela
_GIT_LOCK = threading.Lock()    # serializza le operazioni git worktree (add/remove)
_BALANCE_LOCK = threading.Lock()  # protegge il bilancio condiviso fra thread

__version__ = "1.0.0"          # bump quando cambia il contratto con gli agenti o il config

ROOT = Path(__file__).resolve().parent.parent          # .../mentis
AGENTS_DIR = ROOT / "agents"
CONFIG_PATH = ROOT / "config" / "mentis.toml"

# Step che PRODUCONO codice: il reviewer deve girare su un provider diverso da
# quello che ha eseguito l'ultimo di questi.
IMPLEMENTING_STEPS = {"developer", "devops-engineer", "qa-engineer", "mvp-builder"}

# Ladder ordinata per il clamping del reasoning quando un provider non ha un
# gradino esatto.
REASONING_LADDER = ["none", "low", "medium", "high", "max"]

# Tetto di sicurezza al numero di unità generabili da un *_DEPS.json (output LLM).
MAX_ISSUES = 200

# Pattern che indicano esaurimento/limite → trigger di fallback.
# NB: include le formule reali osservate sulle CLI a subscription ("spend limit",
# "monthly limit", "usage limit") — non solo il lessico API (429/quota).
RATE_LIMIT_PATTERNS = re.compile(
    r"rate.?limit|quota|usage limit|spend limit|monthly limit|out of credit|"
    r"too many requests|\b429\b|overloaded|insufficient.?quota|resource.?exhausted|"
    r"limit reached|upgrade your plan",
    re.IGNORECASE,
)

# Variabili d'ambiente che farebbero fatturare le CLI ad API a consumo: rimosse
# dall'ambiente dei sottoprocessi. Il vincolo del progetto è SOLO abbonamento.
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                    "OPENAI_API_BASE", "ANTHROPIC_BASE_URL")


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
    """Rimuove un commento '#' che non sia dentro una stringa (gestisce le `\\"` escaped)."""
    out, in_str, esc = [], False, False
    for ch in line:
        if ch == '"' and not esc:
            in_str = not in_str
        if ch == "#" and not in_str:
            break
        esc = (ch == "\\" and not esc)
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

# Contratto di ESITO strutturato: ogni agente principale chiude con un blocco
# machine-readable. È l'interfaccia (non la prosa) che l'orchestratore consuma.
CONTRACT_INSTRUCTION = (
    "\n\n---\n[mentis — contratto di esito] Come ULTIMA cosa del tuo output, emetti "
    "ESATTAMENTE un blocco così (JSON valido su una riga tra i marcatori):\n"
    "[[MENTIS-RESULT]]\n"
    '{"status": "done|needs_input|failed", "artifacts": ["path/relativi/prodotti"], '
    '"questions": ["domande bloccanti per l\'umano"], "note": "una riga"}\n'
    "[[/MENTIS-RESULT]]\n"
    "Regole: usa `needs_input` SOLO per decisioni che spettano davvero all'umano "
    "(non conferme banali) elencandole in `questions`; NON usare tool interattivi — "
    "le domande vanno SOLO in questo blocco. `done` con gli `artifacts` che hai scritto. "
    "`failed` + `note` se non puoi completare."
)


def state_path(project: Path) -> Path:
    return project / ".mentis" / "state.json"


def load_state(project: Path) -> dict:
    p = state_path(project)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception as e:
            # NON in silenzio: ripartire da zero significa rifare (e ri-pagare)
            # tutto il lavoro già fatto — chi lancia deve saperlo.
            print(f"⚠ {p} illeggibile ({e}): riparto da stato VUOTO — le unità già "
                  f"completate verranno rifatte. Il file danneggiato è ancora lì.")
    return {"units": {}}


def save_state(project: Path, state: dict):
    p = state_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")          # write ATOMICA: temp + replace
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(str(tmp), str(p))                # un crash a metà non corrompe state.json


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
    d = project / ".mentis" / "logs"
    with _STATE_LOCK:
        if _CALL_SEQ == 0:                        # seed dal log esistente: niente transcript
            f = d / "calls.jsonl"                 # sovrascritti quando si RIPRENDE un run
            if f.exists():
                try:
                    _CALL_SEQ = max((json.loads(l).get("seq", 0)
                                     for l in f.read_text().splitlines() if l.strip()), default=0)
                except Exception:
                    pass
        _CALL_SEQ += 1
        seq = _CALL_SEQ
    (d / "transcripts").mkdir(parents=True, exist_ok=True)
    entry = {"seq": seq, **entry}
    with open(d / "calls.jsonl", "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if prompt or output:
        fn = d / "transcripts" / f"{seq:05d}-{entry.get('provider')}-{entry.get('agent')}.txt"
        fn.write_text(f"=== PROMPT ===\n{prompt}\n\n=== OUTPUT ===\n{output}")


ALL_ARTIFACT_DIRS = ("business-analysis", "tech-analysis", "implementation-plans")

# Artefatti che stanno DAVVERO a monte di ciascuno step. Regola invariante: uno
# step non può includere nell'hash il PROPRIO output (né quello dei suoi
# downstream), altrimenti al rilancio si trova un input "cambiato" da sé stesso e
# si rifà all'infinito — con esso l'intera pipeline a valle. Chi non è elencato
# qui sta a valle di tutto e usa ALL_ARTIFACT_DIRS.
UPSTREAM_DIRS = {
    "business-analyst":       (),
    "tech-architect":         ("business-analysis",),
    "implementation-planner": ("business-analysis", "tech-analysis"),
}


def upstream_hash(project: Path, step: str = None) -> str:
    """Hash dei soli artefatti a monte dello step: se cambiano, lo step è stale."""
    dirs = UPSTREAM_DIRS.get(step, ALL_ARTIFACT_DIRS) if step is not None else ALL_ARTIFACT_DIRS
    h = hashlib.sha1()
    for sub in dirs:
        d = project / sub
        if d.is_dir():
            for f in sorted(d.glob("*")):
                if f.is_file():
                    try:
                        h.update(f.read_bytes())
                    except Exception:
                        pass
    return h.hexdigest()[:12]


def input_hash(agent: "Agent", unit_input: str, project: Path = None) -> str:
    h = hashlib.sha1()
    h.update(agent.body.encode()); h.update(b"\0"); h.update(unit_input.encode())
    if project is not None:                       # dependency-aware: solo gli artefatti A MONTE
        h.update(b"\0"); h.update(upstream_hash(project, agent.name).encode())
    return h.hexdigest()[:12]


def handoff_path(project: Path, uid: str, cwd: Path = None) -> Path:
    """Nota di handoff dell'unità. In `--parallel` l'agente gira in un worktree e
    non può scrivere fuori dalla sua working dir: la nota va quindi creata LÌ e
    poi copiata nel progetto (`sync_handoff`), che è dove il resume la cerca."""
    return (cwd or project) / ".mentis" / "handoff" / f"{_safe(uid)}.md"


def sync_handoff(project: Path, uid: str, cwd: Path = None):
    """Riporta nel progetto la nota scritta dentro un worktree, prima che il
    worktree sparisca: altrimenti il contesto durevole dell'unità va perso."""
    if not cwd or Path(cwd) == project:
        return
    src = handoff_path(project, uid, cwd)
    if not src.exists():
        return
    try:
        dst = handoff_path(project, uid)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text())
    except Exception:
        pass


def _safe(uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", uid)


def questions_path(project: Path, uid: str) -> Path:
    return project / ".mentis" / "questions" / f"{_safe(uid)}.md"


def answers_path(project: Path, uid: str) -> Path:
    return project / ".mentis" / "answers" / f"{_safe(uid)}.md"


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


def git_dirty_set(project: Path) -> set:
    """Path modificati nel working tree, ESCLUSE le scritture dell'orchestratore
    (`.mentis/` e il `.gitignore` che mentis stesso tocca a inizio run: se
    restasse dentro, il tree risulterebbe sporco per tutto il run e QUALSIASI
    unità passerebbe la verifica 'ha prodotto qualcosa')."""
    out = set()
    try:
        r = subprocess.run(["git", "-C", str(project), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return out
        for line in r.stdout.splitlines():
            path = line[3:].strip().strip('"')
            if not path or path.startswith(".mentis") or path == ".gitignore":
                continue
            out.add(path)
    except Exception:
        pass
    return out


def git_dirty(project: Path) -> bool:
    """True se il working tree ha modifiche non-mentis."""
    return bool(git_dirty_set(project))


def make_worktree(project: Path, uid: str):
    """Crea un git worktree isolato per un'unità parallela (branch mentis/{uid}).
    Ritorna il path del worktree, o None se il progetto non è git o l'op fallisce.
    Serializzato da _GIT_LOCK: `git worktree add` non è concorrente-safe."""
    if git_head(project) is None:
        return None
    wt = project / ".mentis" / "worktrees" / _safe(uid)
    branch = f"mentis/{_safe(uid)}"
    with _GIT_LOCK:
        if wt.exists():
            return wt
        wt.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["git", "-C", str(project), "worktree", "add", "-b", branch,
                            str(wt), "HEAD"], capture_output=True, text=True)
        if r.returncode != 0:                      # branch già esistente (run precedente) → riusa
            print(f"    ⚠ branch {branch} già esistente da un run precedente: riparto dal SUO tip, "
                  f"non da HEAD (cancellalo se vuoi ripartire pulito)")
            r = subprocess.run(["git", "-C", str(project), "worktree", "add", str(wt), branch],
                               capture_output=True, text=True)
        return wt if r.returncode == 0 else None


GIT_ID = ["-c", "user.name=mentis", "-c", "user.email=mentis@localhost"]


def remove_worktree(project: Path, wt, uid: str = ""):
    """Rimuove il worktree (il branch resta nel repo: è il deliverable dell'unità).
    PRIMA committa sul branch ciò che l'agente non ha committato: `remove --force`
    lo cancellerebbe, e senza permessi Bash l'agente potrebbe non aver potuto
    committare nulla — l'unità risulterebbe 'done' con il lavoro distrutto."""
    if not wt:
        return
    with _GIT_LOCK:
        try:
            st = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                capture_output=True, text=True, timeout=30)
            if st.returncode == 0 and st.stdout.strip():
                subprocess.run(["git", "-C", str(wt), "add", "-A"],
                               capture_output=True, text=True, timeout=60)
                c = subprocess.run(["git", "-C", str(wt)] + GIT_ID +
                                   ["commit", "-q", "-m", f"mentis: lavoro non committato ({uid or wt.name})"],
                                   capture_output=True, text=True, timeout=60)
                if c.returncode == 0:
                    print(f"    ⛊ {wt.name}: lavoro non committato salvato sul branch prima del cleanup")
        except Exception:
            pass
        subprocess.run(["git", "-C", str(project), "worktree", "remove", "--force", str(wt)],
                       capture_output=True, text=True)


def merge_unit_branch(project: Path, uid: str) -> bool:
    """Integra il branch di un'unità parallela nel branch principale. Senza questo
    passo il codice resta su `mentis/*` e gli step a valle (qa, reviewer, docs)
    girano su un albero che NON lo contiene. Su conflitto: abort + avviso, il
    branch resta lì per la risoluzione manuale."""
    branch = f"mentis/{_safe(uid)}"
    with _GIT_LOCK:
        r = subprocess.run(["git", "-C", str(project)] + GIT_ID +
                           ["merge", "--no-edit", "-m", f"mentis: integra {uid}", branch],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(f"    ⛙ {branch} integrato nel branch principale")
            return True
        subprocess.run(["git", "-C", str(project), "merge", "--abort"],
                       capture_output=True, text=True)
        print(f"    ⚠ merge di {branch} NON riuscito (conflitto o tree sporco): il codice resta "
              f"sul branch. Gli step a valle non lo vedranno finché non lo integri a mano.")
        return False


def repo_facts(project: Path) -> dict:
    """Cosa esiste DAVVERO nel progetto-target: i corpi degli agenti assumono
    remote GitHub + gh + CI, che un progetto nuovo non ha. Meglio dirglielo noi
    che lasciarli fallire un comando alla volta."""
    import shutil as _sh
    facts = {"git": git_head(project) is not None, "remote": False, "gh": bool(_sh.which("gh"))}
    try:
        r = subprocess.run(["git", "-C", str(project), "remote"],
                           capture_output=True, text=True, timeout=10)
        facts["remote"] = bool(r.returncode == 0 and r.stdout.strip())
    except Exception:
        pass
    if facts["gh"]:                                   # installata ≠ autenticata
        try:
            facts["gh"] = subprocess.run(["gh", "auth", "status"], capture_output=True,
                                         text=True, timeout=20).returncode == 0
        except Exception:
            facts["gh"] = False
    return facts


def repo_note(project: Path, branch: str = "", in_worktree: bool = False) -> str:
    """Blocco di contesto iniettato negli step che toccano il repo: dichiara cosa è
    possibile qui, così l'agente non spreca tentativi (e quota) su push/PR/CI
    inesistenti né inventa un branch a caso."""
    f = repo_facts(project)
    lines = ["\n[mentis — contesto reale del repo] Vale su tutto ciò che segue:"]
    if branch:
        if in_worktree:
            lines.append(f"• Sei GIÀ sul branch `{branch}` in un worktree isolato: committa qui. "
                         f"NON fare `git checkout main` né `git pull` (fallirebbero: main è "
                         f"in uso nel worktree principale).")
        else:
            lines.append(f"• Branch di lavoro: `{branch}` — crealo da HEAD (`git checkout -b`) "
                         f"se non esiste. NON fare `git pull`.")
    if not f["remote"]:
        lines.append("• NON esiste un remote `origin`: `git push`, `gh pr create` e qualsiasi "
                     "controllo CI sono IMPOSSIBILI qui. Salta quei passi senza cercare "
                     "alternative: committa in locale ed elenca i commit nel report finale.")
    elif not f["gh"]:
        lines.append("• `gh` non è disponibile/autenticato: niente PR né CI. Committa e pusha "
                     "solo se il remote risponde; salta i passi di PR/review su GitHub.")
    lines.append("• Committa il tuo lavoro: ciò che resta non committato può andare perso.")
    return "\n".join(lines) + "\n"


def branch_for(unit) -> str:
    """Convenzione di branch per un'unità con issue: feat/{id}-{slug-del-titolo}."""
    if not unit.issue:
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", str(unit.issue.get("title", "")).lower()).strip("-")[:40]
    return f"feat/{str(unit.issue['id']).lower()}-{slug}".rstrip("-")


def ensure_git_repo(project: Path) -> bool:
    """Il progetto-target deve essere un repo git con almeno un commit: worktree,
    verifica dell'output e i flussi branch/PR degli agenti lo assumono. Se manca,
    lo inizializza con un commit vuoto (non ingoia file preesistenti)."""
    if git_head(project) is not None:
        return True
    try:
        if not (project / ".git").exists():
            r = subprocess.run(["git", "-C", str(project), "init", "-q"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                return False
        subprocess.run(["git", "-C", str(project)] + GIT_ID +
                       ["commit", "-q", "--allow-empty", "-m", "mentis: bootstrap del repo"],
                       capture_output=True, text=True, timeout=30)
        ok = git_head(project) is not None
        if ok:
            print(f"   ⎇ repo git inizializzato in {project} (commit di bootstrap vuoto)")
        return ok
    except Exception:
        return False


def ensure_gitignore(project: Path):
    """Nel progetto-target, assicura che .mentis/ sia gitignorato (stato/log/transcript)."""
    gi = project / ".gitignore"
    try:
        existing = gi.read_text() if gi.exists() else ""
        if ".mentis" not in existing:
            sep = "" if (not existing or existing.endswith("\n")) else "\n"
            with open(gi, "a") as f:
                f.write(f"{sep}.mentis/\n")
    except Exception:
        pass


def produced_output(project: Path, unit, dry_run: bool, pre_head, started_at,
                    pre_dirty=None) -> bool:
    """Check STRETTO post-esecuzione: l'unità ha davvero prodotto qualcosa?
    Esclude sempre le scritture dell'orchestratore stesso (.mentis/, .gitignore)
    e confronta il tree con la BASELINE presa prima dell'unità: un progetto già
    sporco all'avvio non deve far passare per 'done' un'unità che non ha fatto nulla."""
    if dry_run:
        return True
    glob = EXPECTED_ARTIFACT.get(unit.step)
    if glob:
        # l'artefatto deve essere stato scritto/aggiornato DA QUESTA unità, non
        # essere il residuo di un run precedente
        return any(f.stat().st_mtime >= started_at
                   for f in project.glob(glob) if f.is_file())
    if unit.step in IMPLEMENTING_STEPS:                 # developer/qa/devops/mvp
        head = git_head(project)
        if head is not None:                           # repo git: HEAD avanzato o tree cambiato
            return head != pre_head or git_dirty_set(project) != (pre_dirty or set())
        for p in project.rglob("*"):                   # non-git: file toccati dopo l'avvio, ESCLUSO .mentis/
            if ".mentis" in p.parts:
                continue
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
    """Issue da implementation-plans/*_DEPS.json. Schema CANONICO UNICO:
         {"issues": [ {"id": "...", "title": "...", "label": "Backend|Frontend|DevOps",
                       "deps": ["id", ...]}, ... ]}
    Tollerante: valida e in caso di formato non conforme AVVISA e ritorna None
    (niente fan-out) invece di crashare. Mai iterare valori arbitrari come issue."""
    files = sorted(project.glob("implementation-plans/*_DEPS.json"))
    if not files:
        return None
    if len(files) > 1:                             # un piano vecchio può vincere per ordine alfabetico
        print(f"    ⚠ {len(files)} file *_DEPS.json presenti: uso {files[0].name} (ordine "
              f"alfabetico). Rimuovi quelli obsoleti se non è il piano giusto.")
    try:
        data = json.loads(files[0].read_text())
    except Exception as e:
        print(f"    ⚠ DEPS illeggibile ({files[0].name}): {e} — niente fan-out")
        return None
    if isinstance(data, dict) and isinstance(data.get("issueMap"), dict):
        # formato legacy pocket-it (issueMap + dependencies Linear) → converto in issues[]
        deps_by = {}
        for d in data.get("dependencies", []):
            if isinstance(d, dict) and d.get("blockedPlanId") and d.get("blockerPlanId"):
                deps_by.setdefault(d["blockedPlanId"], []).append(d["blockerPlanId"])
        raw = [{"id": k, "title": (v or {}).get("title", k),
                "label": (v or {}).get("label", "Backend"), "deps": deps_by.get(k, [])}
               for k, v in data["issueMap"].items()]
    else:
        raw = data.get("issues") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        print(f"    ⚠ DEPS {files[0].name}: manca la lista 'issues' "
              f"[{{id,title,label,deps}}] — niente fan-out (unità singola)")
        return None
    issues, order = {}, []
    for it in raw:
        if not isinstance(it, dict) or not it.get("id"):
            continue                                   # scarta voci non conformi, mai crash
        iid = str(it["id"])
        raw_deps = it.get("deps", it.get("dependencies", [])) or []      # `null`/assente → []
        if not isinstance(raw_deps, list):                               # valore singolo o sporco
            raw_deps = [raw_deps]
        issues[iid] = {"id": iid, "title": it.get("title", iid),
                       "label": it.get("label", "Backend"),
                       "deps": [str(d) for d in raw_deps if d]}
        order.append(iid)
    if len(order) > MAX_ISSUES:                    # tetto: un DEPS anomalo (LLM) non genera un runaway
        print(f"    ⚠ DEPS: {len(order)} issue oltre il tetto di {MAX_ISSUES} — troncate "
              f"(probabile output anomalo). Alza MAX_ISSUES se è legittimo.")
        keep = set(order[:MAX_ISSUES])
        issues = {k: v for k, v in issues.items() if k in keep}
        order = order[:MAX_ISSUES]
    return _toposort(issues, order) if issues else None


def _toposort(issues: dict, order: list):
    result, visiting, done, cycles = [], set(), set(), []

    def visit(iid):
        if iid in done or iid not in issues:
            return
        if iid in visiting:                          # arco che chiude un ciclo → ignorato
            cycles.append(iid)
            return
        visiting.add(iid)
        for d in issues[iid]["deps"]:
            visit(d)
        visiting.discard(iid); done.add(iid); result.append(issues[iid])

    for iid in order:
        visit(iid)
    if cycles:
        print(f"    ⚠ dipendenze cicliche nel DEPS (archi ignorati, ordine potenzialmente "
              f"non ottimale): {sorted(set(cycles))}")
    return result


# Step che si espandono per-issue (se esiste un *_DEPS.json). Il reviewer va
# per-issue perché ogni issue può essere stata implementata da un provider diverso
# (balancing) e va revisionata dall'altro.
FANOUT_STEPS = {"developer", "reviewer"}

# Routing per label: la issue viene implementata dall'agente giusto. È ciò che
# rende `devops-engineer` RAGGIUNGIBILE (altrimenti non è in nessuna pipeline).
LABEL_TO_AGENT = {"backend": "developer", "frontend": "developer",
                  "devops": "devops-engineer", "infra": "devops-engineer",
                  "infrastructure": "devops-engineer"}


def agent_for_label(label: str) -> str:
    return LABEL_TO_AGENT.get((label or "").strip().lower(), "developer")


def expand_step(step: str, project: Path, fanout: bool = True) -> list:
    """Espande UN singolo step in unità, leggendo il DEPS AL MOMENTO (lazy).
    Lo step 'developer' instrada ogni issue all'agente giusto per label
    (Backend/Frontend → developer, DevOps → devops-engineer).
    `fanout=False` (comando `review` mirato) tiene lo step come unità singola:
    altrimenti una review su una PR esploderebbe in N review, una per issue."""
    issues = load_issues(project) if (fanout and step in FANOUT_STEPS) else None
    if not issues:
        return [Unit(step, step)]
    if step == "developer":
        return [Unit(f"{agent_for_label(i['label'])}::{i['id']}",
                     agent_for_label(i["label"]), i) for i in issues]
    return [Unit(f"{step}::{i['id']}", step, i) for i in issues]     # reviewer


# --------------------------------------------------------------------------- #
#  Selezione del provider
# --------------------------------------------------------------------------- #
def available_providers(cfg: dict) -> list:
    return [p for p in cfg["preference"] if cfg["providers"].get(p, {}).get("enabled")]


# --------------------------------------------------------------------------- #
#  Balancer load-aware — costo pesato per provider, con reset a finestra
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
#  Bilancio CONDIVISO fra i progetti (~/.mentis/balance.json)
#  La quota è dell'ACCOUNT, non del progetto: tenere il contatore dentro il
#  progetto faceva ripartire da zero ogni nuovo progetto, mentre la quota vera
#  era già stata spesa altrove. Il consumo si accumula quindi in un file utente,
#  con lock fra processi perché due run su progetti diversi possono girare insieme.
# --------------------------------------------------------------------------- #
def mentis_home() -> Path:
    return Path(os.environ.get("MENTIS_HOME") or (Path.home() / ".mentis"))


def balance_path() -> Path:
    return mentis_home() / "balance.json"


@contextmanager
def open_balance(write: bool = False):
    """Apre il bilancio condiviso in lettura-modifica-scrittura, serializzando
    thread (in-process) e processi (flock). Se il lock non è disponibile sulla
    piattaforma si degrada a non-bloccante: peggio un conteggio impreciso che un
    run che non parte."""
    p = balance_path()
    if not write and not p.exists():
        # una semplice LETTURA non deve materializzare nulla: altrimenti un
        # `--dry-run` creerebbe ~/.mentis pur non consumando niente.
        yield {"providers": {}}
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with _BALANCE_LOCK:
        f = open(p, "a+")
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass
            f.seek(0)
            raw = f.read()
            try:
                bal = json.loads(raw) if raw.strip() else {}
            except Exception:
                print(f"⚠ {p} illeggibile: riparto da bilancio vuoto")
                bal = {}
            bal.setdefault("providers", {})
            yield bal
            if write:                              # riscrittura in-place SOTTO lock
                f.seek(0); f.truncate()
                f.write(json.dumps(bal, indent=2))
                f.flush()
                os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            f.close()


def _window_hours(cfg: dict, provider: str) -> float:
    win = cfg.get("balance", {}).get("window", {})
    return float(win.get(provider, win.get("default", 5)))


def _apply_window(bal: dict, provider: str, cfg: dict) -> dict:
    """Azzera il conteggio se la finestra di ricarica è scaduta. Ritorna il record."""
    b = bal["providers"].setdefault(provider, {"cost": 0.0, "window_start": int(time.time())})
    if time.time() - b.get("window_start", 0) >= _window_hours(cfg, provider) * 3600:
        b["cost"] = 0.0
        b["window_start"] = int(time.time())       # la quota si è sbloccata → serbatoio pieno
        b["reset"] = True
    return b


def current_cost(provider: str, cfg: dict) -> float:
    """Costo accumulato dal provider nella finestra corrente, su TUTTI i progetti."""
    with open_balance(write=False) as bal:
        if provider not in bal["providers"]:
            return 0.0
        b = _apply_window(bal, provider, cfg)
        cost = b.get("cost", 0.0)
    if b.pop("reset", None):                       # la finestra è scaduta: persisti l'azzeramento
        with open_balance(write=True) as bal2:
            rec = bal2["providers"].setdefault(provider, {})
            rec["cost"] = 0.0
            rec["window_start"] = int(time.time())
    return cost


def mark_exhausted(provider: str, cfg: dict, project: Path = None):
    """Un rate-limit VERO è l'unico segnale affidabile sulla quota: in quel momento
    sai per certo che è finita, qualunque cosa dicesse la stima.

    Due cose vengono registrate nel bilancio condiviso:
      1. che il provider è esaurito → gli altri progetti e i run successivi lo
         evitano fino alla ricarica, senza contabilità a mano;
      2. **il costo raggiunto in quel momento**, che è il TETTO osservato: da lì
         in poi mentis ha finalmente un denominatore e può dire "sei al 60%"
         invece di un numero senza scala. Si media sulle osservazioni successive,
         perché il limite reale varia (modello usato, lunghezza delle sessioni)."""
    with open_balance(write=True) as bal:
        b = bal["providers"].setdefault(provider, {"cost": 0.0, "window_start": int(time.time())})
        b.pop("reset", None)
        reached = b.get("cost", 0.0)
        if reached > 0:
            prev, n = b.get("limit_observed"), b.get("limit_samples", 0)
            b["limit_observed"] = round(reached if not prev else (prev * n + reached) / (n + 1), 3)
            b["limit_samples"] = n + 1
        reached_tok = int(b.get("tokens", 0))       # se contiamo i token veri, il tetto è QUESTO
        if reached_tok > 0:
            prevt, nt = b.get("limit_tokens"), b.get("limit_tokens_samples", 0)
            b["limit_tokens"] = int(reached_tok if not prevt else (prevt * nt + reached_tok) / (nt + 1))
            b["limit_tokens_samples"] = nt + 1
        b["exhausted_at"] = int(time.time())
        b["window_start"] = int(time.time())       # la ricarica si conta DA ORA
        if project:
            b["last_project"] = str(project)
        limit = b.get("limit_observed")
    print(f"      ⓘ quota di {provider} esaurita: registrata in {balance_path().name}, "
          f"gli altri progetti lo eviteranno fino alla ricarica"
          + (f" — tetto osservato ≈{limit} (ora mentis sa quanto vale il 100%)" if limit else ""))


def quota_used_pct(provider: str, cfg: dict):
    """Percentuale di quota consumata → (pct, unità) oppure None.

    Serve un denominatore, e l'unico modo di conoscerlo su un abbonamento è
    averci sbattuto contro almeno una volta (nessuna CLI espone la quota
    residua). Se stiamo contando i **token veri** la stima è attendibile; se
    contiamo operazioni pesate è solo un ordine di grandezza, perché il tetto
    espresso in quell'unità varia col mix di lavoro. Finché non si è mai toccato
    il muro si ritorna None: meglio tacere che inventare una percentuale."""
    current_cost(provider, cfg)                    # applica l'eventuale reset di finestra
    with open_balance(write=False) as bal:
        b = bal["providers"].get(provider) or {}
    if b.get("limit_tokens") and b.get("tokens"):
        return min(999.0, 100.0 * b["tokens"] / b["limit_tokens"]), "token"
    if b.get("limit_observed"):
        return min(999.0, 100.0 * b.get("cost", 0.0) / b["limit_observed"]), "stima"
    return None


def load_factor(provider: str, cfg: dict) -> float:
    """Quanto è carico un provider, per l'ordinamento. Se la quota è calibrata si
    usa la percentuale — è la grandezza giusta: si manda il lavoro a chi ha più
    quota RESIDUA, non a chi ha fatto meno chiamate."""
    pct = quota_used_pct(provider, cfg)
    return pct[0] if pct else current_cost(provider, cfg)


def exhausted_providers(cfg: dict) -> dict:
    """Provider che risultano esauriti e non ancora ricaricati → {provider: ore residue}."""
    out = {}
    with open_balance(write=False) as bal:
        for p, b in bal["providers"].items():
            ts = b.get("exhausted_at")
            if not ts:
                continue
            left = _window_hours(cfg, p) * 3600 - (time.time() - ts)
            if left > 0:
                out[p] = left / 3600
    return out


def migrate_project_budget(project: Path, state: dict):
    """Un progetto creato prima del bilancio condiviso ha il suo contatore dentro
    `state.json`: lo si travasa una volta sola, così il carico già speso non sparisce."""
    old = state.get("budget")
    if not old or state.get("budget_migrated"):
        return
    with open_balance(write=True) as bal:
        for prov, rec in old.items():
            if not isinstance(rec, dict):
                continue
            b = bal["providers"].setdefault(prov, {"cost": 0.0,
                                                   "window_start": rec.get("window_start", int(time.time()))})
            b["cost"] = round(b.get("cost", 0.0) + rec.get("cost", 0.0), 3)
            b["window_start"] = min(b.get("window_start", int(time.time())),
                                    rec.get("window_start", int(time.time())))
    state["budget_migrated"] = True
    print(f"   ⇄ bilancio di questo progetto travasato nel contatore condiviso "
          f"({balance_path()}): la quota è dell'account, non del progetto.")


def tier_multiplier(cfg: dict, tier: str) -> float:
    """Quanto pesa una chiamata a quel tier rispetto a una `balanced`."""
    return float(cfg.get("balance", {}).get("tier", {}).get(tier or "balanced", 1.0))


def charge(state: dict, provider: str, kind: str, cfg: dict, project: Path,
           dry_run: bool = False, tier: str = None, tokens: int = 0):
    """Addebita il peso dell'operazione (tipo × tier) e persiste subito.

    Due contatori, con scopi diversi:
      • il **bilancio condiviso** (`~/.mentis/balance.json`) è quello che guida il
        routing — la quota è dell'account, quindi va sommata su tutti i progetti;
      • il **totale di progetto** (in `state.json`) resta a titolo informativo:
        "quanto è costato QUESTO progetto".
    In dry-run non addebita nulla: il primo run reale partirebbe con carichi
    fantasma e il balancer sceglierebbe in base a lavoro mai eseguito."""
    if not provider or dry_run:
        return
    weight = cfg.get("balance", {}).get("weights", {}).get(kind, 1.0) * tier_multiplier(cfg, tier)

    with open_balance(write=True) as bal:           # contatore condiviso (routing)
        b = _apply_window(bal, provider, cfg)
        b.pop("reset", None)
        b["cost"] = round(b.get("cost", 0.0) + weight, 3)
        if tokens:                                  # dato REALE della CLI, quando disponibile
            b["tokens"] = int(b.get("tokens", 0)) + int(tokens)
        b["last_project"] = str(project)
        b["updated"] = int(time.time())

    with _STATE_LOCK:                               # totale di progetto (informativo)
        rec = state.setdefault("budget", {}).setdefault(
            provider, {"cost": 0.0, "window_start": int(time.time())})
        rec["cost"] = round(rec.get("cost", 0.0) + weight, 3)
        save_state(project, state)


def ordered_candidates(candidates: list, cfg: dict) -> list:
    """In routing 'balanced' ordina i candidati dal MENO carico al più carico,
    secondo il consumo CONDIVISO fra i progetti (la quota è dell'account).

    Se TUTTI i candidati hanno la quota calibrata si ordina per percentuale usata;
    altrimenti per costo grezzo. Mai mescolare le due scale nello stesso
    confronto: un provider non calibrato risulterebbe sempre il più scarico."""
    if cfg.get("routing", "static") != "balanced":
        return candidates
    # Un provider che ci ha DAVVERO dato rate-limit va in fondo qualunque cosa
    # dica la stima: è l'unico dato certo. (Il breaker lo esclude già a inizio
    # run, ma l'ordinamento non deve dipendere da chi lo chiama.)
    exhausted = exhausted_providers(cfg)
    calibrated = candidates and all(quota_used_pct(p, cfg) for p in candidates)
    key = (lambda p: load_factor(p, cfg)) if calibrated else (lambda p: current_cost(p, cfg))
    return sorted(candidates, key=lambda p: (p in exhausted, key(p)))


def role_weight_kind(step: str) -> str:
    """Che tipo di costo genera l'esecuzione principale di questo step."""
    return "review" if step == "reviewer" else "implement"


def last_implementer_from_state(state: dict):
    """Provider dell'ultima unità implementante registrata nello stato."""
    best_ts, best = -1, None
    for u in state.get("units", {}).values():
        if u.get("step") in IMPLEMENTING_STEPS and u.get("provider") and u.get("ts", 0) >= best_ts:
            best_ts, best = u.get("ts", 0), u["provider"]
    return best


def implementer_unit_of_issue(state: dict, issue_id: str):
    """Unità implementante di una issue, qualunque agente (developer o devops-engineer)."""
    for uid, u in state.get("units", {}).items():
        if u.get("step") in IMPLEMENTING_STEPS and uid.endswith(f"::{issue_id}"):
            return u
    return None


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
                   extra: str = "", resuming: bool = False, contract: bool = True,
                   cwd: Path = None) -> str:
    """Inietta l'input + handoff + contratto di esito; se si riprende, rilegge la nota."""
    body = agent.body.replace("{{ARGUMENTS}}", unit_input)
    hp = handoff_path(project, uid, cwd)           # dentro la working dir dell'agente
    body += HANDOFF_INSTRUCTION.format(handoff=hp)
    durable = handoff_path(project, uid)           # la nota vera vive nel progetto
    if resuming and durable.exists():
        body += ("\n\n[mentis] STAI RIPRENDENDO un lavoro interrotto. Nota di "
                 "progresso precedente:\n---\n" + durable.read_text() +
                 "\n---\nRiparti da lì: NON rifare ciò che è già sotto `Fatto:`.")
    if extra:
        body += "\n\n" + extra
    if contract:
        body += CONTRACT_INSTRUCTION
    return body


def parse_result(output: str):
    """Estrae l'ultimo blocco [[MENTIS-RESULT]]{json}[[/MENTIS-RESULT]] dall'output.
    Ritorna il dict, o None se assente/illeggibile (l'orchestratore ripiega su 'done')."""
    if not output:
        return None
    blocks = re.findall(r"\[\[MENTIS-RESULT\]\](.*?)\[\[/MENTIS-RESULT\]\]", output, re.DOTALL)
    if not blocks:
        return None
    for raw in reversed(blocks):                  # dall'ultimo: l'eco del prompt viene prima
        try:
            data = json.loads(raw.strip())
        except Exception:
            continue
        # scarta l'ESEMPIO che noi stessi mettiamo nel contratto (status letterale
        # "done|needs_input|failed"): se una CLI ri-echeggia il prompt, quel blocco
        # è JSON validissimo e verrebbe scambiato per l'esito vero.
        if isinstance(data, dict) and data.get("status") in ("done", "needs_input", "failed"):
            return data
    return None


def parse_usage_envelope(stdout: str):
    """Estrae (testo, token) dall'envelope JSON delle CLI, quando è attivo.

    Claude Code (`--output-format json`) restituisce UN oggetto con `result` (il
    testo) e `usage.input_tokens` / `usage.output_tokens`. Codex (`--json`)
    restituisce un JSONL di eventi, con `token_count` che porta i totali
    cumulativi. Se non riconosce nulla, ritorna (None, 0) e il chiamante ripiega
    sull'output grezzo: il conteggio token è un di più, non una dipendenza."""
    if not stdout or not stdout.strip():
        return None, 0
    txt = stdout.strip()
    # 1) oggetto singolo (Claude Code)
    if txt.startswith("{"):
        try:
            data = json.loads(txt)
            if isinstance(data, dict) and ("result" in data or "usage" in data):
                u = data.get("usage") or {}
                tok = int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)
                return (data.get("result") if isinstance(data.get("result"), str) else None), tok
        except Exception:
            pass
    # 2) JSONL di eventi (Codex): l'ultimo token_count porta i totali cumulativi
    text_parts, tokens = [], 0
    seen_json = False
    for line in txt.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        seen_json = True
        payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else ev
        if payload.get("type") == "token_count":
            tokens = sum(int(payload.get(k, 0) or 0)
                         for k in ("input_tokens", "output_tokens", "reasoning_tokens"))
        for key in ("text", "message", "delta", "result"):
            v = payload.get(key)
            if isinstance(v, str):
                text_parts.append(v)
    if seen_json:
        return ("\n".join(text_parts) if text_parts else None), tokens
    return None, 0


def timeout_for(cfg: dict, agent_name: str) -> int:
    """Timeout in secondi per una chiamata. Gli step che IMPLEMENTANO durano molto
    di più di quelli documentali (scrivono codice, installano, eseguono test):
    un timeout unico o li uccide a metà o è inutilmente lasco per gli altri."""
    rel = cfg.get("reliability", {})
    if agent_name in IMPLEMENTING_STEPS:
        return int(rel.get("timeout_implementing_seconds", 5400))
    return int(rel.get("timeout_seconds", 1800))


def clean_env() -> dict:
    """Ambiente per le CLI, senza le variabili che le farebbero fatturare ad API a
    consumo: il vincolo del progetto è usare SOLO gli abbonamenti."""
    env = dict(os.environ)
    for k in API_KEY_ENV_VARS:
        env.pop(k, None)
    return env


def preflight(cfg: dict) -> list:
    """Verifica PRIMA di spendere quota che ogni provider abilitato sia eseguibile:
    binario presente nel PATH e template `cmd` con i segnaposti attesi.
    Ritorna la lista dei problemi (vuota = tutto ok)."""
    import shutil as _sh
    problems = []
    for p in available_providers(cfg):
        pc = cfg["providers"].get(p, {})
        tmpl = pc.get("cmd", "")
        if "{prompt}" not in tmpl:
            problems.append(f"[providers.{p}] cmd non contiene il segnaposto {{prompt}}")
        try:
            binary = shlex.split(tmpl)[0] if tmpl else ""
        except ValueError:
            problems.append(f"[providers.{p}] cmd non è una riga di comando valida: {tmpl!r}")
            continue
        if not binary:
            problems.append(f"[providers.{p}] cmd vuoto")
        elif not _sh.which(binary) and not Path(binary).exists():
            problems.append(f"[providers.{p}] eseguibile '{binary}' non trovato nel PATH "
                            f"(installa la CLI e fai login con l'ABBONAMENTO, poi verifica "
                            f"i flag con `{binary} --help`)")
        for tok in re.findall(r"\{(\w+)\}", tmpl):
            if tok not in ("prompt", "model", "reasoning"):
                problems.append(f"[providers.{p}] segnaposto {{{tok}}} non supportato "
                                f"(ammessi: prompt, model, reasoning)")
    return problems


def run_on_provider(agent, provider, prompt, project, cfg, dry_run, cwd=None):
    prompt = NEUTRALITY_PREAMBLE + prompt          # contratto di neutralità in testa
    model, reasoning_value, level = resolve(agent, provider, cfg)
    template = cfg["providers"][provider]["cmd"]
    if "{reasoning}" not in template:              # provider senza flag reasoning (es. Claude):
        prompt += f"\n\n[mentis] Reasoning effort richiesto: {level}."   # passa il segnale nel prompt
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

    cap = int(cfg.get("reliability", {}).get("max_calls_per_run", 0) or 0)
    if cap:
        global _CALLS_THIS_RUN
        with _STATE_LOCK:
            _CALLS_THIS_RUN += 1
            n = _CALLS_THIS_RUN
        if n > cap:
            print(f"      ⛔ tetto di {cap} sessioni CLI per run raggiunto — mi fermo "
                  f"(alza max_calls_per_run in {CONFIG_PATH.name}, o rilancia per riprendere)")
            log_call(project, {**base, "error": "call-cap"})
            return {"ok": False, "rate_limited": False, "error": "call-cap", "cap": True}

    timeout = timeout_for(cfg, agent.name)
    try:
        r = subprocess.run(argv, cwd=str(cwd or project), capture_output=True,
                           text=True, timeout=timeout, env=clean_env())
    except subprocess.TimeoutExpired:
        print(f"      ✗ TIMEOUT dopo {timeout}s — l'unità può aver lasciato il lavoro a metà")
        log_call(project, {**base, "error": "timeout"}, prompt, "TIMEOUT")
        return {"ok": False, "rate_limited": False, "error": "timeout", "timeout": True}
    except FileNotFoundError:
        # CLI non installata / non nel PATH: è un errore di CONFIGURAZIONE, non un
        # errore transitorio — inutile ritentare o passare a un altro provider con
        # lo stesso problema. Prima causava il crash secco dell'orchestratore.
        print(f"      ✗ eseguibile non trovato: '{argv[0]}' non è nel PATH — "
              f"controlla la riga cmd di [providers.{provider}] in {CONFIG_PATH.name}")
        log_call(project, {**base, "error": "cli-not-found"}, prompt, "CLI NOT FOUND")
        return {"ok": False, "rate_limited": False, "error": "cli-not-found", "fatal": True}
    except Exception as e:
        print(f"      ✗ errore di esecuzione: {type(e).__name__}: {e}")
        log_call(project, {**base, "error": str(e)}, prompt, f"ERROR {e}")
        return {"ok": False, "rate_limited": False, "error": str(e)}
    tokens = 0
    if cfg["providers"][provider].get("usage_json"):
        text, tokens = parse_usage_envelope(r.stdout or "")
        if text is not None:                       # il testo vero sta dentro l'envelope
            r = subprocess.CompletedProcess(r.args, r.returncode, text, r.stderr)
        if tokens:
            print(f"      ⛁ {tokens:,} token consumati (dato della CLI, non stimato)")

    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    # rate-limit SOLO se la CLI è FALLITA (rc≠0): allora il messaggio di limite può
    # stare su stderr o su stdout (le CLI a subscription lo scrivono su entrambi).
    # Il vincolo rc≠0 basta a escludere il falso positivo "un TAD che *parla* di 429".
    rate_limited = (r.returncode != 0) and bool(RATE_LIMIT_PATTERNS.search(combined))
    ok = (r.returncode == 0)
    if r.stdout:
        print(r.stdout.rstrip())
    log_call(project, {**base, "chars_out": len(combined), "returncode": r.returncode,
                       "rate_limited": rate_limited, "ok": ok, "tokens": tokens}, prompt, combined)
    return {"ok": ok, "rate_limited": rate_limited, "tokens": tokens,
            "returncode": r.returncode, "output": combined}


def run_unit_with_fallback(agent, unit, unit_input, project, cfg, implementer,
                           dry_run, resuming, breaker, budget, state, force_provider=None,
                           workdir=None):
    all_cand = available_providers(cfg) or list(cfg["preference"])  # dry-run: piano
    # routing balanced: ordina dal meno carico; escludi i circuiti aperti
    candidates = ordered_candidates([p for p in all_cand if not breaker.get(p)], cfg)
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
    spent_kind = role_weight_kind(unit.step)   # quanto costa un tentativo di questa unità
    for prov in order:
        extra = ""
        if agent.name == "reviewer" and implementer:
            extra = f"Implemented-by: {implementer}\nReviewer-provider: {prov}"
        prompt = compose_prompt(agent, unit_input, project, unit.id, extra, resuming, cwd=workdir)
        res = run_on_provider(agent, prov, prompt, project, cfg, dry_run, cwd=workdir)
        sync_handoff(project, unit.id, workdir)     # la nota deve sopravvivere al worktree
        if res.get("ok"):
            # i token DEVONO risalire: le chiamate implementanti sono le più grosse,
            # perderle qui falsava il tetto osservato di un ordine di grandezza
            return {"provider": prov, "ok": True, "output": res.get("output", ""),
                    "tokens": res.get("tokens", 0)}

        if res.get("cap"):
            return {"provider": prov, "ok": False, "cap": True}

        if res.get("timeout"):
            # NIENTE retry automatico: la CLI può aver lasciato il progetto a metà e
            # rifare da capo costa un'altra unità di quota su un albero già modificato.
            # Un timeout ha consumato quanto una chiamata riuscita (spesso di più):
            # non addebitarlo farebbe sembrare SCARICO il provider che ha appena
            # bruciato 90 minuti di quota, e il balancer gli manderebbe altro lavoro.
            charge(state, prov, spent_kind, cfg, project, dry_run, agent.tier,
                   res.get("tokens", 0))
            print("    ⛔ timeout → nessun retry automatico: scalo a te "
                  "(la nota di handoff su disco resta, il rilancio riprende da lì)")
            return {"provider": prov, "ok": False, "timeout": True}

        if res.get("fatal"):
            # errore di CONFIGURAZIONE (CLI assente): non è transitorio e non migliora
            # ritentando — escludo il provider e passo al prossimo senza bruciare budget.
            breaker[prov] = True
            print(f"    ✗ {prov} non eseguibile (configurazione) → escluso per questo run")
            continue

        # --- layered fallback ---
        if res.get("rate_limited"):
            mark_exhausted(prov, cfg, project)           # lo sapranno anche gli altri progetti
            if rel.get("circuit_open_for_run", True):
                breaker[prov] = True                     # CIRCUITO APERTO per il resto del run
            print(f"    ↯ {prov} esaurito/limite → circuito APERTO, fallback")
            print("      (contesto durevole su disco preservato; chi subentra legge l'handoff)")
            resuming = True                              # chi subentra riprende dall'handoff
        else:
            # errore transitorio: la chiamata è comunque PARTITA e ha consumato quota
            # (a differenza di un rate-limit, respinto alla porta e quindi gratis).
            charge(state, prov, spent_kind, cfg, project, dry_run, agent.tier,
                   res.get("tokens", 0))
            # backoff + 1 retry sullo stesso provider
            wait = rel.get("backoff_seconds", 2)
            if budget["left"] > 0 and not dry_run and wait:
                print(f"    ⧖ {prov} errore transitorio → backoff {wait}s e 1 retry")
                time.sleep(wait); budget["left"] -= 1
                retry = run_on_provider(agent, prov, prompt, project, cfg, dry_run, cwd=workdir)
                if retry.get("ok"):
                    # l'output DEVE essere propagato: è lì che vive il contratto
                    # [[MENTIS-RESULT]] (needs_input / failed dichiarati dall'agente)
                    # (l'addebito del tentativo riuscito lo fa il chiamante)
                    return {"provider": prov, "ok": True, "output": retry.get("output", ""),
                            "tokens": retry.get("tokens", 0)}
                if not retry.get("rate_limited") and not retry.get("fatal"):
                    charge(state, prov, spent_kind, cfg, project, dry_run, agent.tier,
                           retry.get("tokens", 0))
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


def reflection_pass(agent, unit, author_provider, project, cfg, dry_run, breaker, state, cwd=None):
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
    res = run_on_provider(agent, author_provider, prompt, project, cfg, dry_run, cwd=cwd)
    charge(state, author_provider, "reflect", cfg, project, dry_run, agent.tier, res.get("tokens", 0))


def parse_verdict(output, dry_run):
    """Robusto: cerca il tag `VERDICT: ...`; poi i token ovunque; ambiguo → NEEDS WORK."""
    if dry_run:
        return "APPROVED", ""
    text = output or ""
    up = text.upper()
    tags = re.findall(r"VERDICT\s*:\s*(APPROVED|NEEDS[ _]WORK)", up)  # 1) tag esplicito
    if tags:
        # l'ULTIMO, non il primo: le istruzioni che chiediamo noi contengono già
        # `VERDICT: APPROVED` come esempio, e una CLI che ri-echeggia il prompt (o un
        # modello che cita il formato prima di decidere) darebbe un APPROVED falso.
        v = "NEEDS WORK" if "NEEDS" in tags[-1] else "APPROVED"
        return v, (text if v == "NEEDS WORK" else "")
    has_needs = ("NEEDS WORK" in up) or ("NEEDS_WORK" in up)          # 2) token ovunque
    has_appr = "APPROVED" in up
    if has_needs and not has_appr:
        return "NEEDS WORK", text
    if has_appr and not has_needs:
        return "APPROVED", ""
    return "NEEDS WORK", text                                         # 3) ambiguo → conservativo


def evaluator_loop(agent, unit, author_provider, project, cfg, dry_run, breaker, state, cwd=None):
    """Evaluator su provider ≠ autore (il più scarico); loop optimizer fino a cap, poi escalation."""
    cap = cfg.get("quality", {}).get("loop_cap", 2)
    artifact = EXPECTED_ARTIFACT.get(unit.step, "l'artefatto")
    rubric = rubric_for(agent.name, cfg)
    all_cand = available_providers(cfg) or list(cfg["preference"])
    indep = ordered_candidates([p for p in all_cand if p != author_provider and not breaker.get(p)],
                               cfg)
    if not indep:
        # policy [review].on_no_alternative — con UN SOLO provider attivo (caso normale
        # finché non hai entrambi gli abbonamenti) 'block' fermerebbe la pipeline alla
        # prima unità: 'warn' degrada a reflection e prosegue, marcando che la review
        # NON è indipendente. Non auto-approva in silenzio: lo stato lo registra.
        policy = cfg.get("review", {}).get("on_no_alternative", "warn")
        print(f"    ⚠ EVALUATOR: nessun provider indipendente (autore={author_provider}, gli altri "
              f"giù/in circuito) → degrado a reflection-only [policy: {policy}].")
        reflection_pass(agent, unit, author_provider, project, cfg, dry_run, breaker, state, cwd)
        if policy == "block":
            return {"status": "escalated", "reason": "no-independent-provider"}
        print("      ⚠ anti-bias NON garantito su questa unità (review non indipendente).")
        return {"status": "approved-not-independent", "reason": "no-independent-provider"}
    evaluator = indep[0]                          # evaluator = più scarico tra gli indipendenti

    for rnd in range(cap + 1):
        print(f"    ⇄ evaluator @ {evaluator} (autore={author_provider}) — valutazione {rnd + 1}")
        eval_prompt = (f"[mentis evaluator] Sei un valutatore INDIPENDENTE, su un modello DIVERSO "
                       f"dall'autore. Valuta criticamente {artifact} dello step '{unit.step}'. "
                       f"Criteri: {rubric}. Se ci sono problemi, elencali numerati e azionabili. "
                       f"Concludi con UNA riga esattamente in questo formato: "
                       f"`VERDICT: APPROVED` oppure `VERDICT: NEEDS WORK`.")
        res = run_on_provider(agent, evaluator, eval_prompt, project, cfg, dry_run, cwd=cwd)
        if not res.get("ok") and not dry_run:                # CLI fallita/limite ≠ verdetto
            if res.get("rate_limited"):
                breaker[evaluator] = True
                mark_exhausted(evaluator, cfg, project)
            print(f"    ⚠ evaluator {evaluator} fallito/limite → escalation (verdetto non ottenibile)")
            return {"status": "escalated", "reason": "evaluator-call-failed"}
        charge(state, evaluator, "evaluate", cfg, project, dry_run, agent.tier, res.get("tokens", 0))
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
        ores = run_on_provider(agent, author_provider, opt_prompt, project, cfg, dry_run, cwd=cwd)
        if not ores.get("ok") and not dry_run:
            if ores.get("rate_limited"):
                breaker[author_provider] = True
                mark_exhausted(author_provider, cfg, project)
            print(f"    ⚠ optimizer {author_provider} fallito/limite → escalation")
            return {"status": "escalated", "reason": "optimizer-call-failed"}
        charge(state, author_provider, "optimize", cfg, project, dry_run, agent.tier, ores.get("tokens", 0))
    return {"status": "approved"}


def apply_quality(agent, unit, author_provider, project, cfg, level, dry_run, breaker, state, cwd=None):
    if level in ("reflect", "full"):
        reflection_pass(agent, unit, author_provider, project, cfg, dry_run, breaker, state, cwd)
    if level in ("evaluate", "full"):
        return evaluator_loop(agent, unit, author_provider, project, cfg, dry_run, breaker, state, cwd)
    return {"status": "approved"}


def reviewer_loop(reviewer_agent, unit, implementer, project, cfg, dry_run, breaker, state,
                  user_input="", rework_agent="developer"):
    """Lo step reviewer come evaluator-optimizer: review → se NEEDS WORK ri-dispaccia
    l'agente che ha implementato (developer o devops-engineer) → re-review, fino a cap."""
    cap = cfg.get("quality", {}).get("loop_cap", 2)
    usable = ordered_candidates([p for p in (available_providers(cfg) or list(cfg["preference"]))
                                 if not breaker.get(p)], cfg)
    indep = [p for p in usable if p != implementer]
    independent = True
    if not indep:
        # policy [review].on_no_alternative: 'block' rifiuta la review, 'warn' procede
        # dichiarando che NON è indipendente (con un solo abbonamento attivo è l'unico
        # modo di far avanzare la pipeline invece di fermarla su ogni issue).
        policy = cfg.get("review", {}).get("on_no_alternative", "warn")
        if policy == "block" or not usable:
            print(f"    ⚠ REVIEWER: nessun provider indipendente (implementer={implementer}) "
                  f"→ escalation [policy: {policy}]")
            return {"status": "escalated", "reason": "no-independent-provider", "provider": None}
        independent = False
        indep = usable
        print(f"    ⚠ REVIEWER: un solo provider disponibile → review NON indipendente "
              f"(anti-bias non garantito su questa unità) [policy: warn]")
    rp = indep[0]                                  # reviewer = indipendente più scarico
    dev_agent = load_agent(rework_agent)           # rework all'agente giusto (fix-CI dispatch)
    issue_txt = f"Issue: {unit.issue['id']} — {unit.issue['title']}\n" if unit.issue else ""
    arg_txt = f"Da revisionare: {user_input}\n" if user_input else ""   # PR/branch passato dall'utente

    for rnd in range(cap + 1):
        print(f"    ⇄ review @ {rp} (implementer={implementer}) — giro {rnd + 1}")
        rv_input = (f"{arg_txt}{issue_txt}Rivedi la PR/il codice di questo lavoro come reviewer "
                    f"indipendente. Concludi con UNA riga: `VERDICT: APPROVED` oppure "
                    f"`VERDICT: NEEDS WORK` (+ problemi numerati se NEEDS WORK).")
        prompt = compose_prompt(reviewer_agent, rv_input, project, unit.id,
                                extra=f"Implemented-by: {implementer}\nReviewer-provider: {rp}",
                                contract=False)
        res = run_on_provider(reviewer_agent, rp, prompt, project, cfg, dry_run)
        if not res.get("ok") and not dry_run:
            if res.get("rate_limited"):
                breaker[rp] = True
                mark_exhausted(rp, cfg, project)
            print(f"    ⚠ reviewer {rp} fallito/limite → escalation (verdetto non ottenibile)")
            return {"status": "escalated", "reason": "reviewer-call-failed", "provider": rp}
        charge(state, rp, "review", cfg, project, dry_run, reviewer_agent.tier, res.get("tokens", 0))
        verdict, critique = parse_verdict(res.get("output", ""), dry_run)
        if verdict == "APPROVED":
            print(f"    ✓ review APPROVED ({rp}" +
                  (f" ≠ {implementer}, indipendente)" if independent else ", NON indipendente)"))
            return {"status": "approved" if independent else "approved-not-independent",
                    "provider": rp}
        if rnd >= cap:
            print(f"    ⛔ ancora NEEDS WORK dopo {cap} rework → STOP, escalation all'utente")
            return {"status": "escalated", "reason": "max-rounds", "provider": rp}
        if not implementer:
            # senza implementer noto non c'è nessuno a cui dispacciare il rework:
            # prima si arrivava a resolve(agent, None, cfg) → KeyError e crash del run.
            print("    ⛔ NEEDS WORK ma l'implementer è sconosciuto (nessuno stato per questa "
                  "unità: review lanciata a sé stante) → escalation, rework non dispacciabile")
            return {"status": "escalated", "reason": "unknown-implementer", "provider": rp}
        if breaker.get(implementer):
            print(f"    ⚠ implementer {implementer} non disponibile per il rework → escalation")
            return {"status": "escalated", "reason": "implementer-unavailable", "provider": rp}
        print(f"    ✎ rework @ {implementer} ({rework_agent}): giro {rnd + 1}/{cap}")
        fix_input = (f"{issue_txt}Un reviewer indipendente ha chiesto modifiche:\n{critique}\n"
                     f"Applica le correzioni al codice, senza introdurre regressioni.")
        dev_prompt = compose_prompt(dev_agent, fix_input, project, unit.id, resuming=True,
                                    contract=False)
        rres = run_on_provider(dev_agent, implementer, dev_prompt, project, cfg, dry_run)
        if not rres.get("ok") and not dry_run:
            if rres.get("rate_limited"):
                breaker[implementer] = True
                mark_exhausted(implementer, cfg, project)
            print(f"    ⚠ rework {implementer} fallito/limite → escalation")
            return {"status": "escalated", "reason": "rework-call-failed", "provider": rp}
        charge(state, implementer, "optimize", cfg, project, dry_run, dev_agent.tier, rres.get("tokens", 0))
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

    import shutil as _sh
    for p in providers:
        keywords = cfg.get("tier_keywords", {}).get(p, {})
        current_map = cfg.get("model_map", {}).get(p, {})
        print(f"── provider: {p}")
        pc = cfg["providers"].get(p, {})
        try:                                       # la CLI c'è? è il primo controllo utile
            binary = shlex.split(pc.get("cmd", ""))[0] if pc.get("cmd") else ""
        except ValueError:
            binary = ""
        if binary:
            found = _sh.which(binary) or (binary if Path(binary).exists() else None)
            print(f"   CLI: {binary} → {found or '✗ NON trovata nel PATH'}"
                  + ("" if found else "  (installala e fai login con l'ABBONAMENTO)"))
        print(f"   abilitato: {'sì' if pc.get('enabled') else 'no'}")
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
def cmd_balance(cfg, reset: bool, provider: str, add: str):
    """Ispeziona/corregge il bilancio CONDIVISO fra i progetti."""
    if reset:
        with open_balance(write=True) as bal:
            targets = [provider] if provider else list(bal["providers"])
            for p in targets:
                old = bal["providers"].get(p, {})
                fresh = {"cost": 0.0, "window_start": int(time.time())}
                # il TETTO osservato è conoscenza acquisita, non stato del run:
                # azzerare il consumo non deve farci disimparare quanto vale il 100%
                for k in ("limit_observed", "limit_samples",
                          "limit_tokens", "limit_tokens_samples"):
                    if old.get(k) is not None:
                        fresh[k] = old[k]
                bal["providers"][p] = fresh
            print(f"✔ consumo azzerato: {', '.join(targets) or '— niente da azzerare —'} "
                  f"(calibrazione del tetto conservata)")
    if add:
        try:
            p, val = add.split("=", 1)
            amount = float(val)
        except ValueError:
            print("errore: formato atteso --add provider=numero (es. --add claude=5)")
            sys.exit(2)
        with open_balance(write=True) as bal:
            b = bal["providers"].setdefault(p.strip(),
                                            {"cost": 0.0, "window_start": int(time.time())})
            b.pop("exhausted_at", None)
            b["cost"] = round(b.get("cost", 0.0) + amount, 3)
            print(f"✔ {p.strip()}: +{amount} → {b['cost']}")

    print(f"\n⚖  bilancio condiviso — {balance_path()}")
    with open_balance(write=False) as bal:
        providers = dict(bal["providers"])
    if not providers:
        print("   (vuoto: nessun consumo registrato)")
        return
    for p in sorted(providers, key=lambda x: cfg.get("preference", []).index(x)
                    if x in cfg.get("preference", []) else 99):
        b = providers[p]
        cost = current_cost(p, cfg)
        left = max(0, _window_hours(cfg, p) * 3600 - (time.time() - b.get("window_start", 0))) / 3600
        got = quota_used_pct(p, cfg)
        if got is None:
            usato = "quota usata: IGNOTA — il tetto non è mai stato osservato"
        else:
            pct, unit = got
            barre = int(min(pct, 100) / 5)
            if unit == "token":
                den = f"tetto {b['limit_tokens']:,} token su {b.get('limit_tokens_samples', 0)} rilevazioni"
            else:
                den = (f"tetto ≈{b['limit_observed']} in unità stimate su "
                       f"{b.get('limit_samples', 0)} rilevazioni — ordine di grandezza")
            usato = f"[{'█' * barre}{'·' * (20 - barre)}] {pct:5.1f}% usato ({den})"
        flag = ""
        if b.get("exhausted_at"):
            ex_left = _window_hours(cfg, p) * 3600 - (time.time() - b["exhausted_at"])
            flag = (f"  ⛔ ESAURITO, ricarica fra ~{ex_left / 3600:.1f}h"
                    if ex_left > 0 else "  ✓ ricaricato")
        print(f"   {p:8} costo≈{cost:6.1f}   {usato}{flag}")
        print(f"            ~{left:.1f}h alla ricarica della finestra"
              + (f"   ·   ultimo run: {b['last_project']}" if b.get("last_project") else ""))
    print("\n   Questo contatore è di tutti i progetti insieme, perché la quota è dell'account.")
    print("   La percentuale compare solo DOPO il primo rate-limit reale: è quello a dire")
    print("   quanto vale il 100%. Nessuna CLI a subscription espone la quota residua")
    print("   (`/usage` in Claude Code e `/status` in Codex sono solo interattivi), quindi")
    print("   il tetto si impara sbattendoci contro una volta — poi la stima è tarata.")
    print("   Il consumo fatto FUORI da mentis (Claude Code interattivo, claude.ai) non è")
    print("   visibile qui: emerge come un rate-limit anticipato. `--add` lo anticipa a mano.")


def cmd_status(project, cfg):
    from collections import Counter
    state = load_state(project)
    print(f"🧭 mentis status — progetto: {project}")
    if state.get("command"):
        print(f"   ultimo comando: {state['command']}")
    # consumo OSSERVATO dal log: è il dato vero, e serve a ricalibrare i pesi
    observed, calls_by_prov = {}, {}
    logfile = project / ".mentis" / "logs" / "calls.jsonl"
    if logfile.exists():
        try:
            for line in logfile.read_text().splitlines():
                if not line.strip():
                    continue
                e = json.loads(line)
                pv = e.get("provider")
                if not pv or e.get("dry_run"):
                    continue
                observed[pv] = observed.get(pv, 0) + e.get("chars_in", 0) + e.get("chars_out", 0)
                calls_by_prov[pv] = calls_by_prov.get(pv, 0) + 1
        except Exception:
            pass

    with open_balance(write=False) as bal:
        shared = {p: dict(r) for p, r in bal["providers"].items()}
    if shared:
        print(f"   quota consumata — CONDIVISA fra tutti i progetti ({balance_path()}):")
        tot_obs = sum(observed.values())
        for p in cfg.get("preference", list(shared)):
            b = shared.get(p)
            if not b:
                continue
            cost = current_cost(p, cfg)
            remaining = max(0, _window_hours(cfg, p) * 3600
                            - (time.time() - b.get("window_start", 0))) / 3600
            mine = state.get("budget", {}).get(p, {}).get("cost", 0.0)
            got = quota_used_pct(p, cfg)
            quota = (f"{got[0]:.0f}% della quota [{got[1]}]" if got else "quota residua ignota")
            print(f"     {p:8} totale≈{cost:6.1f} → {quota}   di cui questo progetto ≈{mine:.1f}"
                  f"   ~{remaining:.1f}h alla ricarica")
        for p in cfg.get("preference", list(shared)):
            obs = observed.get(p, 0)
            if not obs:
                continue
            share = f"{100 * obs / tot_obs:.0f}%" if tot_obs else "—"
            print(f"     {p:8} in questo progetto: {calls_by_prov.get(p, 0):>3} chiamate, "
                  f"{obs / 1000:.0f}k char ({share}) [OSSERVATO]")
        if tot_obs:
            print("     ↳ se stima e osservato divergono molto, ricalibra "
                  "[balance.weights]/[balance.tier] sui dati osservati.")
        print("     ↳ la quota che consumi FUORI da mentis (Claude Code interattivo, "
              "claude.ai) non è visibile qui: registrala con `mentis balance --add`.")
    units = state.get("units", {})
    if units:
        c = Counter(u.get("status", "?") for u in units.values())
        print("   unità:", "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
        for tag, mark in (("awaiting_input", "⏸ in attesa di risposte (rispondi in .mentis/answers/)"),
                          ("escalated", "⛔ escalation (attendono te)"), ("failed", "✗ fallite")):
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
def process_unit(ctx, unit, force_provider=None, workdir=None):
    project, cfg, state = ctx["project"], ctx["cfg"], ctx["state"]
    breaker, budget, dry_run = ctx["breaker"], ctx["budget"], ctx["dry_run"]
    run_cwd = workdir or project           # worktree isolato per le unità parallele, altrimenti il progetto
    agent = load_agent(unit.step)
    unit_input = ctx["user_input"]
    branch = ""
    if unit.issue:
        # `Branch:` è un campo che developer/devops si aspettano negli argomenti:
        # senza, ognuno improvvisa un nome (o lavora sul branch sbagliato).
        branch = f"mentis/{_safe(unit.id)}" if workdir else branch_for(unit)
        unit_input = (f"Issue: {unit.issue['id']} — {unit.issue['title']}\n"
                      f"Label: {unit.issue.get('label', 'Backend')}\n"
                      f"Branch: {branch}\n{unit_input}")
    if unit.step in IMPLEMENTING_STEPS and not dry_run:
        unit_input += "\n" + repo_note(project, branch, bool(workdir))
    h = input_hash(agent, unit_input, project)     # dependency-aware (solo artefatti a MONTE)
    prev = state["units"].get(unit.id)
    label = unit.id + (f"  ({unit.issue['title']})" if unit.issue else "")
    print(f"▸ {label}  (tier={agent.tier}, reasoning={agent.reasoning})")

    # RESUME: salta se già completata, input invariato e artefatto presente
    if (prev and prev.get("status") == "done" and prev.get("input_hash") == h
            and artifact_ok(project, unit.step, dry_run)):
        print(f"    ⤿ già completata su {prev.get('provider')} — salto (resume)")
        return {"status": "skipped", "provider": prev.get("provider")}

    # HITL: unità che aveva chiesto input umano — riprende SOLO se ci sono le risposte
    # Le risposte si iniettano ogni volta che esistono e non sono ancora state
    # consumate — non solo se l'unità è ferma in `awaiting_input`. Se un tentativo
    # fallisce DOPO l'iniezione, lo stato non torna `awaiting_input` e legandosi a
    # quello le risposte dell'utente resterebbero orfane per sempre.
    answers_consumed = None
    ap = answers_path(project, unit.id)
    if ap.exists() and ap.read_text().strip():
        unit_input += "\n\n[Risposte dell'utente alle tue domande]\n" + ap.read_text().strip()
        answers_consumed = ap
        print(f"    ✍ risposte trovate ({ap.name}) — riprendo con le risposte iniettate")
    elif prev and prev.get("status") == "awaiting_input":
        print(f"    ⏸ in attesa di risposte: rispondi in {ap} e rilancia")
        return {"status": "awaiting_input", "provider": prev.get("provider")}

    resuming = bool(prev and prev.get("status") == "running")
    if resuming:
        print("    ↻ unità interrotta in un run precedente — riprendo dalla nota di handoff")
    if prev and prev.get("status") == "escalated":
        # l'utente può aver corretto a mano ciò che aveva bloccato l'unità: rifarla
        # da zero sovrascriverebbe quelle correzioni senza accorgersene.
        unit_input += ("\n\n[mentis] Questa unità era stata SCALATA all'utente e viene ora "
                       "rieseguita. L'utente può aver già corretto qualcosa a mano: leggi lo "
                       "stato attuale dei file PRIMA di riscriverli e conserva le sue modifiche.")

    # implementer per l'anti-bias del reviewer (per-issue dallo stato, qualunque agente)
    implementer = None
    rework_agent = "developer"
    if unit.step == "reviewer":
        iu = implementer_unit_of_issue(state, unit.issue["id"]) if unit.issue else None
        if iu:
            implementer = iu.get("provider")
            rework_agent = iu.get("step", "developer")   # rework all'agente che ha implementato
        implementer = implementer or last_implementer_from_state(state)

    pre_head = git_head(run_cwd) if not dry_run else None
    pre_dirty = git_dirty_set(run_cwd) if not dry_run else set()   # baseline: un tree già
    started_at = time.time()                                       # sporco non vale come output
    mark_unit(project, state, unit.id, status="running", step=unit.step, input_hash=h)

    # --- REVIEWER: loop review→rework (evaluator-optimizer) ---
    if unit.step == "reviewer":
        rev = reviewer_loop(agent, unit, implementer, project, cfg, dry_run, breaker, state,
                            user_input=ctx["user_input"], rework_agent=rework_agent)
        status = "escalated" if rev["status"] == "escalated" else ("planned" if dry_run else "done")
        mark_unit(project, state, unit.id, status=status, provider=rev.get("provider"),
                  quality_status=rev["status"])
        if rev["status"] == "escalated":
            print(f"    ⛔ ESCALATION ({rev.get('reason')}) su '{unit.id}' — mi fermo e scalo a te.")
        return {"status": status, "provider": rev.get("provider")}

    # --- step normale: implementazione + quality ---
    res = run_unit_with_fallback(agent, unit, unit_input, project, cfg, implementer,
                                 dry_run, resuming, breaker, budget, state, force_provider, workdir)
    if res.get("ok"):
        charge(state, res.get("provider"), role_weight_kind(unit.step), cfg, project, dry_run,
               agent.tier, res.get("tokens", 0))

    # --- contratto di esito strutturato dichiarato dall'agente ---
    result = parse_result(res.get("output", "")) or {}
    rstatus = result.get("status")
    if res.get("ok") and rstatus == "needs_input":
        qs = result.get("questions") or []
        qp = questions_path(project, unit.id)
        qp.parent.mkdir(parents=True, exist_ok=True)
        qp.write_text(f"# Domande di {unit.id}\n\n" + "\n".join(f"- {q}" for q in qs) +
                      f"\n\nScrivi le risposte in `{answers_path(project, unit.id).name}` "
                      f"(cartella .mentis/answers/) e rilancia lo stesso comando per riprendere.\n")
        mark_unit(project, state, unit.id, status="awaiting_input",
                  provider=res.get("provider"), questions=qs)
        print(f"    ⏸ {unit.id} richiede input umano ({len(qs)} domande) → {qp}")
        return {"status": "awaiting_input", "provider": res.get("provider")}
    if res.get("ok") and rstatus == "failed":
        mark_unit(project, state, unit.id, status="failed", provider=res.get("provider"),
                  reason=result.get("note", "l'agente ha dichiarato failed"))
        print(f"    ✗ {unit.id}: l'agente ha dichiarato failed ({result.get('note', '')})")
        return {"status": "failed", "provider": res.get("provider")}

    author = res.get("provider")
    level = quality_level(unit.step, cfg, ctx["quality_override"])
    if res.get("ok") and level != "off":
        print(f"    ◇ quality: {level}")
        qres = apply_quality(agent, unit, author, project, cfg, level, dry_run, breaker, state, run_cwd)
        mark_unit(project, state, unit.id, quality=level, quality_status=qres["status"])
        if qres.get("status") == "escalated":
            print(f"    ⛔ ESCALATION ({qres.get('reason')}) su '{unit.id}' — mi fermo e scalo a te.")
            mark_unit(project, state, unit.id, status="escalated")
            return {"status": "escalated", "provider": author}

    produced = produced_output(run_cwd, unit, dry_run, pre_head, started_at, pre_dirty)
    if dry_run:
        mark_unit(project, state, unit.id, status="planned", provider=author)
        return {"status": "planned", "provider": author}
    if res.get("ok") and produced:
        if answers_consumed:                       # risposte usate → archiviate, così un
            try:                                   # rilancio non le ri-inietta su domande nuove
                answers_consumed.rename(answers_consumed.with_suffix(".md.used"))
            except Exception:
                pass
        mark_unit(project, state, unit.id, status="done", provider=author,
                  artifact=EXPECTED_ARTIFACT.get(unit.step))
        return {"status": "done", "provider": author}
    reason = "artefatto atteso assente" if res.get("ok") and not produced else "esecuzione fallita"
    mark_unit(project, state, unit.id, status="failed", provider=author, reason=reason)
    print(f"    ✗ unità fallita ({reason})")
    return {"status": "failed", "provider": author, "reason": reason}


def build_waves(units, cfg, parallel):
    """Raggruppa in wave parallele le unità implementanti indipendenti (deps non nella
    wave). Comprende `devops-engineer`: dopo il routing per label una issue DevOps è
    un'unità implementante come le altre, e limitare le wave al solo `developer`
    spezzava ogni wave in cui capitasse in mezzo."""
    if not parallel:
        return [[u] for u in units]
    nprov = len(cfg.get("preference", [])) or 1
    waves, i = [], 0
    while i < len(units):
        u = units[i]
        if u.step not in IMPLEMENTING_STEPS or not u.issue:
            waves.append([u]); i += 1; continue
        wave, ids, j = [u], {u.issue["id"]}, i + 1
        while (j < len(units) and len(wave) < nprov and units[j].step in IMPLEMENTING_STEPS
               and units[j].issue and not (set(units[j].issue.get("deps", [])) & ids)):
            wave.append(units[j]); ids.add(units[j].issue["id"]); j += 1
        waves.append(wave); i = j
    return waves


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(prog="mentis", add_help=True)
    ap.add_argument("command", help="pipeline: build | tad | bad | review | mvp | doctor | status | balance")
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
    # opzioni del comando `balance` (bilancio condiviso fra progetti)
    ap.add_argument("--reset", action="store_true", help="balance: azzera il consumo registrato")
    ap.add_argument("--add", help="balance: registra a mano consumo esterno, es. claude=5")
    args = ap.parse_args()

    cfg = load_toml(CONFIG_PATH)

    if args.command == "doctor":
        cmd_doctor(cfg, args.provider, args.models, args.apply)
        return

    if args.command == "balance":
        cmd_balance(cfg, args.reset, args.provider, args.add)
        return

    project = Path(args.project).resolve()
    user_input = " ".join(args.description)

    if args.command == "status":
        cmd_status(project, cfg)
        return

    if not project.is_dir():
        # senza questo, un typo in --project diventa silenziosamente un progetto nuovo
        # (le prime scritture creano la cartella) e ci gira l'intera pipeline.
        print(f"errore: la cartella del progetto non esiste: {project}")
        print("       creala prima (es. mkdir -p) — mentis non inventa un progetto da un path errato.")
        sys.exit(2)

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

    if not dry_run:
        problems = preflight(cfg)      # fail-fast PRIMA di spendere quota
        if problems:
            print("✗ preflight fallito — non ho eseguito nulla:")
            for pb in problems:
                print(f"   • {pb}")
            print("\n  Correggi config/mentis.toml (o installa la CLI e fai login con "
                  "l'abbonamento), poi rilancia. `--dry-run` mostra comunque il piano.")
            sys.exit(2)

    if args.fresh:
        # risposte HITL e note di handoff appartengono al run azzerato: lasciarle lì
        # significa iniettarle su domande che non sono più le stesse.
        for sub in ("answers", "handoff"):
            d = project / ".mentis" / sub
            if d.is_dir() and any(d.iterdir()):
                old = project / ".mentis" / f"{sub}.old"
                try:
                    if old.exists():
                        shutil.rmtree(old)
                    d.rename(old)
                    print(f"   ⌫ --fresh: {sub}/ del run precedente archiviato in {sub}.old/")
                except Exception:
                    pass

    state = {"units": {}} if args.fresh else load_state(project)
    state["command"] = args.command
    migrate_project_budget(project, state)   # contatore per-progetto → condiviso (una volta sola)
    ensure_gitignore(project)          # .mentis/ non deve finire nei commit del progetto-target
    if not dry_run:
        ensure_git_repo(project)       # worktree, verifica output e flussi branch lo assumono
    fanout = args.command != "review"  # una review mirata non deve esplodere in N review
    print()

    # --- COMPARE: challenge, nessuno stato/resume (è un confronto, non un build) ---
    if mode == "compare":
        if len(steps) > 1:
            # ogni step gira in una cartella isolata per provider: il passo 2 non
            # vede l'artefatto del passo 1, quindi la pipeline non si tiene in piedi.
            print(f"⚠ --compare è pensato per pipeline a UNO step (tad, bad, mvp): '{args.command}' "
                  f"ne ha {len(steps)} e ogni step gira in una cartella isolata, senza vedere "
                  f"gli artefatti del precedente.\n"
                  f"  Gli output sono confrontabili solo step per step: per un confronto "
                  f"sensato usa `mentis tad ... --compare`.\n")
        for step in steps:
            for unit in expand_step(step, project, fanout):
                agent = load_agent(unit.step)
                print(f"▸ {unit.id}  (tier={agent.tier}, reasoning={agent.reasoning})")
                run_unit_compare(agent, user_input, project, cfg, dry_run, unit.id)
                print()
        print("✔ fine (compare).")
        return

    # --- FALLBACK pipeline: resumability + checkpoint + quality + balancing + wave ---
    # Un provider che ci ha dato rate-limit di recente (anche in un ALTRO progetto)
    # parte già escluso: è l'unica informazione certa che abbiamo sulla quota.
    breaker = {}
    if not dry_run:
        for p, hours_left in exhausted_providers(cfg).items():
            if p in (enabled or []):
                breaker[p] = True
                print(f"   ⓘ {p}: quota esaurita di recente (anche fuori da questo progetto) — "
                      f"escluso, ricarica stimata fra ~{hours_left:.1f}h")
        if enabled and all(breaker.get(p) for p in enabled):
            print("\n⛔ tutti i provider attivi risultano a quota esaurita: non parto per non "
                  "sprecare tentativi.\n   Se la quota è già tornata, azzera con: mentis balance --reset\n")
            sys.exit(3)

    ctx = {"project": project, "cfg": cfg, "state": state, "breaker": breaker,
           "budget": {"left": cfg.get("reliability", {}).get("retry_budget", 6)},
           "dry_run": dry_run, "user_input": user_input, "quality_override": args.quality}

    parallel = args.parallel and not dry_run and len(available_providers(cfg)) >= 2
    if args.parallel and not dry_run:
        print("   ⧉ --parallel: le unità implementanti girano in git worktree ISOLATI "
              "(branch mentis/{unit}); niente collisione su .git/index.lock.\n")

    stop = False
    for step in steps:                              # LAZY: espando lo step ADESSO
        if stop:
            break
        step_units = expand_step(step, project, fanout)   # fan-out letto DOPO che il planner ha scritto il DEPS
        if len(step_units) > 1:
            print(f"   ⤷ {step}: fan-out in {len(step_units)} unità (per-issue)")
        for wave in build_waves(step_units, cfg, args.parallel):
            if stop:
                break
            if len(wave) > 1:
                print(f"⛓ wave {'parallela' if parallel else '(sequenziale)'}: {[u.id for u in wave]}")
            if parallel and len(wave) > 1:
                ordered = ordered_candidates(available_providers(cfg), cfg)

                def _run_isolated(u, prov):
                    wt = make_worktree(project, u.id) if u.step in IMPLEMENTING_STEPS else None
                    if wt:
                        print(f"    ⧉ {u.id}: worktree isolato ({wt.name}) — niente collisione git")
                    res = None
                    try:
                        res = process_unit(ctx, u, prov, workdir=wt)
                        return res
                    finally:
                        remove_worktree(project, wt, u.id)   # il branch resta; il worktree no
                        if wt and res and res.get("status") == "done":
                            # senza questo il codice resta su mentis/{unit} e gli step a
                            # valle (qa, reviewer, docs) girano su un albero che non lo ha
                            merge_unit_branch(project, u.id)

                with ThreadPoolExecutor(max_workers=len(wave)) as ex:
                    futs = {ex.submit(_run_isolated, u, ordered[k % len(ordered)]): u
                            for k, u in enumerate(wave)}
                    results = {futs[f].id: f.result() for f in futs}
            else:
                results = {}
                for u in wave:
                    results[u.id] = process_unit(ctx, u)
                    if results[u.id]["status"] in ("failed", "escalated", "awaiting_input"):
                        break
            for u in wave:
                r = results.get(u.id)
                if r and r["status"] in ("failed", "escalated", "awaiting_input"):
                    if r["status"] == "awaiting_input":
                        print(f"    ⏸ PAUSA su {u.id}: rispondi in .mentis/answers/ e rilancia lo stesso comando.")
                    else:
                        print(f"    ⛔ STOP su {u.id} ({r['status']}) — stato in {state_path(project)};")
                        print(f"      rilancia lo stesso comando per riprendere (le 'done' si saltano), --fresh per azzerare.")
                    stop = True
            print()

    if cfg.get("routing") == "balanced" and not dry_run:
        with open_balance(write=False) as bal:
            have = [p for p in cfg["preference"] if p in bal["providers"]]
        if have:
            costs = "  ".join(f"{p}={current_cost(p, cfg):.1f}" for p in have)
            print(f"⚖  quota consumata nella finestra (tutti i progetti): {costs}")
    print("✔ fine." + ("  (dry-run: unità marcate 'planned', nessuna 'done')" if dry_run else ""))


if __name__ == "__main__":
    main()
