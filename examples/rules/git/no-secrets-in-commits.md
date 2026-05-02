---
activation: semantic
trigger: "Before any git commit: scrub commit message + staged diff for API keys, hostnames, internal IPs, paths. Use generic placeholders."
trigger_keywords:
  - '\bgit\s+commit\b'
  - '\bgit\s+push\b'
  - '\b(open|create|make)\s+(a\s+)?PR\b'
  - '\bship\s+(this|it|that|the)\b'
  - '\bcut\s+(a\s+)?(release|tag)\b'
trigger_utterances:
  - "let me commit and push"
  - "ready to ship the release"
  - "open a PR for this"
  - "merge this branch"
  - "tag the release"
score_threshold: 0.45
---

# No secrets in commits

Do not commit secrets, internal IPs, hostnames, or local paths. Public
repositories index forever; private repos go public eventually.

## How to apply

- Scrub the commit message AND the staged diff.
- Replace internal paths (`/home/user/...`, `/srv/...`) with relative or
  generic placeholders.
- Replace internal IPs (`10.x`, `192.168.x`, VPN/tailnet IPs) with `<host>`.
- Strip API/Telegram tokens.

## Pair this rule with a code-enforced PreToolUse hook

This rule is a behavioural reminder. The actual enforcement should be a
mechanical guard that runs `git diff --cached` and pattern-matches before
the commit lands.
