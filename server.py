"""Bouncer — predicate-gated rule injection daemon.

Loads behavioural rules from a markdown directory (set via RULES_DIR env),
builds a SemanticRouter over their trigger_utterances, and serves /route
requests from a UserPromptSubmit hook (or any other caller).

Why a daemon: the HuggingFace encoder (all-MiniLM-L6-v2) takes ~10s to
load. Per-prompt subprocess spawn is prohibitive. Long-running daemon
keeps the model warm; hook hits localhost in <50ms per request.

Frontmatter schema per rule .md file:
    ---
    activation: always | semantic | manual
    trigger: "[trigger] → [action]"           # one-line hot-list summary
    trigger_keywords: ["regex1", ...]          # optional, evaluated first
    trigger_utterances: [str, ...]             # required when activation: semantic
                                               # if no trigger_keywords are set
    score_threshold: 0.32-0.35                 # optional, default 0.35
    ---

Routing pipeline (per /route call):
  1. Always-on rules → unconditionally included.
  2. Keyword pass → evaluate every rule's trigger_keywords regex; collect hits.
  3. Semantic pass → retrieve_multiple_routes over rules NOT already keyword-matched.
  4. Merge keyword + semantic, dedup by slug, cap at K=3.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any

import yaml
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bouncer")

# RULES_DIR is required at runtime — no sensible default that doesn't leak
# the operator's local layout. Set via env or `--rules-dir` (when wrapped).
RULES_DIR = Path(os.environ.get("RULES_DIR", "./rules"))
MODEL_NAME = os.environ.get("MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2")
DEFAULT_THRESHOLD = float(os.environ.get("DEFAULT_THRESHOLD", "0.35"))
TOP_K = int(os.environ.get("TOP_K", "3"))

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, Any]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        log.warning("frontmatter parse failed: %s", e)
        return {}


def _extract_trigger_fallback(text: str) -> str:
    body = FRONTMATTER_RE.sub("", text, count=1)
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:240]
    return "(no trigger)"


def _compile_keywords(patterns: list[str], slug: str) -> list[re.Pattern]:
    compiled: list[re.Pattern] = []
    for p in patterns or []:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error as e:
            log.warning("rule %s: invalid keyword regex %r: %s", slug, p, e)
    return compiled


def load_rules() -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (always_rules, semantic_rules, invalid_rules).
    Each rule is a dict with {slug, path, activation, trigger, threshold,
    keywords (compiled), utterances}.
    invalid_rules: rules with activation: semantic but neither keywords
    nor utterances. Skipped from routing; surfaced in /healthz.
    """
    always: list[dict] = []
    semantic: list[dict] = []
    invalid: list[dict] = []
    if not RULES_DIR.exists():
        log.error("rules dir not found: %s", RULES_DIR)
        return always, semantic, invalid

    for md in sorted(RULES_DIR.rglob("*.md")):
        rel = md.relative_to(RULES_DIR)
        text = md.read_text()
        fm = parse_frontmatter(text)
        # Default activation: rules under _always/ are always; others semantic.
        default_activation = "always" if rel.parts[0] == "_always" else "semantic"
        activation = fm.get("activation", default_activation)
        trigger = fm.get("trigger") or _extract_trigger_fallback(text)
        utterances = fm.get("trigger_utterances", []) or []
        keywords_raw = fm.get("trigger_keywords", []) or []
        keywords = _compile_keywords(keywords_raw, md.stem)
        threshold = float(fm.get("score_threshold", DEFAULT_THRESHOLD))

        rule = {
            "slug": md.stem,
            "path": str(rel),
            "activation": activation,
            "trigger": trigger,
            "threshold": threshold,
            "utterances": utterances,
            "keywords": keywords,
            "keywords_raw": keywords_raw,
        }

        if activation == "always":
            always.append(rule)
        elif activation == "semantic":
            if not utterances and not keywords:
                log.warning(
                    "rule %s: activation=semantic but no trigger_keywords or trigger_utterances; skipping",
                    rule["slug"],
                )
                invalid.append(rule)
            else:
                semantic.append(rule)
        # 'manual' rules are quietly ignored — surface only when explicitly fetched.
    log.info(
        "loaded %d always-rules, %d semantic rules, %d invalid (skipped)",
        len(always), len(semantic), len(invalid),
    )
    return always, semantic, invalid


