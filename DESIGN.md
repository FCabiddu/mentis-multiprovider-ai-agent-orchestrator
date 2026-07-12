# mentis — design

Documento di architettura e decisioni. Compagno operativo: `README.md`.

---

## 1. Perché a subscription e non ad API

Vincolo di partenza dell'utente: **usare gli abbonamenti pagati, mai API a
consumo**. Questo è possibile perché ogni grande provider oggi spedisce una
**CLI agentica** che si autentica con l'abbonamento consumer, non con una API key:

| CLI | Login con | Invocazione headless |
|---|---|---|
| Claude Code | Claude Pro/Max | `claude -p "…"` |
| Codex CLI | account ChatGPT Plus/Pro | `codex exec "…"` |

L'API a consumo (OpenAI/Anthropic, LiteLLM, OpenRouter) è quindi **fuori scope**
per scelta. L'orchestratore non parla mai con un'API: **pilota le CLI**.

Conseguenza: automatizzare la web-app di ChatGPT via browser sarebbe contro i
termini e fragile — **non** lo facciamo. Solo CLI ufficiali.

## 2. L'orchestratore è un regista, non un agente

`mentis.py` non ha cervello LLM né tool propri. Sa solo:

1. invocare una CLI headless con un prompt e una cartella di lavoro;
2. leggere il risultato (output + file scritti su disco);
3. decidere il prossimo passo (fallback / confronto / review incrociata).

I **tool** (leggere/scrivere file, eseguire comandi) li porta **ogni CLII**: non
li reimplementa l'orchestratore. Il **passaggio di consegne tra agenti** avviene
via **file su disco** nel progetto (il BAD in `business-analysis/`, il TAD in
`tech-analysis/`, …): un artefatto su disco è neutro, non importa quale modello
l'ha scritto. Questo — ereditato dal design di `pocket-it` — è ciò che rende la
pipeline portabile tra provider senza stato condiviso in memoria.

Lo **spawn di sotto-agenti**, che in Claude Code avveniva dentro l'agente,
qui **sale nell'orchestratore**: è il ciclo `for` sugli step della pipeline. È
lì che si sceglie il provider di ogni passo.

## 3. Agenti neutri: due assi ortogonali

Il frontmatter Claude-specifico (`model:`, `model_settings.thinking`, `tools:`)
è stato sostituito da due campi neutri:

```yaml
tier: frontier        # frontier | balanced | fast     → QUALE modello
reasoning: high       # none | low | medium | high | max → QUANTO ragiona
```

Sono **ortogonali**: un modello `balanced` può ragionare `high` (task medio ma
insidioso, senza pagare il flagship), un `frontier` può girare a `low` per
andare veloce. Tenerli separati preserva combinazioni che un unico campo
"modello" perderebbe.

`tools:` è stato **eliminato**: ogni CLI porta i suoi. Il corpo dell'agente
(istruzioni in inglese) è **riusato tale e quale**.

### Traduzione dai file originali di pocket-it

| Agente | model originale | → tier | thinking budget → reasoning |
|---|---|---|---|
| business-analyst | fable | frontier | 5000 → medium |
| tech-architect | fable | frontier | 8000 → high |
| mvp-builder | sonnet | balanced | 10000 → high |
| developer | sonnet | balanced | 5000 → medium |
| devops-engineer | sonnet | balanced | 3000 → medium |
| documentation-agent | sonnet | balanced | 3000 → medium |
| implementation-planner | sonnet | balanced | 5000 → medium |
| qa-engineer | sonnet | balanced | 5000 → medium |
| reviewer | sonnet | balanced | 3000 → medium |

Regola usata: `fable → frontier`, `sonnet → balanced`; budget thinking in bande
(≤2500→low, ≤7000→medium, >7000→high). Tutto **tunabile** cambiando il
frontmatter dell'agente — nient'altro dipende da questi valori.

## 4. Le mappe di traduzione (in `config/mentis.toml`)

**Asse 1 — tier → modello:**

```
tier        claude       codex (gpt-5.6)
frontier →  opus-4-8   | gpt-5.6-sol
balanced →  sonnet     | gpt-5.6-terra
fast     →  haiku-4-5  | gpt-5.6-luna
```

**Asse 2 — reasoning → meccanismo del provider:**

```
reasoning   claude (thinking budget)   codex (reasoning effort)
none     →  0                        | minimal
low      →  2000                     | low
medium   →  5000                     | medium
high     →  10000                    | high
max      →  24000                    | ultra
```

