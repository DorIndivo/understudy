# understudy

Record a few seconds of someone doing a real business task on macOS, and produce
a machine-readable description of *what they did and why* — detailed enough for
an agent to repeat the process.

## The idea

Screenshots alone aren't enough. A frame shows a cursor at (842, 331) and a
button that lit up; it doesn't tell you the button was named "Approve", that the
previous keystrokes were an invoice ID, or that the click *meant* "submit for
payment". So the recorder captures three synchronized channels on one clock:

| Channel | Source | Answers |
|---|---|---|
| Input events | Quartz `CGEventTap` | *when* and *where* — clicks, keys, scrolls |
| UI element | macOS Accessibility API | *what* — role, label, identifier, ancestor chain |
| Frames | `mss` + Pillow | *what it looked like* — before, after, and a close-up of the click |

The accessibility channel is what makes the output replayable. `AXButton
"Approve" in AXToolbar` can be queried and verified by an agent at replay time;
a coordinate breaks the moment a window moves. It's strictly additive though —
when it returns nothing (common in Electron apps), the trace still records
coordinates and frames, and the interpretation falls back to reading the images.

When a control exposes no label of its own, we harvest one from its contents or
from an adjacent caption — an icon-only button containing static text, or an
empty field next to "Invoice ID:". Harvested labels are tagged with their source
(`label_source`) and presented to the model as weaker evidence, because they are.
Panel-sized elements are excluded: a label only means something for a control,
and a large container adopting the text of a distant descendant produces
plausible-looking nonsense.

## Setup

```bash
uv sync
uv run understudy doctor        # checks Accessibility + Screen Recording
```

`doctor` will tell you exactly which System Settings panes to enable. Grant them
before recording — without them a recording looks successful but contains empty
elements and blank frames. A third permission, **Input Monitoring**, has no
query API; if recordings capture no clicks, that's the one to enable.

## Use

Five commands: `doctor`, `record`, `inspect`, `interpret`, `synthesize`.

### End to end

```bash
# once per machine - permissions and API key
uv run understudy doctor

# record the same process two or three times, varying the data that matters
uv run understudy record -s 45 -g "route the supplier invoice to the correct queue"
uv run understudy record -s 45 -g "route the supplier invoice to the correct queue"

# free: check the trace captured what you think it did
uv run understudy inspect recordings/<id>

# one run on its own -> sop.md + procedure.json
uv run understudy interpret recordings/<id> --model sonnet

# several runs compared -> one procedure with its branches
uv run understudy synthesize recordings/<id-A> recordings/<id-B> \
    --name invoice-routing --model sonnet
```

`interpret` is optional once you are synthesizing: `synthesize` reads `steps.json`
directly. Run it when you want to see a single run on its own terms.

**Always pass `--goal`.** One line costs you nothing and anchors the entire
interpretation: without it the model must reverse-engineer intent from clicks
alone, which is where confident-but-wrong readings come from. The goal is treated
as stated intent, not gospel — if the recording plainly shows something else, the
recording wins and the discrepancy lands in `gaps`.

**`--dry-run` before any paid call.** It builds the identical request and calls
`count_tokens` instead of generating, so you see the price before committing.

### Choosing a model

`--model opus | sonnet | haiku`, or a full model id. Default is `opus`.

| | cost, one interpretation | use for |
|---|---|---|
| `opus` | ~$0.13 | output that will actually drive an agent |
| `sonnet` | ~$0.08 | routine runs |
| `haiku` | ~$0.02 | "did this capture anything at all" |

Measured on the same recording. Haiku is 7x cheaper and still flagged the same
missing rule — but it reported `high` overall confidence on a trace where both
other models said `medium`, and confidence is what you would use to decide whether
a procedure is safe to act on. Cheap is fine for a smoke test, not for the
artifact you trust.

### Why record the same process more than once

A single recording shows *which path the operator took*. It cannot show *why*,
because nothing in one run distinguishes "this is the rule" from "this is what
happened that time". `synthesize` compares runs: where they agree is the
procedure, where they diverge is a decision, and the data that differs at the
divergence is the evidence for the condition.

```
BRANCH after step 2 on invoice_amount (low confidence)
    €420.00,   supplier 'Acme Stationery'    ->  Approve for payment
    €2,480.75, supplier 'Initech Consulting' ->  Escalate to finance

  rule:  low amount -> approve directly; high amount -> escalate to finance
  bound: threshold lies in the open interval (€420.00, €2,480.75); no run
         tested inside that range, so the exact cutoff cannot be determined
```

**Vary one thing at a time.** The run above changed supplier *and* amount
together, so the two are confounded — and the synthesis said so, naming supplier
identity as an equally supported explanation and marking the branch `low`. Two
runs differing in exactly one variable are worth more than two differing in three.

**Two runs is the minimum; three is better.** Two place a threshold only as an
interval. A third inside that interval narrows it. When the evidence is thin, the
output says which run would settle it — that is the next thing to record.

### Reading past runs

Every interpretation and synthesis is kept, never overwritten:

```bash
uv run understudy interpret recordings/<id> --list     # what runs exist
uv run understudy interpret recordings/<id> --show 2   # reprint run 2, free
```

## API key

`interpret` needs an Anthropic API key. It is resolved from two sources, first
match winning:

| Source | When it's right |
|---|---|
| `ANTHROPIC_API_KEY` env var | CI, or a key you already export for other tools |
| `.env` in the project root | a key scoped to this project alone; already gitignored |

