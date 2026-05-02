# bouncer

> **Predicate-gated rule injection for long-running LLM sessions.**

A small daemon that decides which behavioural rules from a markdown directory
are relevant to the current user prompt, and returns just those — keeping the
per-prompt context bounded as your rule library grows.

Built for [Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
sessions, but works with any LLM agent that has a hook or middleware capable
of mutating the system context per prompt.

## The problem

When you re-inject a "hot list" of behavioural rules into every LLM prompt to
fight long-session drift, the list grows. At ~10 rules / ~700 words it works.
At ~30+ it dilutes — the model treats the wall of bullets as boilerplate.

Frequency-based promotion is the obvious answer and the wrong one: every rule
is equally important *when its situation arises*. The right axis is
**situational relevance**, not historical frequency.

## The pattern (cross-domain)

Lint engines (ESLint, Semgrep, Rubocop), policy engines (OPA, Drools), and
linter packs (MegaLinter) all solved scaling-rule-sets the same way decades
ago: each rule carries its own activation predicate. The runtime evaluates
predicates against the current context and admits only matching rules. Total
injected size stays bounded by the situational match, not the total rule count.

bouncer applies that pattern to LLM behavioural rules, with two predicate
styles per rule:

- **Hard filter** (`trigger_keywords`): regex matched against the prompt.
  Cheap, deterministic, predictable. Right for high-precision intents
  ("ask copilot to review", "git commit").
- **Semantic match** (`trigger_utterances`): per-rule embedding prototype
  computed from example phrasings; cosine similarity vs prompt embedding.
  Catches paraphrases the regex misses ("ship this release" → commit rules).

Both styles run per prompt; results are merged, deduped, capped at K=3.
Plus a small **always-on** core (`_always/` subdir) that escapes gating
entirely — for rules whose trigger is the response itself, not the prompt.

## Architecture

```
                ┌──────────────────────┐
                │   user prompt        │
                └──────────┬───────────┘
                           │
                ┌──────────▼───────────┐
                │  UserPromptSubmit    │
                │  hook (your code)    │
                └──────────┬───────────┘
                           │ POST /route
                ┌──────────▼───────────┐
                │   bouncer daemon     │
                │  ┌─────────────────┐ │
                │  │ keyword pass    │ │ ← trigger_keywords regex
                │  │ semantic pass   │ │ ← cosine vs precomputed
                │  │ merge + cap K=3 │ │   rule embeddings
                │  └─────────────────┘ │
                └──────────┬───────────┘
                           │ {always:[...], matched:[...]}
                ┌──────────▼───────────┐
                │  hook injects into   │
                │  additionalContext   │
                └──────────────────────┘
```

The daemon stays warm (encoder cold-start ~10 s, kept resident).

## Installation

```bash
git clone https://github.com/Joncik91/bouncer
cd bouncer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Usage

### 1. Author your rules

Create a directory with two subdirs:

```
rules/
├── _always/                 # always-injected
│   └── be-terse.md
├── git/                     # situational
│   └── no-secrets-in-commits.md
└── output/
    └── prefer-bullets.md
```

Each rule is a markdown file with YAML frontmatter:

```markdown
---
activation: semantic                         # always | semantic | manual
trigger: "Before commit → scrub for secrets" # one-line summary, returned in /route
trigger_keywords:                            # optional, regex; high-precision matches
  - '\bgit\s+commit\b'
  - '\bgit\s+push\b'
trigger_utterances:                          # required for semantic if no keywords
  - "let me commit and push"
  - "ship the release"
  - "open a PR"
score_threshold: 0.45                        # optional, default 0.35
---

# Rule body, never trimmed by bouncer — return for the model to consult later.

Rationale, examples, edge cases live here. The daemon only surfaces the
`trigger:` one-liner per /route call.
```

For `activation: always`, only `activation` and `trigger` are required.

### 2. Run the daemon

```bash
RULES_DIR=./rules .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8765
```

Or as a systemd service (template under `examples/`).

### 3. Wire your hook

Your UserPromptSubmit hook (or equivalent) POSTs to `/route`:

```python
import json, urllib.request
body = json.dumps({"prompt": user_prompt}).encode()
req = urllib.request.Request("http://127.0.0.1:8765/route",
    data=body, headers={"Content-Type": "application/json"})
routed = json.load(urllib.request.urlopen(req, timeout=1.5))
# routed = {"always": [...], "matched": [...]}
```

Then format and inject into the model's context per your platform's hook
interface (e.g. Claude Code's `additionalContext` field).

**Always implement a fail-open fallback** — if the daemon is unreachable,
log it and emit a degraded notice so the model knows it has reduced rule
context.

## API

### `GET /healthz`

```json
{"ok": true, "always": 7, "semantic": 6, "invalid": 0}
```

### `POST /route`

```json
{"prompt": "let me commit this PR"}
```

→

```json
{
  "always": [
    {"slug": "be-terse", "path": "_always/be-terse.md",
     "trigger": "...", "activation": "always"}
  ],
  "matched": [
    {"slug": "no-secrets-in-commits", "path": "git/no-secrets-in-commits.md",
     "trigger": "...", "activation": "semantic", "match_via": "keyword"}
  ]
}
```

`match_via` is `keyword` or `semantic` for matched rules; `null` for always.

### `POST /reload`

Re-reads the rules directory + rebuilds the index. Returns the same shape
as `/healthz`. Use after editing a rule's frontmatter.

## Routing pipeline

1. Always-on rules → unconditionally included.
2. **Keyword pass:** evaluate every semantic rule's `trigger_keywords` regex
   against the prompt. Collect matches.
3. **Semantic pass:** direct cosine similarity between prompt embedding and
   per-rule prototype embeddings (mean of utterances). Skips rules already
   matched in step 2 (saves the lookup).
4. Merge keyword + semantic, dedup by slug, cap at K=3.

A short-prompt length filter and a length-aware threshold bump keep noise
down on terse user inputs ("yes", "ok", short follow-ups).

## Configuration

| Env var | Default | Notes |
|---|---|---|
| `RULES_DIR` | `./rules` | Path to your rules directory |
| `MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` | Any HuggingFace encoder |
| `DEFAULT_THRESHOLD` | `0.35` | Per-rule `score_threshold` overrides |
| `TOP_K` | `3` | Hard cap on matched rules per prompt |

## Testing

`test-replay.py` walks a transcript file (any `.jsonl` with `{type: "user", message: {content: ...}}` records) and prints what would fire per prompt. Useful for tuning thresholds against real history before going live.

```bash
.venv/bin/python3 test-replay.py path/to/session.jsonl
```

## Why a daemon

The HuggingFace encoder takes ~10 s to load. Per-prompt subprocess spawn is
prohibitive. The daemon keeps the model resident; a hook hits localhost in
under 50 ms per request after warm-up. Use `systemctl --user` or similar for
non-root setups.

## Status

v0.1 — works on the creator's box. Open-sourcing for early feedback.
Single-machine. No persistence (rules reloaded fresh each daemon start).

## License

MIT.
