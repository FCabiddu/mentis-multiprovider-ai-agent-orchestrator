# Primo run reale — punti aperti

Tutto ciò che segue è rimasto aperto **per un solo motivo**: non è verificabile
senza le CLI installate e gli abbonamenti attivi. Il resto del sistema è coperto
da 68 test e da simulazioni end-to-end contro CLI finte (comprese quelle con una
quota vera che si esaurisce).

Ordine consigliato: A → B → C. Il punto **A1 è bloccante**: se sbagliato, gli
agenti scrivono file ma non committano e non eseguono i test, e mezza pipeline
diventa muta senza dare errore.

---

## A. Prima di lanciare — flag delle CLI

`mentis` fa un preflight (binario nel PATH, segnaposti validi) e si ferma prima
di spendere quota, ma **non può validare i flag**: quello è un `--help` a mano.

### A1. Claude: l'agente può ESEGUIRE comandi? ⚠️ bloccante

```bash
claude --help | grep -iE "permission|allowed-tools|dangerously"
```

`--permission-mode acceptEdits` autorizza le *modifiche ai file*, ma in headless
non basta perché l'agente esegua comandi. Developer, qa-engineer e devops-engineer
hanno bisogno di `git commit`, install e esecuzione test: senza i permessi giusti
scrivono il codice e poi non lo committano né lo verificano — e l'unità risulta
comunque riuscita.

- [ ] Verificato quale combinazione di flag consente Bash in `-p`
- [ ] Aggiornata la riga `cmd` di `[providers.claude]`
- [ ] Provato con un `bad` su progetto giocattolo: il commit avviene davvero?

### A2. Claude: i valori di `--model`

Oggi `[model_map.claude]` usa gli **alias** (`opus`/`sonnet`/`haiku`), che sono
sempre validi e stabili fra le release. Gli id completi (`claude-opus-5`,
`claude-opus-4-8`, `claude-sonnet-5`, `claude-haiku-4-5`) vanno bene se vuoi
pinnare una versione — ed è la forma su cui `doctor` sa ragionare, perché
confronta i numeri di versione.

- [ ] Deciso alias o id completi
- [ ] Se id completi: `mentis doctor --provider claude --models "..."` per allineare

### A3. Codex: quattro cose insieme

Dalla documentazione attuale, la riga `cmd` di `[providers.codex]` è quasi
certamente sbagliata su tutti e quattro i punti:

| Cosa | Problema | Da verificare |
|---|---|---|
| `--reasoning` | con ogni probabilità **non esiste** | si passa via `-c model_reasoning_effort="..."` o profili |
| sandbox | default **read-only** → non scrive nulla | serve `--sandbox workspace-write` o `--full-auto` |
| rete | **spenta** di default nella sandbox | senza, niente `npm install`, `git push`, ricerche web |
| cartella non-git | rifiuta di partire | `--skip-git-repo-check` |

- [ ] `codex --help` e `codex exec --help` letti, riga `cmd` riscritta
- [ ] Verificato che gli id `gpt-5.6-sol/terra/luna` siano accettati

### A4. Il `effortLevel` globale

`~/.claude/settings.json` contiene `"effortLevel": "xhigh"`. Vale per ogni
chiamata a Claude Code, quindi **sovrascrive** il `reasoning` per-agente di mentis
(che per Claude è già solo un suggerimento nel prompt) e fa consumare più quota.

- [ ] Deciso se lasciarlo, o usare un settings di progetto nel target

---

## B. Durante il primo run — protocollo per non bruciare la quota

Una `build` da 10 issue coi default può generare **decine di sessioni CLI**.
C'è un tetto di sicurezza (`max_calls_per_run = 60`) che ferma il run salvando lo
stato, ma la prima volta conviene andare per gradi:

```bash
mentis bad  "..." --project ~/dev/prova --quality off      # 1 sola chiamata
mentis tad  "..." --project ~/dev/prova --compare          # il pilota del confronto
mentis build "..." --project ~/dev/prova --quality reflect # 2-3 issue, SENZA --parallel
```

- [ ] `bad`: il contratto `[[MENTIS-RESULT]]` torna, l'artefatto è scritto
- [ ] `tad --compare`: due output affiancati in `.mentis/compare/`
- [ ] `build` piccola: fan-out, review cross-provider, nessuna escalation spuria
- [ ] Solo dopo: `--parallel` e `--quality full`

---

## C. Dopo il primo run — calibrare sui dati veri

Tutto ciò che segue è **stima** finché non hai numeri reali. `mentis status`
mostra affiancate la colonna `[STIMA]` e `[OSSERVATO]` proprio per questo.

### C1. Attivare il conteggio token reale

