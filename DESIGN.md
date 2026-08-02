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

I **tool** (leggere/scrivere file, eseguire comandi) li porta **ogni CLI**: non
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
tier        claude     codex (gpt-5.6)
frontier →  opus     | gpt-5.6-sol
balanced →  sonnet   | gpt-5.6-terra
fast     →  haiku    | gpt-5.6-luna
```

Lato Claude si usano gli **alias** della CLI (`opus`/`sonnet`/`haiku`): sono i
valori che `--model` accetta sempre, stabili fra le release. Gli id completi
(`claude-opus-5`, `claude-opus-4-8`, …) vanno bene se si vuole pinnare una
versione precisa — ed è la forma su cui `doctor` sa ragionare, perché confronta
i numeri di versione. I valori precedenti (`opus-4-8`, `haiku-4-5`) non erano
né alias né id validi: l'audit di agosto li ha corretti.

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
**stesso prompt**, arricchito dalla nota di handoff su disco (chi subentra riparte da
lì: il contesto durevole si preserva, l'effimero no — §10). È lo switch "quando
Claude si esaurisce" richiesto.

Il **riconoscimento** del limite è deliberatamente conservativo: si considera
rate-limit solo una CLI che è **fallita** (rc≠0) e il cui output contiene una delle
formule note (`rate limit`, `quota`, `usage limit`, `spend limit`, `429`, …). Il
vincolo su rc≠0 basta a escludere il falso positivo classico — un TAD che *parla* di
"429" — senza doversi limitare a stderr: le CLI a subscription scrivono il messaggio
di limite su entrambi i canali.

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
  `mvp-builder`) viene registrato in `.mentis/state.json` nel progetto con il
  provider che l'ha eseguito (il vecchio `ledger.json` non esiste più: §10).
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
| mvp-builder | `reflect` | output verificabile guardandolo; nessuno step a valle |

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

**Pesi** (`[balance.weights]`): implement 1.0, optimize 1.0, **review 1.0**,
reflect 0.5, evaluate 0.3 — moltiplicati per il **tier** del modello
(`[balance.tier]`: frontier 3.0, balanced 1.0, fast 0.4).

I due fattori sono stati corretti misurando un run completo (§14):

- **Il reviewer valeva 0.3 e non doveva.** Il suo prompt è il più grosso della
  pipeline (~18k caratteri: corpo agente intero + diff, 2,6× un developer), ma
  cadeva sotto `evaluate`, la voce pensata per la critica breve del quality gate
  (~1k caratteri). Ora ha una voce sua, `review`.
- **Il tier non contava niente.** `business-analyst` su frontier e `developer` su
  balanced pesavano uguale, mentre su abbonamento una chiamata frontier consuma
  molto di più: il conteggio sbagliava proprio dove il costo è alto.

Effetto misurato sullo stesso run: lo scarto fra la quota di lavoro *stimata* dal
balancer e quella *osservata* nei log scende da 3,9 a 2,8 punti percentuali.

**I valori restano stime, ed è dichiarato.** `mentis status` mostra affiancate la
colonna `[STIMA]` (il costo pesato) e `[OSSERVATO]` (chiamate e caratteri reali
da `calls.jsonl`): se divergono, si ricalibrano i pesi sui dati veri invece di
indovinare.

**Cosa viene addebitato.** Non solo le chiamate riuscite: un **timeout** ha
consumato quanto una riuscita (spesso di più) e un errore transitorio ha comunque
fatto partire la richiesta — se non li si addebita, il provider che ha appena
bruciato 90 minuti risulta il più scarico e il balancer gli manda altro lavoro.
Un **rate-limit** invece è respinto alla porta e non si paga.

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

### Il conto è dell'account, non del progetto

Il contatore stava in `{progetto}/.mentis/state.json`: ogni nuovo progetto
ripartiva da zero mentre la quota vera era già stata spesa altrove. Ora il
consumo si accumula in **`~/.mentis/balance.json`** (override con `MENTIS_HOME`),
condiviso da tutti i progetti, con `flock` perché due run possono girare insieme.
Nel `state.json` resta un totale **per progetto**, ma solo informativo: "quanto è
costato questo progetto". I progetti che avevano già un contatore vengono
travasati una volta sola (`migrate_project_budget`).

Verificato: due `bad` di fila su progetti diversi → il primo consuma claude, il
secondo **vede claude già carico e instrada su codex**. Prima ripartiva da zero e
ricaricava lo stesso provider.

### Il rate-limit è l'unico dato certo — e va ricordato

Resta il consumo che avviene **fuori** da mentis: una sessione interattiva di
Claude Code, claude.ai. Nessuna CLI a subscription espone la quota residua (è il
motivo stesso per cui il progetto usa le CLI e non le API), quindi non c'è modo
di leggerla.

Ma un rate-limit vero *è* la misura: in quel momento sai per certo che la quota è
finita, qualunque cosa dicesse la stima. Prima quell'informazione veniva usata
solo per il run corrente (circuit breaker) e poi buttata. Ora `mark_exhausted` la
scrive nel bilancio condiviso, e ogni run successivo — anche in un altro progetto
— **parte già escludendo quel provider** fino alla ricarica stimata. Se risultano
esauriti tutti, mentis non parte affatto invece di sprecare tentativi.

Il comando `mentis balance` mostra il conto, lo azzera (`--reset`, quando sai che
la quota è tornata) e permette di registrare a mano un consumo esterno
(`--add claude=5`). Quest'ultimo è una scorciatoia, non il meccanismo: la
contabilità a mano non la tiene aggiornata nessuno, mentre il rate-limit si
registra da solo. `--reset` azzera il **consumo** ma conserva la **calibrazione**:
il tetto è conoscenza acquisita, non stato del run.

### "Quanti token mi restano?" — la risposta onesta

Nessuna delle due CLI espone la quota residua in modo programmatico: `/usage` in
Claude Code e `/status` in Codex esistono **solo in interattivo** (ci sono issue
aperte per una versione headless). Quindi la domanda "che percentuale mi resta"
non ha una risposta diretta, e mentis non deve fingere di averla.

Quello che le CLI danno è il **consumo effettivo per chiamata**:

| CLI | Flag | Cosa restituisce |
|---|---|---|
| Claude Code | `--output-format json` | `usage.input_tokens`, `usage.output_tokens`, `total_cost_usd` |
| Codex | `--json` | JSONL di eventi; `token_count` porta i totali cumulativi |

Con `usage_json = true` sul provider, mentis legge quei numeri
(`parse_usage_envelope`) e conta i **token veri** invece di stimarli dai pesi.
Verificato in simulazione contro una quota nota: il conteggio coincide
esattamente con la verità (13.587 e 15.154 token, zero scarto).

Il **denominatore** invece si può solo imparare: quando arriva un rate-limit
vero, i token consumati fino a quel momento *sono* il tetto. Da lì in poi
`mentis balance` mostra una percentuale reale. Nella simulazione lo scarto
rispetto alla verità è di 5–9 punti, sempre **per eccesso** — mentis crede di
aver consumato più di quanto abbia davvero, perché il tetto osservato esclude la
chiamata rifiutata. Per un meccanismo di sicurezza è la direzione giusta: prudente,
non temerario. Finché il muro non è mai stato toccato, `balance` dice
**«quota usata: IGNOTA»** invece di inventare un numero.

Restano fuori dal conto le sessioni interattive e claude.ai: non si vedono in
anticipo, ma emergono come un rate-limit che arriva prima del previsto — e quello
viene registrato.

**v2 (non fatto):** auto-calibrare i pesi dalle dimensioni reali osservate (ora
il dato c'è: `chars_in`/`chars_out` in `calls.jsonl`, e `mentis status` lo mostra
accanto alla stima); stimare la ricarica dall'osservazione (429→successo) invece
che dalle ore di `[balance.window]`.

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
- **Suite di test** — `tests/test_mentis.py` (stdlib `unittest`): parser
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

## 14. Audit pre-primo-run (2026-08-01) — cosa è cambiato

Secondo giro avversariale, questa volta mirato all'imminenza del primo run reale:
sei lenti indipendenti sul codice attuale più un regression-check voce-per-voce
della review esterna di luglio. Il nucleo di design ha retto; a rompersi erano
il **ciclo di vita di un run vero** e il **contatto con le CLI** — cioè
esattamente ciò che il dry-run non poteva esercitare.

### I difetti che avrebbero rotto il primo run (corretti)

- **Il resume si auto-invalidava.** `upstream_hash` includeva *tutti* gli
  artefatti, quindi anche l'output dello step stesso: al rilancio ogni unità
  `done` risultava "cambiata da sé stessa" e l'intera pipeline si rifaceva da
  capo, ribruciando la quota. Il fix (`UPSTREAM_DIRS`) hasha solo ciò che sta
  **davvero a monte** — l'invariante è che uno step non può dipendere dal
  proprio output né dai suoi downstream. È il fix più importante del giro: il
  precedente hardening aveva risolto il problema opposto (non invalidare mai)
  trasformandolo nel suo eccesso.
- **Il rate-limit reale non veniva riconosciuto.** I pattern coprivano il
  lessico API (`429`, `quota`) ma non le formule delle CLI a subscription
  ("You've hit your monthly spend limit", "usage limit reached"), e il match
  girava solo su stderr. Conseguenza: limite letto come errore transitorio →
  retry sullo stesso provider esaurito → `retry_budget` finito → STOP, invece
  del fallback all'altro provider. Ora: rc≠0 **e** pattern su stdout+stderr.
- **Con un solo provider la pipeline si fermava alla prima unità.** La policy
  `[review].on_no_alternative = "warn"` era onorata solo da `pick_provider`,
  morto per questi percorsi: `evaluator_loop` e `reviewer_loop` escalavano
  sempre. Con `business-analyst = evaluate` nel profilo di default, un solo
  abbonamento attivo significava zero pipeline. Ora `warn` degrada e prosegue
  marcando l'esito `approved-not-independent` (mai un'approvazione silenziosa).
- **Il codice implementato spariva per gli step a valle.** In `--parallel` i
  branch `mentis/*` non venivano mai integrati: qa, reviewer e docs giravano su
  un albero che non conteneva il lavoro. Ora, a unità riuscita, l'orchestratore
  li integra (`merge_unit_branch`, con abort+avviso sul conflitto).
- **`remove_worktree --force` distruggeva il lavoro non committato** mentre
  l'unità veniva marcata `done`. Ora quel lavoro viene committato sul branch
  prima del cleanup.
- **`produced_output` era di nuovo sempre vero**: `ensure_gitignore` sporca il
  `.gitignore` a inizio run, quindi il tree risultava "modificato" per tutto il
  run. Ora c'è una baseline pre-unità e `.gitignore` è escluso; l'artefatto
  atteso vale solo se **scritto da questa unità** (non se residuo di un run
  precedente).
- **CLI assente = crash secco** (`FileNotFoundError` non gestita). Ora è un
  errore di configurazione: provider escluso, nessun budget bruciato — e un
  **preflight** lo intercetta prima ancora di partire.
- **Il retry riuscito perdeva l'output**, e con esso il contratto
  `[[MENTIS-RESULT]]`: un `needs_input` diventava un `done` silenzioso.
- **`mentis review` a sé stante**: crashava (`KeyError` su implementer ignoto) e
  faceva fan-out su tutte le issue del DEPS — N review per una sola PR.
- **Timeout**: unico e non documentato, e su timeout si ritentava da capo su un
  progetto già mezzo modificato. Ora è configurabile, più alto per gli step
  implementanti, e un timeout **scala all'utente** invece di ritentare.

### Il debito di contratto verso gli agenti (chiuso)

- **`Branch:` non veniva mai passato** benché developer/devops lo pretendessero,
  e i loro corpi ordinavano `git checkout main && git pull origin main` — che in
  un worktree fallisce e in sequenziale nasconde il lavoro della issue
  precedente. Ora l'orchestratore passa `Branch:` e un blocco
  `[mentis — contesto reale del repo]` che dichiara cosa il repo supporta
  davvero; i corpi sono stati riscritti di conseguenza.
- **Nessun percorso degradato senza remote/gh/CI.** Un progetto locale — il caso
  normale di un primo run — faceva fallire push e `gh pr create`, e il reviewer,
  che tratta la CI assente come rossa, bocciava ogni issue: loop di rework fino
  al cap, quota bruciata, escalation ovunque. Ora developer, devops, qa e
  reviewer hanno un ramo esplicito "niente remote → consegna su branch locale" e
  il reviewer ha un **LOCAL MODE** che valuta il diff. Regola nuova e netta:
  *l'infrastruttura mancante non è mai motivo di NEEDS WORK*.
- **Il reviewer non chiudeva con la riga `VERDICT`** che l'orchestratore legge, e
  il suo template conteneva entrambi i token — quindi il parser cadeva
  nell'ambiguo (= NEEDS WORK conservativo) e innescava rework inutili. Ora lo
  Step 6 impone la riga finale, unica e ultima. In più `parse_verdict` prende
  l'**ultimo** match, non il primo: le nostre stesse istruzioni contengono
  `VERDICT: APPROVED` come esempio.
- **La board `tasks/` era consumata da tre agenti e prodotta da nessuno.** Ora la
  scrive il planner (Step 6a/6b), con il formato che developer, qa e reviewer
  parsano davvero. Nello stesso passaggio sono spariti fisicamente i sotto-passi
  Linear `6a–6d` (145 righe di codice morto che ordinavano MCP e
  `AskUserQuestion`: l'ultima prosa capace di far deragliare un agente).
- **Auto-merge letto da `/tmp/{repo}-automerge`**, path prevedibile e scrivibile
  da qualsiasi processo locale → spostato in `.mentis/automerge`, dentro il
  progetto.

### Controllo di spesa

Il tetto vero non è per-provider ma **per run**: `[reliability].max_calls_per_run`
(default 60) conta le sessioni CLI effettivamente eseguite e ferma il run quando
lo supera — lo stato è salvato, quindi si riprende con un rilancio. È la rete di
sicurezza contro il caso in cui fan-out × quality × retry × loop si moltiplicano
su un abbonamento: prima di questo, l'unico limite era il `retry_budget`, che
conta solo i *fallimenti*.

### Altro

Ambiente dei sottoprocessi ripulito dalle variabili `*_API_KEY` (il vincolo è
*solo* abbonamento: una chiave nel profilo farebbe fatturare a consumo);
`charge()` non addebita più in dry-run (il primo run reale partiva con carichi
fantasma nel balancer) ed è sotto lock; `--project` con path inesistente non
crea più silenziosamente un progetto; `deps: null` nel DEPS non solleva più
`TypeError`; `build_waves` include `devops-engineer` (una issue DevOps in mezzo
spezzava ogni wave). Suite di test: **da 27 a 49 casi**, con i nuovi mirati ai
percorsi che il primo run attraversa davvero.

### Verifica: una `build` completa contro provider finti

Oltre agli unit test, l'orchestratore è stato esercitato **end-to-end** con CLI
finte che scrivono artefatti veri in un repo git vero: `build` completa (BAD →
TAD → IPD → fan-out di 3 issue con routing per label → qa → review per-issue →
docs) tutta `done`, review sempre su provider ≠ implementer, carichi finali
allineati (claude 4.6 / codex 4.3); rilancio → **11 unità su 11 saltate**;
`--parallel` → worktree isolati, branch integrati nel tree principale, cleanup
pulito. È la prova che mancava: non "il piano è giusto", ma "il ciclo di vita di
un run funziona".

## 15. Cosa resta da fare quando attivi gli abbonamenti

> La versione operativa di questa sezione — con checklist, comandi e cosa
> fare della risposta — è in **[FIRST_RUN.md](FIRST_RUN.md)**. Qui resta il
> perché; lì c'è il come.

Tutto ciò che resta richiede le CLI vere: nessuno di questi punti è verificabile
in dry-run.

1. Installare le CLI e fare login con gli **abbonamenti**.
2. `enabled = true` sui provider in `mentis.toml`.
3. **Correggere le righe `cmd`** (`--help` di ciascuna CLI). Punti noti, dai
   commenti del toml: (a) per Claude, `--permission-mode acceptEdits` non basta
   perché l'agente **esegua comandi** in headless — senza i permessi giusti
   scrive file ma non committa né esegue i test, e metà pipeline diventa muta;
   (b) i valori accettati da `--model` sono alias o id completi, non le stringhe
   attuali di `[model_map]`; (c) per Codex, `--reasoning` con ogni probabilità
   non esiste (si passa via `-c model_reasoning_effort=…`), la sandbox di default
   è read-only con la rete spenta, e su cartelle non-git serve
   `--skip-git-repo-check`.
4. Verificare il **reasoning per Claude**: oggi è solo un suggerimento nel
   prompt, e un `effortLevel` impostato globalmente nei settings dell'utente lo
   sovrascriverebbe comunque.
4bis. **Attivare il conteggio token reale**: aggiungere `--output-format json` al
   `cmd` di Claude (e `--json` a quello di Codex) e mettere `usage_json = true`.
   È ciò che trasforma il balancer da stima a misura — e senza cui le percentuali
   di quota restano un ordine di grandezza. Verificare che il testo dell'agente
   arrivi comunque: `parse_usage_envelope` estrae `result` dall'envelope, ma se il
   formato reale differisse, mentis ripiega sull'output grezzo senza rompersi.
5. Provare in quest'ordine: `bad --quality off` (una sola chiamata), poi
   `tad --compare` come pilota, poi `build` su un progetto piccolo con
   `--quality reflect` e **senza** `--parallel`.
6. Solo allora tarare con i dati veri: `[balance.window]` (le ore di ricarica
   sono una stima), i due timeout, e i pesi del balancer — leggendoli da
   `.mentis/logs/calls.jsonl` e da `mentis status`.

Un solo numero da tenere d'occhio dal primo giorno: **`max_calls_per_run`**
(default 60). Se un run si ferma dicendo che ha raggiunto il tetto, guarda
`calls.jsonl` prima di alzarlo — spesso il vero problema è un loop che non
converge, non un tetto troppo basso.