### Clamping

Se un provider non offre un gradino di reasoning, l'orchestratore scende al più
vicino disponibile (funzione `clamp_reasoning`, ladder
`none<low<medium<high<max`). Oggi entrambi i provider hanno tutti e 5 i gradini,
quindi non scatta — ma è pronto per un provider più povero domani. Stesso
principio vale concettualmente per il tier.

Aggiungere un provider = **una sezione `[providers.X]` + una colonna** alle due
mappe. Gli agenti non si toccano mai.

## 5. Fallback

`mode = "fallback"`: si prova il provider in cima a `preference`; se la CLI
ritorna un errore che matcha i pattern di rate-limit
(`rate limit`, `quota`, `429`, `overloaded`, …) si passa al successivo con lo
**stesso identico prompt**. È lo switch "quando Claude si esaurisce" richiesto.

## 6. Confronto / challenge

`--compare` (o `mode = "compare"`): lo stesso agente gira su **tutti** i
provider attivi, ognuno in una cartella separata
(`.mentis/compare/<agente>/<provider>/`), così gli output si possono diffare.
È lo strumento di de-bias: far criticare a GPT ciò che ha prodotto Claude e
viceversa.

## 7. Review incrociata anti-bias (la regola chiave)

**Il reviewer non gira mai sullo stesso provider che ha scritto il codice.** Un
modello che rivede sé stesso condivide i propri punti ciechi; l'intero senso
della review è un secondo parere **indipendente**.

Meccanismo:

- Ogni step che produce codice (`developer`, `devops-engineer`, `qa-engineer`,
  `mvp-builder`) viene registrato in `.mentis/ledger.json` nel progetto con il
  provider che l'ha eseguito.
- Quando arriva il reviewer, l'orchestratore legge l'ultimo implementer e sceglie
  per il reviewer un provider **diverso** (`pick_provider`). Con 2 provider è
  deterministico: implementa Claude → rivede Codex, e viceversa.
- Il reviewer riceve negli argomenti `Implemented-by:` e `Reviewer-provider:` e,
  come doppia sicurezza, il suo prompt (`agents/reviewer.md`, Step 0) **rifiuta**
  la review se i due coincidono.