For the environment variable, put it in your shell profile so it survives new
shells rather than exporting it per session:

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.zshrc && source ~/.zshrc
```

For a project-scoped key, `.env` in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Either way the key is never logged, never written into a recording, and always
masked when printed. `doctor` reports which source is active and whether the
value is even shaped like a key.

### Identity-linked keys

A key issued to an individual rather than an organisation must name the
workspace each request acts in, or the API rejects it with
`anthropic-workspace-id is required`. Set it the same way:

```bash
echo 'export ANTHROPIC_WORKSPACE_ID=wrkspc_...' >> ~/.zshrc && source ~/.zshrc
```

The id is in the URL when you open the workspace at
console.anthropic.com > Settings > Workspaces. It is an identifier, not a
secret. Organisation keys ignore it.

## Output

```
recordings/2026-09-02T10-42-11/
  manifest.json    session metadata, displays, permission state, clock origin
  events.jsonl     raw events, append-only
  steps.json       normalized semantic steps  <- the contract everything reads
  frames/          000_pre.webp  000_crop.webp  000_post.webp  ...
  sop.md           the process written for a human reviewer (newest run)
  procedure.json   the agent-executable form (newest run)
  interpretations/
    001/           sop.md, procedure.json, run.json
    002/           ...

syntheses/invoice-routing/
  interpretations/
    001/           the same three files, for a procedure with branches
```

`run.json` records how each run was made — model, effort, token counts, cached
tokens, cost — which is what makes two runs comparable. The root `sop.md` and
`procedure.json` mirror the newest run, so anything reading those keeps working.

`procedure.json` carries, per step: the **intent** in business terms, a
**target** (app, window, AX role/label/identifier, an **ordinal** like "2 of 3
AXButton" when the label does not distinguish it, coordinates only as a
fallback), an **input** with `{variable}` slots, a **postcondition**, and a
**confidence**. Alongside it are the inferred `variables` — the values that would
differ on another run — and `gaps`, an explicit list of what the recording
couldn't show.

A synthesis adds `branches`: for each decision point, the value observed in every
run, the path taken, the rule those runs are consistent with, the **bound** on
what is still unknown, and the **alternative explanations** the evidence cannot
rule out.

Read `gaps` first. A few seconds of recording captures one path: the happy path,
with no error branches. A confident, wrong procedure is the worst possible
output, so the model is asked to name what it couldn't determine. If `gaps` comes
back empty on an ambiguous recording, distrust the rest.

## Privacy

Two layers of redaction, both applied *before* anything reaches disk:

- **Structural** — keystrokes into a field the accessibility API reports as
  `AXSecureTextField` are never buffered; only a character count survives.
- **Pattern** — a regex denylist (API keys, tokens, JWTs, card numbers, SSNs)
  scrubs text typed into ordinary fields.

Recordings still contain screenshots of whatever was on screen. Review a
recording with `inspect` before sending it anywhere.

## Development

```bash
uv run pytest        # normalization, redaction, and request assembly
```

Normalization is deterministic and tested from hand-written event fixtures, so
most of the pipeline is verifiable without recording anything.

## Known gaps, and how they get closed

The output tells you where it is weak — read `gaps` and the per-step
`confidence` first. Beyond that, in rough order of value per unit of work:

1. **State the goal** (done) — `--goal` at record time.
2. **Harvest labels from neighbours** (done) — measured on real apps: VS Code's
   label rate roughly doubled before the panel guard, and holds a real gain after
   it; Chrome 26% → 30%.
3. **Ordinal disambiguation** (done) — an unlabeled element among identical
   siblings is only re-findable by *which one it is*. Steps carry
   `index_in_parent` / `same_role_siblings`, and ancestor paths carry per-hop
   ordinals. Verified in the wild: three identical "Select" buttons, correct one
   recorded as `2 of 3` and carried into `procedure.json`.
4. **Secure input markers** (done) — macOS turns on secure input mode inside a
   password field, which stops event taps seeing the keystrokes *at all*. That is
   a stronger privacy guarantee than any code here could make, but it used to
   mean the credential step vanished from the procedure entirely and a replaying
   agent would skip authentication. A click landing on a secure element now emits
   a `secure_input` step whose value is a variable supplied at run time.
5. **Multi-take alignment** (done) — `synthesize`. Values that differ across takes
   are demonstrably variables rather than suspected ones, and steps that diverge
   are branches with a stated rule, a bound on the unknown, and the alternative
   explanations the evidence cannot exclude.
6. **Turn `gaps` into an interview** (not built) — have the model ask the operator
   3–5 targeted questions about what it could not determine, then re-interpret
   with the answers. This is now the largest remaining payoff: it recovers error
   branches and business context that no amount of instrumentation can observe.
   `synthesize` already names the specific run that would settle an ambiguity,
   which is most of the question-generation problem solved.
7. **Scroll frames** (not built) — only clicks trigger frame capture, so scroll
   extents are inferred. Wants a debounced end-of-burst grab.
8. **CDP for browsers** (not built) — real DOM selectors instead of the
   accessibility tree. Browser AX trees are the thinnest place in the pipeline:
   `identifier` and `window` come back null on nearly every web target.

A note on expectations: none of this yields blind 100% reliability. What it
yields is *calibrated* confidence — steps marked `high` that really are reliable,
and `low` ones flagged for a human rather than silently guessed. An agent that
runs 85% autonomously and escalates the rest beats one that claims 100% and is
quietly wrong.

## Not built yet

- **Replay.** `procedure.json` is designed to be executed, but no executor
  exists. Targets, ordinals, postconditions and verifications are there for it.
- **DOM selectors.** Browser steps use the accessibility tree, which is coarser
  than real CSS selectors. `Element.dom_selector` is reserved for a future Chrome
  DevTools Protocol source.
- **Branch conditions from a single run.** By construction: one recording cannot
  distinguish a rule from a choice. Record the process more than once and use
  `synthesize`.
