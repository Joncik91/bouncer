#!/usr/bin/env python3
"""Replay a session's user prompts through the bouncer daemon.

Reads a Claude Code transcript (.jsonl), extracts each user prompt, sends it
to /route, prints which rules fire. Used to spot-check whether the routing
pattern matches expectations.

Usage:
    test-replay.py <session.jsonl> [--limit N]
"""
import argparse
import json
import re
import sys
import urllib.request

ROUTER_URL = "http://127.0.0.1:8765/route"


def extract_user_prompts(jsonl_path: str):
    """Yield (turn_idx, prompt_text) for user-typed messages.

    Skips system envelopes, tool results, sidechain subagents, slash-command bodies.
    """
    with open(jsonl_path) as f:
        for i, line in enumerate(f):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "user":
                continue
            if rec.get("isSidechain"):
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            if not content:
                continue
            # Content may be string or list-of-blocks.
            if isinstance(content, list):
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
                content = "\n".join(texts).strip()
            if not isinstance(content, str) or not content.strip():
                continue
            # Skip tool_result envelopes & system reminders.
            if content.startswith("<system-reminder"):
                continue
            if content.startswith("[Request interrupted"):
                continue
            yield i, content


def call_route(prompt: str):
    body = json.dumps({"prompt": prompt}).encode("utf-8")
    req = urllib.request.Request(
        ROUTER_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl", help="path to session .jsonl")
    ap.add_argument("--limit", type=int, default=0, help="max prompts to test (0=all)")
    args = ap.parse_args()

    n_tested = 0
    n_matched = 0
    match_counts: dict[str, int] = {}

    for turn_idx, prompt in extract_user_prompts(args.jsonl):
        # Trim long prompts for display
        display = prompt[:120].replace("\n", " ")
        try:
            r = call_route(prompt)
        except Exception as e:
            print(f"[{turn_idx:4d}] ERROR: {e}")
            continue
        matched = r.get("matched", [])
        n_tested += 1
        if matched:
            n_matched += 1
        for m in matched:
            match_counts[m["slug"]] = match_counts.get(m["slug"], 0) + 1
        match_str = ", ".join(f"{m['slug']}({m['match_via']})" for m in matched) or "—"
        print(f"[{turn_idx:4d}] {match_str:60s} | {display!r}")
        if args.limit and n_tested >= args.limit:
            break

    print()
    print(f"Tested: {n_tested} prompts")
    print(f"Matched at least one rule: {n_matched} ({100*n_matched/max(1,n_tested):.0f}%)")
    print("Per-rule fire counts:")
    for slug in sorted(match_counts, key=lambda s: -match_counts[s]):
        print(f"  {match_counts[slug]:3d}× {slug}")


if __name__ == "__main__":
    main()
