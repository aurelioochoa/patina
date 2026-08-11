<p align="center">
  <img src="assets/banner.jpg" alt="patina — the word revealed in bright copper where the tarnish has worn away, with Clawd struck into the corner as a maker's mark" width="100%">
</p>

<p align="center">
  <strong>Claude Code forgets everything when a session ends.<br>This remembers the parts worth keeping.</strong>
</p>

<p align="center">
  <a href="#español">🇪🇸 Léeme en español</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#configuration">Configuration</a>
</p>

---

You tell Claude "stop formatting it like that." You explain, for the third time, that this
project runs tests with `pnpm`, not `npm`. You work out a fiddly fix for a build error at
11pm. Next session, all of it is gone.

patina is a background loop that reads your finished sessions and turns what it learned into
skills — the files Claude actually loads next time. It runs after you've closed the terminal,
costs a few cents a session, and never changes anything without asking you first.

It's a port of [Hermes Agent](https://github.com/hermes-agent)'s self-improvement loop to
Claude Code, and it's named for the finish that builds on a surface through use — the kind
worth keeping rather than polishing off.

---

## What it actually looks like

You finish a session. A few minutes later, quietly, in the background, a small model reads
what happened and decides whether anything is worth keeping. Nothing appears in your terminal.

Next time you sit down, you check the queue:

```
$ /patina:pending

1 pending:

  NEW   reviewing-migrations-a3f9c1
        skill: reviewing-migrations  +64/-0 lines
        based on 2 lesson(s), strongest: high
```

You look at what it wants to add — and, more importantly, *why*:

```
$ /patina:approve reviewing-migrations-a3f9c1

what it says it learned:
  [correction, high] Migrations that add a NOT NULL column with a default rewrite
      the whole table on Postgres below 11, which locks it for the duration.
      evidence: "no — that one rewrites the table, we can't ship that on prod"
```

If the claim is right, you approve it and Claude knows it from then on. If it's wrong, you
reject it and it's gone. **Nothing reaches your skill library unless you say so.**

---

## Quick start

The plugin is the easy path — it brings the commands and the hooks together, and nothing
touches your `settings.json`.

```sh
git clone https://github.com/aurelioochoa/patina
claude --plugin-dir ./patina        # try it for one session
```

Happy with it? Install it properly:

```sh
claude plugin marketplace add ./patina
claude plugin install patina@patina
```

That's it. The loop starts working at the end of your next session.

### The commands

| Command | What it does |
|---|---|
| `/patina:status` | Is the loop running? What is it costing? Is any of it being used? |
| `/patina:pending` | What it wants to change |
| `/patina:approve <id>` | Apply one — or `--all` if you're feeling brave |
| `/patina:reject <id>` | Discard one |
| `/patina:patination` | Review the session you're in right now, without waiting for it to end |
| `/patina:curate` | Tidy the library now, instead of waiting for the weekly pass |
| `/patina:pause` | Stop the scheduled work (`resume` to start again) |

Only you can run these. They're marked `disable-model-invocation: true`, which is deliberate
rather than tidy — they spend money and change what loads into every later session. A model
free to decide that now is a good moment for `/patina:approve --all` would defeat the whole
point of having a queue.

---

## How it works

<img src="assets/loop.jpg" alt="A diagram engraved into a verdigris copper plate: six boxes in a ring — SESSION, REFLECT, PLACE, QUEUE, LIBRARY, CURATOR — joined by arrows flowing clockwise into a closed loop." width="100%">

### Two passes, and the split matters

<img src="assets/two-passes.jpg" alt="A diagram engraved into a copper plate: SESSION to REFLECT on the left, PLACE to QUEUE on the right, with a slot cut clean through the metal between them. A copper tag labelled LESSONS bridges the gap — the only thing that crosses." width="100%">

A review is two separate forks, not one.

**Reflect** reads the session and returns a list of lessons — a claim, the evidence for it,
and how confident it is. It has no tools, no directory access, and no way to write anything
at all.

**Place** takes that list and works out where each lesson belongs in your library. It never
sees the transcript.

Two things follow. The cheap one: most sessions teach nothing, and finding that out costs one
tool-less pass instead of a full write-capable fork.

The important one: **the pass that can write files never reads attacker-controlled text.**
Your transcripts are full of web pages, file contents and command output — and a skill's
description gets injected into the system prompt of every session afterwards. Fencing a
prompt asks a model not to be fooled. This arranges for it never to be asked.

### Nothing goes live unreviewed

<img src="assets/queue.jpg" alt="A diagram engraved into a verdigris copper plate: a box labelled QUEUE forking into two paths that never rejoin — up through APPROVE to LIBRARY, down through REJECT to DISCARD." width="100%">