class _State:
    def __init__(self) -> None:
        self.always: list[dict] = []
        self.semantic: list[dict] = []
        self.invalid: list[dict] = []
        self.router = None
        self.encoder = None
        self.slug_by_route_name: dict[str, dict] = {}
        self.lock = Lock()

    def reload(self) -> None:
        """Load rules + encoder + precompute utterance embeddings.

        We bypass semantic-router's high-level SemanticRouter (it only returns
        single best match via __call__) and use the encoder directly. For each
        rule with utterances, we precompute one mean-pooled rule-level embedding
        from its utterances and store it. Routing then = cosine vs all rule
        embeddings, returning all that exceed per-rule threshold.
        """
        import numpy as np

        with self.lock:
            self.always, self.semantic, self.invalid = load_rules()

            if self.encoder is None:
                from semantic_router.encoders import HuggingFaceEncoder
                log.info("loading encoder %s (cold start)", MODEL_NAME)
                self.encoder = HuggingFaceEncoder(name=MODEL_NAME)
                log.info("encoder loaded")

            # Precompute one embedding per semantic-rule (with utterances).
            # We embed all utterances per rule and take their mean — equivalent
            # to "rule-level prototype" embedding. Single similarity per rule
            # at query time.
            rules_with_utterances = [r for r in self.semantic if r["utterances"]]
            if rules_with_utterances:
                all_utterances: list[str] = []
                offsets: list[tuple[int, int]] = []  # (start, end) per rule
                for r in rules_with_utterances:
                    start = len(all_utterances)
                    all_utterances.extend(r["utterances"])
                    offsets.append((start, len(all_utterances)))
                # Batch embed.
                vecs = np.array(self.encoder(all_utterances), dtype=np.float32)
                # Normalize.
                norms = np.linalg.norm(vecs, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                vecs = vecs / norms
                # Per-rule mean of utterance vectors, then re-normalize.
                self.rule_vecs = []
                for (start, end) in offsets:
                    proto = vecs[start:end].mean(axis=0)
                    n = np.linalg.norm(proto)
                    if n > 0:
                        proto = proto / n
                    self.rule_vecs.append(proto)
                self.rule_vecs = np.stack(self.rule_vecs, axis=0)  # shape (R, D)
                self.rules_with_utterances = rules_with_utterances
            else:
                self.rule_vecs = None
                self.rules_with_utterances = []

            log.info(
                "router rebuilt: %d semantic rules with utterances, "
                "%d keyword-only, %d invalid",
                len(rules_with_utterances),
                len(self.semantic) - len(rules_with_utterances),
                len(self.invalid),
            )


state = _State()


# ---- API ----

class RouteRequest(BaseModel):
    prompt: str
    k: int = TOP_K


class RuleOut(BaseModel):
    slug: str
    path: str
    trigger: str
    activation: str
    match_via: str | None = None  # "keyword" | "semantic" | None for always
    similarity: float | None = None


class RouteResponse(BaseModel):
    always: list[RuleOut]
    matched: list[RuleOut]


class HealthResponse(BaseModel):
    ok: bool
    always: int
    semantic: int
    invalid: int


app = FastAPI(title="rule-router")


@app.on_event("startup")
def _startup():
    state.reload()


@app.get("/healthz", response_model=HealthResponse)
def healthz():
    return HealthResponse(
        ok=True,
        always=len(state.always),
        semantic=len(state.semantic),
        invalid=len(state.invalid),
    )


@app.post("/reload", response_model=HealthResponse)
def reload_rules():
    state.reload()
    return healthz()


@app.post("/route", response_model=RouteResponse)
def route(req: RouteRequest):
    import numpy as np

    always_out = [
        RuleOut(slug=r["slug"], path=r["path"], trigger=r["trigger"], activation="always")
        for r in state.always
    ]

    matched: dict[str, RuleOut] = {}  # slug → RuleOut, for dedup

    prompt_stripped = req.prompt.strip()

    # System-content filter: skip routing for known meta-prompts that aren't
    # genuine user intent (compaction summaries, skill descriptions, slash-cmd
    # boilerplate). These contain keywords that trigger false positives.
    SYSTEM_PREFIXES = (
        "This session is being continued from a previous conversation",
        "Base directory for this skill:",
        "<command-name>",
        "<command-message>",
        "<local-command-",
        "<task-notification>",
        "<task-id>",
        "Caveat: The messages below were generated by the user",
        "Continue from where you left off",
        "<<autonomous-loop",
    )
    is_system_meta = any(prompt_stripped.startswith(p) for p in SYSTEM_PREFIXES)

    if prompt_stripped and not is_system_meta:
        # --- Pass 1: keyword pre-pass over all semantic rules ---
        keyword_hit_slugs: set[str] = set()
        for rule in state.semantic:
            if not rule["keywords"]:
                continue
            if any(kw.search(req.prompt) for kw in rule["keywords"]):
                matched[rule["slug"]] = RuleOut(
                    slug=rule["slug"],
                    path=rule["path"],
                    trigger=rule["trigger"],
                    activation="semantic",
                    match_via="keyword",
                )
                keyword_hit_slugs.add(rule["slug"])

        # --- Pass 2: semantic over rules NOT already keyword-matched ---
        # Direct cosine against precomputed rule embeddings → multi-match.
        # Skip semantic for very short prompts — encoder noise dominates and
        # short tokens like "approve", "yes", "ok" produce spurious matches.
        word_count = len(req.prompt.split())
        # Length-aware threshold bump. Short prompts have noisier embeddings:
        # short common phrases ("9am", "as long as") produce spurious cosine
        # matches near the per-rule floor. Add a per-prompt nudge that decays
        # to 0 as prompt grows past ~12 words.
        if word_count < 6:
            length_bump = 0.05
        elif word_count < 12:
            length_bump = 0.02
        else:
            length_bump = 0.0
        if state.rule_vecs is not None and state.encoder is not None and word_count >= 4:
            try:
                qv = np.array(state.encoder([req.prompt]), dtype=np.float32)[0]
                qn = np.linalg.norm(qv)
                if qn > 0:
                    qv = qv / qn
                sims = state.rule_vecs @ qv  # shape (R,)
                # Score every semantic-rule that has utterances and isn't keyword-matched.
                scored = []
                for i, rule in enumerate(state.rules_with_utterances):
                    if rule["slug"] in keyword_hit_slugs:
                        continue
                    sim = float(sims[i])
                    if sim >= (rule["threshold"] + length_bump):
                        scored.append((sim, rule))
                # Sort descending by similarity.
                scored.sort(key=lambda t: t[0], reverse=True)
                for sim, rule in scored:
                    if rule["slug"] in matched:
                        continue
                    matched[rule["slug"]] = RuleOut(
                        slug=rule["slug"],
                        path=rule["path"],
                        trigger=rule["trigger"],
                        activation="semantic",
                        match_via="semantic",
                        similarity=sim,
                    )
            except Exception as e:
                log.warning("semantic scoring failed: %s", e)

    # --- Cap at K (request override allowed but bounded by TOP_K default) ---
    cap = max(1, min(req.k, TOP_K))
    matched_list = list(matched.values())[:cap]

    return RouteResponse(always=always_out, matched=matched_list)
