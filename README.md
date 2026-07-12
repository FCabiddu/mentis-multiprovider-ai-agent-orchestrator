# mentis

**Orchestratore agentico agnostico rispetto al provider LLM — a subscription, non ad API.**
*A provider-agnostic multi-agent orchestrator that drives official subscription CLIs (Claude Code, OpenAI Codex) instead of paid APIs — for cross-model de-biasing, automatic fallback, and independent review.*

mentis prende uno stesso sistema di agenti (analista → architetto → planner → sviluppatore → QA → reviewer → docs) e lo esegue su **provider diversi** — oggi **Claude** (via Claude Code CLI) e **GPT-5.6** (via Codex CLI) — usando **gli abbonamenti che paghi**, non chiavi API a consumo. Serve per **sfidare** l'output di un modello con un altro, **switchare in automatico** quando un provider esaurisce i limiti, e ottenere una **review indipendente** su un modello diverso da quello che ha scritto il codice.

> Il regista sta **sopra** le CLI, non dentro: non ha tool propri né un cervello LLM. Sceglie *quale* CLI esegue *quale* passo, passa i compiti come prompt e le consegne come **file su disco**.

---

## Architettura

```mermaid
flowchart TD
    U["Tu — mentis build 'app TODO'"] --> ORC["orchestratore / regista<br/>orchestrator/mentis.py"]
    ORC -->|legge| AG["agents/*.md<br/>neutri: tier + reasoning"]
    ORC -->|legge| CFG["config/mentis.toml<br/>mappe modelli, pesi, routing"]

    ORC --> BAL["Balancer load-aware<br/>sceglie il provider meno carico"]
    BAL -->|implementa| P1["Claude Code CLI<br/>abbonamento Pro/Max"]
    BAL -->|valuta / fallback| P2["Codex CLI<br/>abbonamento ChatGPT"]

    P1 --> DISK[("Progetto su disco<br/>artefatti · .mentis/state.json · handoff · log")]
    P2 --> DISK
    DISK -->|resume / handoff| ORC

    ORC --> Q["Quality<br/>reflection = stesso provider<br/>evaluator = provider DIVERSO"]
    Q -->|approvato| DISK
    Q -->|NEEDS WORK x2| ESC["Escalation all'utente<br/>niente merge"]

    CB["Circuit breaker<br/>provider esaurito → escluso"] -.-> BAL
```

## La pipeline `build`

```mermaid
flowchart LR
    BA["business-analyst<br/>BAD"] --> TA["tech-architect<br/>TAD"] --> IP["implementation-planner<br/>IPD + DEPS.json"]
    IP --> DEV["developer<br/>una unità per issue"]
    DEV --> QA["qa-engineer"]
    QA --> REV["reviewer<br/>una unità per issue<br/>provider ≠ implementer"]
    REV --> DOC["documentation-agent"]
```

Ogni passo scrive il suo artefatto su disco; il passo dopo lo legge. È questo — e non uno stato in memoria — che rende la pipeline portabile tra provider: un file `.md` è neutro, non importa quale modello l'ha scritto.

---

## Cosa fa

| | Feature | In breve |
|---|---|---|
| 🔀 | **De-bias / challenge** | Stesso task su più provider, output affiancati per il confronto (`--compare`). |
| ↯ | **Fallback automatico** | Se un provider esaurisce i limiti, il task passa da solo al successivo. |
| 🛡️ | **Review anti-bias** | Il reviewer gira **sempre** su un provider diverso da chi ha implementato. |
| ⚖️ | **Balancer load-aware** | I ruoli ruotano così i due abbonamenti si consumano **insieme**, non uno solo. |
| 💾 | **Resumability** | Stato per-unità salvato dopo ogni unità: un run interrotto **riprende** da dove era. |
| ◇ | **Quality** | Reflection (auto-critica) + evaluator-optimizer cross-provider, loop cap 2 poi escalation. |
| 🧩 | **Fan-out per-issue** | `developer` e `reviewer` si espandono per issue, con ordinamento a dipendenze. |
| ⛓ | **Parallelismo** | Issue indipendenti su provider distinti in contemporanea (`--parallel`). |
| 🩺 | **doctor** | Aggiorna la mappa dei modelli quando esce una nuova famiglia — scoperta automatica, decisione tua. |
| 🧭 | **Osservabilità** | Ogni chiamata loggata; `mentis status` riassume carico, finestre, stato. |
| 📇 | **Contratto strutturato** | Ogni agente chiude con `[[MENTIS-RESULT]]{status,...}`: l'orchestratore consuma un'interfaccia, non prosa. |
| ⏸ | **HITL riprendibile** | `needs_input` mette in pausa e scrive le domande su file; rispondi in `.mentis/answers/` e rilancia per riprendere. |

Perché **a subscription e non ad API**: gli abbonamenti consumer (ChatGPT Plus, Claude Pro/Max) non danno accesso API — ma ogni vendor spedisce una **CLI agentica** che si autentica con l'abbonamento. mentis pilota quelle CLI in headless. I *tool* (leggere/scrivere file, eseguire comandi) li porta ogni CLI; l'orchestratore fa solo da regista.