Neither fork can reach your real skill library. Both write into a scratch copy and aren't
even told where the real one is. Afterwards, every difference is filed for you to approve or
reject.

```sh
pending.py list                 # what the loop wants to change
pending.py show <id>            # the claims, then the checks, then the diff
pending.py approve <id>         # apply it, and trust that skill from now on
pending.py reject <id>          # discard it
pending.py approve --all
```

`show` leads with what the change *claims to have learned* and the evidence for it, because
that's the question a diff can't answer. Then the mechanical checks: is the name valid, is
the description too long, is it written in first person, does its trigger collide with a
skill you already have. Anything that would stop the skill loading at all blocks approval —
`--force` if you really mean it.

Patches go through the queue too, not just new skills. Patching is the loop's most common
action, and one that skipped review could quietly change a skill you already trust.

A note on rejection: it removes the entry, and for a *new* skill it also records a refusal
that the use-time gate honours. It does **not** stop a later session proposing the same
thing again — nothing in the capture path consults that record.

**Why quarantine at all, rather than just prompting when a skill is used?** Because a skill's
name and description go into the system prompt every single session, whether or not it's ever
invoked. A bad skill sitting in your library costs context and nudges behaviour without the
`Skill` tool ever firing. Gating invocation can't fix that. Keeping it out of the library can.

### The use-time backstop

`skillgate.py` runs on the `Skill` tool and refuses auto-created skills you haven't blessed.
Quarantine means it usually has nothing to do — it's there for the gaps quarantine misses, like
a skill approved once and later edited by hand.

| State | Behaviour |
|---|---|
| `always` | allow, never ask again |
| `never` | deny, never ask again |
| session | allow for this session only |
| unset | ask |

Your hand-written skills and plugin skills are never gated. And if the gate itself breaks, it
allows — a bug in it must never lock you out of your own skills.

---

## Living with it

```sh
review.py  --status          # runs, spend, no-op rate, how much is ever used
curator.py --status          # intervals, sweep backlog, run count
curator.py --run             # run the curator now, ignoring the interval
curator.py --curate-only     # curate without sweeping first
curator.py --sweep-only      # catch up on missed sessions, skip consolidation
curator.py --pause           # stop the loop without uninstalling
git -C ~/.claude/skills log  # everything it has ever written
```

Two numbers are worth watching. If **every review is a no-op**, the prompt isn't reaching the
model — that's a broken loop wearing the costume of a quiet one. If skills are **written but
never loaded**, the loop is working and the library still isn't: an unused skill is almost
always a description that never matched anything, rather than a body that was wrong.

### Handing a skill over

The loop only writes to skills carrying the marker, so you decide what it may touch:

```yaml
---
name: my-skill
description: ...
metadata:
  autoManaged: true
---
```

Remove the marker to take it back.

---

## Installing as scripts instead

If you'd rather not use the plugin, `install.sh` puts the scripts in `~/.claude/patina` and
wires the hooks into your `settings.json` by hand.

```sh
./install.sh                  # copy scripts, init the audit repo
```

This does **not** activate anything, on purpose. A broken `SessionStart` hook fires on every
session, and you'd be debugging it inside the tool it's breaking. Check it by hand first:

```sh
# 1. Dry run — builds the prompts, forks nothing
python3 ~/.claude/patina/review.py \
    --transcript ~/.claude/projects/<slug>/<session>.jsonl --dry-run

# 2. Rehearsal: real auth, redirected writes, live library untouchable
export PATINA_SKILLS_DIR=/tmp/rehearsal/skills
export PATINA_STATE_DIR=/tmp/rehearsal/state
export PATINA_PROJECTS_DIR=/tmp/rehearsal/projects   # see the warning below
python3 ~/.claude/patina/review.py --transcript <transcript>
python3 ~/.claude/patina/pending.py list
git -C /tmp/rehearsal/skills log -p

# 3. Only once that looks right
./install.sh --register-hooks
```

⚠️ **Redirect `PATINA_PROJECTS_DIR` too**, or point it somewhere empty. It's where the sweep
looks for unreviewed sessions, and it defaults to your real `~/.claude/projects` even when
everything else is redirected — so `curator.py --run` during a rehearsal will find your
genuine backlog and fork a batch of real reviews before it ever reaches the curator. Use
`curator.py --curate-only` when the curator is what you meant to test.

`./install.sh --uninstall` removes the scripts and the hooks, and leaves your skills and their
git history alone.