È il passaggio che trasforma il balancer da stima a misura.

| CLI | Flag da aggiungere al `cmd` | Cosa restituisce |
|---|---|---|
| Claude Code | `--output-format json` | `usage.input_tokens`, `usage.output_tokens`, `total_cost_usd` |
| Codex | `--json` | JSONL; `token_count` porta i totali cumulativi |

Poi `usage_json = true` sul provider. `parse_usage_envelope` estrae il testo
dall'envelope e, se il formato reale differisse, ripiega sull'output grezzo senza
rompersi. Verificato in simulazione: il conteggio coincide **esattamente** con la
verità.

- [ ] Flag aggiunti, `usage_json = true`
- [ ] Un run di prova: compare la riga `⛁ N token consumati (dato della CLI)`
- [ ] Il testo dell'agente arriva ancora (contratto e VERDICT parsati)

### C2. La percentuale di quota

**Nessuna CLI espone la quota residua** (`/usage` e `/status` sono solo
interattivi). Il denominatore si impara: al primo rate-limit vero, i token
consumati fino a quel momento *sono* il tetto, e `mentis balance` inizia a
mostrare una percentuale. Prima di allora dice «IGNOTA» invece di inventare.

Lo scarto misurato in simulazione è 5–9 punti, sempre **per eccesso** (mentis si
crede più consumato di quanto sia): per una rete di sicurezza è la direzione
giusta.

- [ ] Dopo il primo rate-limit: `mentis balance` mostra il tetto osservato
- [ ] La percentuale è plausibile rispetto a `/usage` letto in interattivo

### C2bis. Verificare la scansione delle trascrizioni

`usage_scan` è attivo di default su Claude e legge `~/.claude/projects`. È ciò
che rende visibile **anche il consumo interattivo**: senza, mentis vedrebbe solo
le proprie chiamate e crederebbe la quota intatta mentre l'hai già spesa altrove.

```bash
mentis balance     # la riga [claude] deve dire "fonte: trascrizioni locali"
```

- [ ] La fonte è «trascrizioni», non «solo le chiamate di mentis»
- [ ] Il numero è dello stesso ordine di grandezza di quanto hai lavorato oggi
- [ ] Per Codex: verificato se `~/.codex` tiene una history con i token, e in
      caso affermativo impostato `usage_scan` anche lì

### C3. I numeri da ritarare

Tutti in `config/mentis.toml`, tutti dichiarati come stime:

| Chiave | Oggi | Come tararla |
|---|---|---|
| `[balance.window]` | 5h | ore reali fra un esaurimento e la ricarica |
| `[balance.tier]` | frontier 3.0 / balanced 1.0 / fast 0.4 | rapporto token osservato fra i tier |
| `[balance.weights]` | review 1.0, reflect 0.5, evaluate 0.3 | token medi per tipo di operazione |
| `timeout_implementing_seconds` | 5400 | durata reale di uno step developer |
| `max_calls_per_run` | 60 | chiamate di una build tipica ×1,5 |

La materia prima è in `.mentis/logs/calls.jsonl` (`chars_in`, `chars_out`,
`tokens` quando `usage_json` è attivo).

- [ ] Finestra osservata almeno una volta
- [ ] Pesi confrontati coi token reali per tipo di agente

---

## D. Limiti noti che restano

Non sono bug e non si chiudono con la calibrazione: sono vincoli.

- **Il consumo nel browser (claude.ai) non è visibile.** Quello dalla CLI invece
  sì: `usage_scan` legge le trascrizioni locali di Claude Code
  (`~/.claude/projects/**/*.jsonl`), che contengono il blocco `usage` di ogni
  messaggio — sessioni interattive comprese. È attivo di default; verifica con
  `mentis balance` che la fonte dica «trascrizioni». Per Codex non c'è ancora un
  equivalente verificato: lì il conteggio resta quello delle sole chiamate di
  mentis, finché non controlli se `~/.codex` tiene una history analoga.
- **Il contesto effimero non è trasferibile.** Uno switch di provider a metà step
  perde il ragionamento vivo della CLI. Mitigato dalla nota di handoff su disco e
  dal checkpoint per-unità, non eliminato.
- **`--compare` ha senso solo su pipeline a uno step** (`tad`, `bad`, `mvp`): su
  `build` ogni step gira isolato e non vede gli artefatti del precedente. mentis
  lo avvisa.

---

## E. Non fatto per scelta (v2)

- Auto-calibrazione dei pesi dai log invece che a mano (il dato ora c'è).
- Rilevare la finestra di ricarica empiricamente (429 → primo successo) invece di
  leggerla da `[balance.window]`.
- Switch proattivo al ~90% di quota, ora che una percentuale esiste.