---

## Uso

```bash
python3 orchestrator/mentis.py <comando> "<descrizione>" [flag]
```

| Comando | Cosa fa |
|---|---|
| `build "<descrizione>"` | Pipeline completa: BAD → TAD → IPD → developer → qa → reviewer → docs |
| `tad "<descrizione>"` | Solo l'architettura (tech-architect) |
| `bad "<descrizione>"` | Solo l'analisi (business-analyst) |
| `review "<PR/branch>"` | Solo review, su provider ≠ implementer |
| `mvp "<descrizione>"` | Sito statico (mvp-builder) |
| `doctor` | Manutenzione della mappa modelli |
| `status` | Carico per provider, finestre, stato unità, log |

**Flag principali:** `--project DIR` (default: cartella corrente) · `--compare` (challenge multi-provider) · `--quality off\|reflect\|evaluate\|full` · `--parallel` · `--fresh` (ignora lo stato) · `--dry-run`.

### Esempi

```bash
# pipeline completa su un progetto
python3 orchestrator/mentis.py build "app TODO con login" --project ~/dev/todo

# challenge: stesso TAD su Claude E GPT, output affiancati
python3 orchestrator/mentis.py tad "gateway pagamenti" --compare

# massimo controllo qualità + issue indipendenti in parallelo
python3 orchestrator/mentis.py build "app TODO" --quality full --parallel

# interrotto a metà? rilancia lo stesso comando: riprende dalle unità non fatte
python3 orchestrator/mentis.py build "app TODO" --project ~/dev/todo

# stato del progetto
python3 orchestrator/mentis.py status --project ~/dev/todo
```

---

## Stato attuale: DRY-RUN

> ⚠️ **Stato onesto.** La logica di mentis è coperta da una **suite di test** (`tests/`, stdlib `unittest` — esegui `python3 -m unittest discover -s tests`) e validata in dry-run. Ma mentis **non ha mai eseguito una chiamata reale** (nessun abbonamento attivo: entrambi i provider sono `enabled = false`): in dry-run stampa **il piano** senza eseguire nulla. Un review tecnico esterno ha fatto emergere bug di integrazione (crash sullo schema DEPS, timing del fan-out, verifica output, rate-limit): i **bloccanti sono corretti**, ed è stato aggiunto un **contratto di esito strutturato** (`done|needs_input|failed`) con **HITL riprendibile**. Il debito residuo — corpi degli agenti ancora Claude-oriented dietro un adapter di prosa — è tracciato in `DESIGN.md §13`.

### Attivare un provider

1. Installa e fai **login con l'abbonamento** alla CLI (non una API key): `claude` (Pro/Max) e/o `codex` (ChatGPT Plus/Pro).
2. In `config/mentis.toml` metti `enabled = true` sul provider.
3. **Verifica i flag** nella riga `cmd` con `claude --help` / `codex --help` (cambiano spesso) — è l'unica cosa da ritoccare.
4. Rilancia senza `--dry-run`.

---

## Come mappa gli agenti su modelli diversi

Gli agenti non nominano modelli: dichiarano un **tier** (quanto forte) e un **reasoning** (quanto ragiona). L'orchestratore li traduce per il provider corrente — tabelle in `config/mentis.toml`:

| L'agente dice | Claude | GPT-5.6 |
|---|---|---|
| `tier: frontier` | opus-4-8 | gpt-5.6-sol |
| `tier: balanced` | sonnet | gpt-5.6-terra |
| `tier: fast` | haiku-4-5 | gpt-5.6-luna |
| `reasoning: high` | thinking ~10k token | reasoning effort `high` |

Quando esce una nuova famiglia (es. `gpt-5.7-*`) cambi solo 3 righe — o lo fa `doctor`:

```bash
python3 orchestrator/mentis.py doctor --provider codex \
    --models "gpt-5.7-sol,gpt-5.7-terra,gpt-5.7-luna" --apply
```

---

## Struttura

```
mentis/
├── agents/            # i 9 agenti, frontmatter NEUTRO (tier + reasoning)
├── config/
│   └── mentis.toml    # unico punto provider-specifico: mappe, pesi, routing, template CLI
├── orchestrator/
│   └── mentis.py      # il regista — stdlib pura, zero dipendenze (Python ≥ 3.7)
├── README.md          # questo file
└── DESIGN.md          # architettura, decisioni e razionale in dettaglio
```

Zero dipendenze: `mentis.py` usa solo la standard library (incluso un mini-parser TOML). Nessun `pip install`.

Per il **perché** di ogni scelta — abbonamento vs API, i due tipi di contesto (durevole vs effimero), il reset a finestra del balancer, l'evaluator-optimizer — vedi **[DESIGN.md](DESIGN.md)**.

---

*mentis nasce come evoluzione provider-agnostica di un sistema di agenti costruito per Claude Code. Gli agenti restano neutri; l'orchestrazione, il de-bias e il bilanciamento vivono nel regista.*