⚠️ **Don't do both.** The plugin ships the same three hooks, so registering them in
`settings.json` as well fires each one twice per session. The loop survives it — the review
lock defers the duplicate — but the sweep can then start twice as many forks, and a doubled
spend ceiling isn't a ceiling. `--register-hooks` detects an installed plugin and refuses; a
plugin loaded with `--plugin-dir` leaves no trace on disk, so that case is on you.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PATINA_MODEL` | `sonnet` | model for both review and curator |
| `PATINA_TIMEOUT` | `600` | per-review timeout, seconds |
| `PATINA_CURATOR_TIMEOUT` | `900` | curator timeout, seconds |
| `PATINA_SKILLS_DIR` | `~/.claude/skills` | redirect writes (rehearsal) |
| `PATINA_STATE_DIR` | `~/.claude/patina` | redirect state |
| `PATINA_PROJECTS_DIR` | `~/.claude/projects` | redirect transcript discovery |
| `PATINA_WORK_DIR` | `~/.cache/patina` | scratch tree handed to the fork |
| `PATINA_MAX_USD` | `0.50` | hard spend ceiling per fork |
| `PATINA_FALLBACK_MODEL` | unset | model to fall back to when the primary is unavailable |

The pre-rename `CLAUDE_SELF_IMPROVE_*` spellings still work as a fallback, so an old export in
a shell profile won't silently stop being read.

Intervals live in `state.json`:

| Key | Default | Purpose |
|---|---|---|
| `interval_hours` | `168` | how often the curator consolidates the library |
| `sweep_interval_hours` | `24` | how often the sweep picks up missed sessions |
| `sweep_limit` | `10` | forks per sweep — a spend ceiling as much as a batch size |
| `min_tool_calls` | `8` | below this *and* `min_user_turns`, a session isn't reviewed |
| `min_user_turns` | `5` | either signal alone is enough to earn a review |

---

## Under the hood

- A `SessionEnd` hook forks a detached, headless `claude -p` that reads a bounded digest of the
  transcript and decides whether anything is worth keeping. Sessions too small to have taught
  anything aren't forked at all — the prompt pushes hard to find a lesson, and aimed at a
  three-message session that pressure just manufactures one.
- Every session, reviewed or not, records which skills it loaded. That count is the only
  feedback the loop has: it decides what the curator treats as stale, and it's the difference
  `--status` reports between skills written and skills used.
- A `SessionStart` hook checks two intervals and forks whatever is due. No daemon. Daily, a
  sweep catches sessions whose `SessionEnd` hook never fired — a hard kill, a closed terminal,
  a crash. Weekly, a curator pass consolidates overlapping skills and archives stale ones.
- A review that fails is left unwatermarked so the sweep retries it, up to three attempts. The
  first failure in the wild was an account limit, which is transient by definition; treating
  that as reviewed would lose the session permanently.
- Writes are confined to skills marked `metadata.autoManaged: true`, enforced both in the
  prompt and by a post-run check that reverts anything outside the allowlist.
- Every fork carries a hard dollar ceiling, and a run that hits it is logged as exactly that
  rather than as a crash — retrying can't fix a limit that's simply too low.
- Every write is a git commit in a local audit repo, plus a line in an append-only log.

## Design notes

Two things carried over from Hermes, because they're what separates a useful skill library
from a junk drawer:

**The preference order.** Patch a loaded skill → patch an existing umbrella → add a
`references/` file → only then create a new skill. Without it you get a flat list of
one-session-one-skill entries.

**The "Do NOT capture" list.** Environment-dependent failures, negative claims about tools,
transient errors and unresolved failures are all explicitly excluded. This is what stops
"browser tools do not work" from hardening into a permanent self-inflicted constraint the
agent cites against itself months after the problem was fixed.

And three added since, each answering a failure the original design left open:

**Reflect and place are separate forks.** Splitting evaluation from curation is
[ACE](https://arxiv.org/abs/2510.04618)'s measured result; here it also buys the injection
boundary described above, plus an early exit on the common case.

**The curator can act.** Archiving and consolidation both need to move or remove files, which
a capture pass that only walked the proposed tree couldn't see. Both of the curator's headline
actions used to be silent no-ops.

**Usage is measured.** Age alone can't tell a skill nobody needs from one that quietly does
its job every month. The curator now sees load counts, and a skill used recently is never
stale.

Full detail in [the original design](docs/superpowers/specs/2026-08-07-claude-self-improvement-loop-design.md)
and [the 2026-08-10 amendment](docs/superpowers/specs/2026-08-10-two-pass-review-and-measured-library.md).

## Images

Every image here was generated with Higgsfield. The prompts, the reasoning behind each one,
and the notes on regenerating them are in
[`assets/image-prompts.md`](assets/image-prompts.md).

---

## Español

**Claude Code olvida todo al terminar una sesión. Esto recuerda lo que vale la pena.**

Le dices a Claude "deja de darle ese formato". Le explicas, por tercera vez, que este proyecto
usa `pnpm` y no `npm`. Resuelves a las 11 de la noche un error de build que costó trabajo
entender. En la siguiente sesión, nada de eso existe.

patina es un proceso en segundo plano que lee tus sesiones terminadas y convierte lo aprendido
en *skills*: los archivos que Claude sí carga la próxima vez. Corre después de que cerraste la
terminal, cuesta unos centavos por sesión, y **nunca cambia nada sin preguntarte antes**.

### Cómo funciona

Cada revisión son dos procesos separados, y esa separación es el punto:

- **Reflect** lee la sesión y devuelve una lista de lecciones: una afirmación, la evidencia
  que la respalda y qué tan seguro está. No tiene herramientas ni acceso a directorios: no
  puede escribir nada.
- **Place** toma esa lista y decide dónde va cada lección en tu biblioteca. Nunca ve la
  transcripción.

De ahí salen dos cosas. La barata: la mayoría de las sesiones no enseñan nada, y descubrirlo
cuesta una sola pasada sin herramientas.

La importante: **el proceso que puede escribir archivos nunca lee texto que un atacante
podría controlar.** Tus transcripciones están llenas de páginas web, contenido de archivos y
salida de comandos, y la descripción de un skill entra al prompt del sistema en todas las
sesiones siguientes. Blindar un prompt es pedirle al modelo que no se deje engañar; esto hace
que nunca se le pregunte.

### Nada se aplica sin tu revisión

Ninguno de los dos procesos alcanza tu biblioteca real. Ambos escriben en una copia temporal y
ni siquiera saben dónde está la verdadera. Después, cada diferencia queda en una cola para que
tú la apruebes o la rechaces.

`show` empieza por lo que el cambio **dice haber aprendido** y su evidencia, porque esa es la
pregunta que un diff no puede responder. Luego vienen las verificaciones mecánicas: si el
nombre es válido, si la descripción es demasiado larga, si está escrita en primera persona, si
su disparador choca con un skill que ya tienes. Cualquier problema que impediría que el skill
cargue bloquea la aprobación.

**¿Por qué una cola, en vez de solo preguntar al usarlo?** Porque el nombre y la descripción
de un skill entran al prompt del sistema en cada sesión, se invoque o no. Un mal skill guardado
en tu biblioteca consume contexto y sesga el comportamiento sin que la herramienta `Skill` se
active nunca. Controlar la invocación no arregla eso; mantenerlo fuera de la biblioteca sí.

### Instalación

```sh
git clone https://github.com/aurelioochoa/patina
claude --plugin-dir ./patina        # pruébalo por una sesión
```

Si te convence, instálalo de forma permanente:

```sh
claude plugin marketplace add ./patina
claude plugin install patina@patina
```

Listo. El proceso empieza a trabajar al final de tu siguiente sesión.

### Comandos

| Comando | Qué hace |
|---|---|
| `/patina:status` | ¿Está corriendo? ¿Cuánto cuesta? ¿Se usa algo de lo que escribió? |
| `/patina:pending` | Qué quiere cambiar |
| `/patina:approve <id>` | Aplicar uno, o `--all` para todos |
| `/patina:reject <id>` | Descartar uno |
| `/patina:patination` | Revisar la sesión en curso, sin esperar a que termine |
| `/patina:curate` | Ordenar la biblioteca ahora, sin esperar a la pasada semanal |
| `/patina:pause` | Detener el trabajo programado (`resume` para reanudar) |

Solo tú puedes ejecutarlos. Llevan `disable-model-invocation: true` a propósito: gastan dinero
y cambian lo que se carga en todas las sesiones siguientes. Un modelo con libertad para
decidir que este es buen momento para `/patina:approve --all` anularía el sentido de tener una
cola.

### Entregarle un skill

El proceso solo escribe en skills que llevan la marca, así que tú decides qué puede tocar:

```yaml
---
name: mi-skill
description: ...
metadata:
  autoManaged: true
---
```

Quita la marca para recuperarlo.

### Dos números que vale la pena vigilar

Si **todas las revisiones no hacen nada**, el prompt no está llegando al modelo: es un proceso
roto disfrazado de biblioteca tranquila. Si se **escriben skills pero nunca se cargan**, el
proceso funciona y la biblioteca no: un skill sin uso casi siempre tiene una descripción que
nunca coincidió con nada, no un contenido equivocado.

La configuración completa, las variables de entorno y los detalles internos están en las
secciones en inglés de arriba.

---

## License

MIT
