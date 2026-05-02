---
activation: semantic
trigger: "Before building anything: search GitHub + web for existing tools. Use as-is, build on top, or borrow parts. Custom code only for the glue."
trigger_utterances:
  - "let's build a tool for this"
  - "we need to write a script that does X"
  - "I want to create a new app"
  - "design a system to handle Y"
  - "implement something that solves Z"
  - "we should make a daemon for this"
  - "create a library for handling X"
score_threshold: 0.42
---

# Never reinvent the wheel

Before writing custom code, look for existing open-source tools that solve
the same problem. Use as-is when possible, build on top when not, borrow the
useful parts when neither fits. Reserve custom code for the glue.

## Why

Reinventing is expensive in two directions: building takes time, and
maintaining your reinvention competes for attention with everything else
you'd rather work on. Off-the-shelf tools have ecosystems behind them; your
fork has you.

## How to apply

- Search GitHub topic tags + the package registry for your language.
- Skim the top 3 results to assess fit and maintenance status.
- If nothing fits exactly, find the closest match and ask: can I extend it,
  fork it, or use parts of it? Only build from scratch when the answer is no.
