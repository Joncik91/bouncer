#!/usr/bin/env python3
"""Reference UserPromptSubmit hook integration with bouncer.

Adapt the I/O envelope to your platform. The core pattern (POST prompt,
get back always + matched, format, inject) is identical across platforms.

This example is shaped for Claude Code's hook contract.
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROUTER_URL = "http://127.0.0.1:8765/route"
ROUTER_TIMEOUT = 1.5
FALLBACK_LOG = Path("/var/log/bouncer-hook.log")


def log_fallback(reason: str, session_id: str = "") -> None:
    try:
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} fallback session={session_id[:8]} reason={reason}\n"
        FALLBACK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FALLBACK_LOG.open("a") as f:
            f.write(line)
    except Exception:
        pass


def call_bouncer(prompt: str, session_id: str) -> dict | None:
    try:
        body = json.dumps({"prompt": prompt}).encode("utf-8")
        req = urllib.request.Request(
            ROUTER_URL, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=ROUTER_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.URLError as e:
        log_fallback(f"unreachable:{type(e).__name__}", session_id)
        return None
    except Exception as e:
        log_fallback(f"error:{type(e).__name__}", session_id)
        return None


def format_rules(routed: dict) -> str:
    lines: list[str] = []
    for r in routed.get("always", []):
        lines.append(f"- **{r['slug']}** [always] — {r['trigger']}")
    for r in routed.get("matched", []):
        via = r.get("match_via") or "match"
        lines.append(f"- **{r['slug']}** [{via}] — {r['trigger']}")
    return "\n".join(lines)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return

    session_id = payload.get("session_id") or ""
    user_prompt = payload.get("prompt") or ""

    routed = call_bouncer(user_prompt, session_id)
    if routed is None:
        # Fail-open with a visible degraded notice so the model knows it has
        # reduced rule context. Don't silently drop.
        block = (
            "[bouncer] DEGRADED: rule daemon unreachable. "
            "No behavioural rules injected this prompt. "
            "Check daemon health + /var/log/bouncer-hook.log."
        )
    else:
        block = (
            f"[bouncer] {len(routed.get('always', []))} always + "
            f"{len(routed.get('matched', []))} matched rules.\n\n"
            + format_rules(routed)
        )

    # Adapt this output envelope to your platform.
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block,
        }
    }
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