- Se esiste un solo provider attivo, `[review].on_no_alternative` decide:
  `warn` (procede segnalando che l'anti-bias non è garantito) o `block` (rifiuta).

Verificato in dry-run: nella pipeline `build`, con implementer su `claude`, il
reviewer viene instradato su `codex/gpt-5.6-terra` automaticamente.

## 8. Manutenzione dei modelli — il comando `doctor`

Il costo di manutenzione del sistema è volutamente ridotto a **una tabella**:
`[model_map]` in `mentis.toml`. Né l'orchestratore né gli agenti contengono nomi
di modelli. Quando esce una nuova famiglia, cambiano solo 3 righe per provider.

`doctor` automatizza la *scoperta* senza togliere la *decisione*:

- ottiene la lista modelli — da `--models "a,b,c"` (a mano) oppure eseguendo
  `list_models_cmd` della CLI;
- per ogni tier usa `[tier_keywords]` (frontier→`sol`/`opus`, …) per trovare il
  modello giusto con la **versione più alta** (`version_of`);
- segnala i modelli **scomparsi** (da sostituire) e quelli **aggiornabili**
  (ne esiste uno più recente);
- senza `--apply` stampa solo le proposte; con `--apply` riscrive i valori in
  `[model_map.<provider>]` **preservando i commenti**.

Scelta di design: **non** si auto-adotta il modello più recente in silenzio. Un
nuovo flagship può cambiare prezzo, reasoning o comportamento sui tool; il
passaggio resta un atto deliberato. `doctor` ti avvisa, tu decidi.

## 9. Controllo qualità: Reflection + Evaluator (IMPLEMENTATO)

mentis adotta pattern agentici standard del 2026
(Reflection, Evaluator-Optimizer, Human-in-the-Loop, Durable Execution,
Circuit Breaker), composti su una spina dorsale Sequential Pipeline + un
Orchestrator-Worker per il fan-out per-issue.

### Due controlli complementari
- **Reflection** — auto-critica *dello stesso provider* prima della consegna.
  Economica. Filtro per errori oggettivi/banali. **Vale davvero solo con un
  segnale esterno** (test, build): forte su developer/qa, debole su prosa.
- **Evaluator (cross-provider)** — critica di un modello *diverso*
  dall'implementer (è il nostro reviewer anti-bias, generalizzato). Unico a
  vedere i punti ciechi. Serve una **rubrica per tipo di agente**: gate binario
  per il codice, rubrica qualitativa (completezza/ambiguità/coerenza) per BAD/TAD.

Ordine: **reflection prima** (build + esecuzione), poi evaluator.

### Configurabilità — due livelli
- **Per-agente (profilo):** in `config/mentis.toml` sotto `[quality.profile]`
  (non nel frontmatter dell'agente): livello di default per agente.
- **Di sessione (override):** manopola `--quality`:
  `off` | `reflect` | `evaluate` | `full` (reflection→evaluator), che sovrascrive
  globalmente il profilo.

### Default quando l'utente non specifica — PESATO SUL RISCHIO (non `full` piatto)
Motivo: reflection su prosa rende poco, e su abbonamento flat ogni chiamata
extra brucia budget di rate-limit spingendo prima nel fallback.

| Agente | Default | Perché |
|---|---|---|
| business-analyst | `evaluate` | errore avvelena tutto a valle, niente CI a valle |
| tech-architect | `evaluate` (o `full`) | massimo effetto-leva |
| implementation-planner | `evaluate` | forma il lavoro di tutti |
| developer | `reflect` | reflection forte (test); il check cross-provider del codice è già lo step `reviewer` |
| qa-engineer | `reflect` | ha già i test come segnale |
| devops-engineer | `reflect` | verificabile dalla CI |
| documentation-agent | `reflect` | ultimo step, non propaga |
| reviewer | `off` | è LUI l'evaluator del codice — non si valuta l'evaluator |

`--quality full` forza tutto in alto (run paranoico); `off` disattiva (bozza).

### Come è implementato (note sulla realizzazione)
- Il profilo e le rubriche vivono in `config/mentis.toml` (`[quality.profile]`,
  `[quality.rubric]`), non nel frontmatter: tunabili in un punto solo.
- `--quality` è un **override globale** (applica lo stesso livello a tutti gli
  agenti). Senza flag → profilo per-agente sopra.
- `developer` è `reflect` (non `full`) per **non duplicare** la review: il check
  cross-provider del codice è lo step `reviewer`. `reviewer` è `off`.
- **Audit dei corpi Claude-only** risolto con un **contratto di neutralità**
  (`NEUTRALITY_PREAMBLE`) iniettato in OGNI chiamata: vieta tool `Agent`/MCP
  Linear, impone file locali (`tasks/`, `*_DEPS.json`), firma commit come
  `mentis`. Così i corpi restano vicini a pocket-it senza fork.
- **Reliability (§3) implementata:** circuit breaker (un provider rate-limited
  resta escluso per il resto del run), budget di retry globale, backoff+1 retry
  sugli errori transitori. Config in `[reliability]`.

### Regole del loop e dei fallimenti (DECISE)
- **Cap iterazioni evaluator-optimizer = 2.** genera→valuta→revisiona, max 2
  revisioni. Se dopo il 2° giro l'evaluator boccia ancora → **STOP: non si
  procede né si merga, si scala all'utente** con le obiezioni residue.
- **Evaluator-provider non disponibile** (circuit breaker aperto / rate-limit,
  e resta solo il provider dell'implementer): → **degrado a reflection-only,
  marcata "review NON indipendente — anti-bias non garantito", E si scala
  all'utente.** Non auto-approva mai in silenzio.

## 10. Durable execution / resumability (IMPLEMENTATO)

Colma il gap #7 (no resumability) e abilita il **provider-switch a metà lavoro
senza perdere il contesto durevole**. Sostituisce il vecchio `ledger.json`
(salvato solo a fine run) con `.mentis/state.json`, unico store salvato **dopo
ogni unità**. Implementa: state file + resume, nota di handoff, fan-out
per-issue e verifica dell'output (Tier 1 #2). Funziona in dry-run e reattivo.

**Perché il rate-limit NON dà un "ultimo respiro":** l'esaurimento quota è una
**porta binaria** (429), non un segnale che scema — l'agente viene rifiutato in
blocco e non può scrivere nulla in extremis. Per questo l'handoff NON è un evento
finale ma un **processo continuo**: la nota di progresso si scrive dopo ogni
unità, così è già su disco *prima* che la porta sbatta. Lo switch proattivo (al
~90% di quota) è possibile solo se la CLI espone la quota residua — bonus, non
base su cui contare.

### Due tipi di contesto — solo uno è trasferibile tra provider
- **Durevole = ciò che è su disco** (artefatti, file di codice parziali, stato
  git). Sopravvive e il provider subentrante lo legge, perché tutte le CLI
  condividono la stessa working dir. **Questo si preserva.**
- **Effimero = il ragionamento vivo dell'agente** (conversazione/piano interno).
  Vive nella sessione della CLI e **NON è portabile** tra Claude e Codex
  (sistemi/tokenizer/formati diversi). Uno switch a metà step lo perde. Nessun
  handoff "in memoria" tra provider diversi è possibile — non prometterlo.

### Come rendere la perdita dell'effimero quasi irrilevante
1. **Working dir condivisa** (già così): i file parziali persistono.
2. **Externalize su disco:** ogni agente scrive, oltre agli artefatti, una breve
   **nota di handoff** (`fatto: … / da fare: … / decisioni: …`). Chi subentra la
   legge e si riorienta invece di ricostruire a naso. (Pattern
   "externalized state / rehydration".)
3. **Checkpoint a grana fine:** spezzare gli step grossi (developer su N issue)
   in unità **per-issue** (l'Orchestrator-Worker, gap #6). Un crash/switch perde
   al più il ragionamento di *una* unità, non dell'intero step. → #6, #7 e la
   nota di handoff sono lo **stesso** problema: minimizzare l'effimero perso.

### Meccanismo concreto
`.mentis/state.json`: per ogni **unità** (step, o issue dentro uno step)
`pending | running | done` + path artefatto + **hash dell'input**. All'avvio:
- salta le unità `done` il cui artefatto esiste **e** il cui input non è cambiato
  (hash) — altrimenti sono stale e vanno rifatte;
- riprende dalla prima non-`done`;
- se un'unità era `running` (crash a metà), legge la nota di handoff e la riassegna
  al provider disponibile.

**Regola critica:** lo stato si scrive **dopo ogni unità**, non a fine run (è
l'errore attuale del ledger). Il ledger anti-bias andrà allineato allo stesso
salvataggio incrementale.

## 11. Balancer load-aware — rotazione dei ruoli (IMPLEMENTATO)

**Problema.** Con ruoli fissi (Claude implementa sempre, Codex valuta sempre) e
implementer ≈ 1× vs evaluator ≈ 0,3×, Claude si svuota ~3× più in fretta: arrivi
al suo muro con Codex ancora quasi libero. Throughput totale sprecato.

**Soluzione.** `routing = "balanced"`: un contatore di lavoro pesato per provider
(persistito in `.mentis/state.json`). Chi **implementa** = provider col costo più
basso; l'**evaluator** = il più scarico tra gli ALTRI (anti-bias sempre rispettato).
I ruoli ruotano per leapfrog e i due budget si esauriscono **insieme** →
massimo numero di artefatti prima di uno stop.

**Pesi** (`[balance.weights]`, si auto-correggono per leapfrog, i valori esatti
contano poco): implement 1.0, optimize 1.0, reflect 0.5, evaluate 0.3.

**Reset a finestra, non decadimento.** Le quote a subscription non calano piano:
si **sbloccano a scatti ogni N ore**. Quindi il costo è cumulativo DENTRO la
finestra e si **azzera** quando la finestra scade (`[balance.window]`, ore per
provider — STIMA da calibrare col primo run reale). Non serve decadimento
esponenziale.

**Conseguenza strutturale — reviewer per-issue.** Bilanciare il fan-out developer
sparge le issue tra i due provider; quindi il `reviewer` si espande anch'esso
**per-issue** (`FANOUT_STEPS`), e ogni `reviewer::FC-x` gira sul provider ≠
implementer *di quella specifica issue* (letto da `state.json`). Verificato: FC-1
fatto da codex → review su claude; FC-2 da claude → review su codex; ecc. Sblocca
anche il parallelismo (issue indipendenti su provider diversi).

**Interazioni.** Circuit breaker: un provider esaurito è escluso da entrambi i
ruoli → resta solo l'altro → scatta il degrado+escalation dell'evaluator (già
gestito). Anti-bias: intatto. Pin per-agente: non necessari (l'utente non ha
preferenze su chi guida).

**v2 (non fatto):** pesare anche per tier (frontier costa più di balanced);
auto-calibrare i pesi dalle dimensioni reali osservate (serve il contatore di
chiamate/log); rilevare il reset di finestra empiricamente (429→successo).

## 12. Osservabilità, robustezza, parallelismo (IMPLEMENTATO)

Pacchetto di rifiniture, tutte testate in dry-run + unit test:

- **Log/transcript** — ogni chiamata a un provider è registrata in
  `.mentis/logs/calls.jsonl` (metadati: provider, modello, reasoning, agente,
  chars in/out, exit code, rate_limited) + transcript completo prompt+output in
  `.mentis/logs/transcripts/`. Base per debug reale e per l'auto-calibrazione
  futura dei pesi del balancer.
- **`mentis status --project DIR`** — mostra carico per provider, ore residue
  alla ricarica della finestra, conteggio unità per stato, escalation/fallite in
  attesa, numero di chiamate registrate.
- **Parsing robusto del verdetto** — l'evaluator/reviewer conclude con
  `VERDICT: APPROVED|NEEDS WORK`; `parse_verdict` cerca il tag, poi i token
  ovunque, e in caso ambiguo è **conservativo** (NEEDS WORK: non approva al buio).
- **Verifica output stretta** (`produced_output`) — per gli step implementanti
  senza artefatto-file, verifica via git (HEAD avanzato o tree sporco) o, in
  assenza di git, file modificati dopo l'avvio. Niente più "done" silenzioso su
  un'unità che non ha prodotto nulla.
- **Reviewer come evaluator-optimizer** (`reviewer_loop`) — lo step reviewer non
  è più un passaggio unico: review → se NEEDS WORK ri-dispaccia il **developer**
  sull'implementer → re-review, fino a `loop_cap`, poi escalation. Stesso
  trattamento dei documenti (§9), applicato al codice.
- **Parallelismo** (`--parallel`) — le issue developer indipendenti (deps non
  nella stessa wave) girano in wave concorrenti su **provider distinti**
  (`build_waves` + `ThreadPoolExecutor`, `_STATE_LOCK` protegge lo stato). Mai due
  task sullo stesso provider insieme (eviterebbe di accelerare il rate-limit). In
  dry-run le wave sono mostrate ma eseguite in sequenza.

## 13. Review esterno e hardening (Tier A applicato)

Un review tecnico avversariale esterno ha esaminato mentis in dry-run e ha
trovato bug di **integrazione non ancora esercitata** (coerenti con lo stato
"mai eseguito dal vivo"). Diagnosi-radice condivisa: *il contratto tra
orchestratore e agenti era prosa, non un'interfaccia*. **Bloccanti corretti:**

- **Crash sul DEPS del planner** — il planner scriveva `issueMap`/`dependencies`
  (formato Linear), il parser voleva `issues[]` → `AttributeError`. Ora esiste
  **uno schema canonico unico** `{"issues":[{id,title,label,deps}]}`, il planner
  lo produce, e `load_issues` è tollerante (converte il legacy, valida, avvisa e
  ritorna `None` invece di crashare).
- **Fan-out prima del planner** — `expand_units` girava una volta a inizio run,
  prima che il planner scrivesse il DEPS. Ora l'espansione è **lazy per-step**
  (`expand_step`): developer/reviewer si espandono quando si arriva al loro turno,
  leggendo il DEPS appena prodotto.
- **`produced_output` sempre vero** — le scritture di mentis stesso (`.mentis/`)
  facevano passare unità vuote per `done`. Ora la verifica **esclude `.mentis/`**
  (git dirty filtrato, mtime che salta `.mentis/`), e l'orchestratore gitignora
  `.mentis/` nel progetto-target.
- **Falso positivo rate-limit** — il grep girava su stdout di merito (un TAD che
  *parla* di "429" triggerava un fallback spurio). Ora: `rate_limited` solo se
  **rc≠0 E** pattern su **stderr**.
- **Loop quality ignoravano gli errori** — l'output di una CLI fallita/limite
  finiva in `parse_verdict` come NEEDS WORK spurio. Ora evaluator/reviewer/loop
  **onorano `ok`/`rate_limited`** ed escalano (o aprono il breaker) invece di
  fabbricare un verdetto.
- **Label hardcoded, review-arg perso, reasoning no-op, mvp path assoluto** —
  label reale dal DEPS; `reviewer_loop` riceve l'argomento utente; hint di
  reasoning iniettato nel prompt per provider senza flag `{reasoning}` (Claude);
  mvp-builder scrive relativo al progetto.

**Tier B — parte applicata:**
- **Contratto di esito strutturato** (`CONTRACT_INSTRUCTION` + `parse_result`):
  ogni agente principale chiude con `[[MENTIS-RESULT]]{"status":"done|needs_input|
  failed", "artifacts", "questions", "note"}[[/MENTIS-RESULT]]`. L'orchestratore
  consuma un'**interfaccia**, non prosa. (Il reviewer resta su `VERDICT:`.)
- **HITL riprendibile** — `needs_input` → le domande vanno su
  `.mentis/questions/{unit}.md`, l'unità va in `awaiting_input`, la pipeline si
  ferma; l'utente risponde in `.mentis/answers/{unit}.md` e al rilancio l'unità
  riprende con le risposte iniettate. Sostituisce `AskUserQuestion` (incompatibile
  con headless).
- **Resume dependency-aware** — `input_hash` include `upstream_hash` (BAD/TAD/IPD):
  un artefatto a monte cambiato invalida i downstream `done`.
- **Suite di test** — `tests/test_mentis.py` (stdlib `unittest`, 20 test): parser
  TOML, `load_issues` (canonico/legacy/garbage/anti-crash), balancer, verdict,
  result, toposort, waves, no-shell-injection, flusso HITL.

**Tier B — neutralizzazione corpi agente (in corso).** I tre offender principali
sono stati riscritti direttamente nei corpi (non solo coperti dal preambolo):
- `reviewer.md` — rimosso lo **spawn** di sotto-agenti (`Agent`/`subagent_type`):
  ora riporta il fix come `NEEDS WORK` e il `reviewer_loop` ri-dispaccia.
- `implementation-planner.md` — Step 6 **Linear neutralizzato**: niente MCP, il
  deliverable è il `*_DEPS.json` canonico + l'IPD locale.
- `business-analyst.md` — **`AskUserQuestion` rimosso**: le domande bloccanti
  passano dal contratto `needs_input`, altrimenti default + assunzioni documentate.

**Tutti i 9 corpi ora neutralizzati** (parte 3): rimosse le chiamate reali
`AskUserQuestion` (→ contratto `needs_input` o default inferiti+documentati) e
`mcp__claude_ai_Linear__*` in developer/qa/devops/mvp/tech-architect/planner/docs.
Auto-merge: niente più domanda interattiva né file `/tmp` insicuro → default
Manual approval. Rimosso il blocco `curl` all'API Linear e il riferimento fantasma
alla CLAUDE.md. *Nota onesta:* i sotto-passi Linear `6a–6d` del planner restano
fisicamente nel file ma sono **morti** (saltati dallo Step 6 + vietati dal
preambolo); una rimozione fisica completa è un cleanup a costo/rischio non
giustificato ora.

**Fatto anche (senza abbonamenti, testati con git reale + dry-run):**
- **Routing per label** — lo step `developer` instrada ogni issue all'agente giusto
  (`Backend`/`Frontend` → developer, `DevOps` → devops-engineer via `agent_for_label`).
  Rende **`devops-engineer` raggiungibile** (prima non era in nessuna pipeline, #8).
- **Fix-CI dispatch** — il `reviewer_loop` ri-dispaccia il rework all'**agente che ha
  implementato** quella issue (letto da `implementer_unit_of_issue`), non sempre il
  developer.
- **Worktree isolation** — in `--parallel` ogni unità implementante gira in un git
  worktree isolato (`make_worktree`/`remove_worktree`, branch `mentis/{unit}`,
  serializzati da `_GIT_LOCK`); niente collisione su `.git/index.lock`. Il `cwd` è
  propagato in tutta la catena (run/quality/verifica git); il branch persiste dopo il
  cleanup del worktree.

**Ancora da fare:** rimozione fisica dei sotto-passi Linear morti del planner
(cosmetico). Poi i v2 (balancer avanzato, terzo provider, preflight auth) — da
tarare col primo run reale.

## 14. Cosa resta da fare quando attivi gli abbonamenti

1. Installare le CLI e fare login con gli **abbonamenti**.
2. `enabled = true` sui provider in `mentis.toml`.
3. **Confermare i flag reali** nelle righe `cmd` (`--help` di ciascuna CLI):
   è l'unico punto che dipende da dettagli che cambiano nel tempo. In
   particolare: come Claude Code accetta il livello di thinking, e la sintassi
   esatta di `codex exec`/`--reasoning`.
4. Provare prima in dry-run, poi un `tad --compare` come pilota, infine `build`.
