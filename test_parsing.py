"""Unit tests for the frontmatter/threshold parsing defects fixed on
branch harden-parsing (A-9, A-6, A-7).

Run with:
    pytest test_parsing.py -v
"""
import logging

import pytest

import server


# ---- A-9: CRLF frontmatter ----

LF_RULE = (
    "---\n"
    "activation: semantic\n"
    "trigger: \"do the thing\"\n"
    "trigger_keywords:\n"
    "  - '\\bthing\\b'\n"
    "score_threshold: 0.4\n"
    "---\n"
    "\n"
    "Body text.\n"
)

CRLF_RULE = LF_RULE.replace("\n", "\r\n")


def test_crlf_frontmatter_parses_identically_to_lf():
    lf_fm = server.parse_frontmatter(LF_RULE, source="lf-rule.md")
    crlf_fm = server.parse_frontmatter(CRLF_RULE, source="crlf-rule.md")

    assert crlf_fm == lf_fm
    assert crlf_fm["activation"] == "semantic"
    assert crlf_fm["trigger"] == "do the thing"
    assert crlf_fm["score_threshold"] == 0.4


def test_well_formed_lf_rule_parsing_is_unchanged():
    fm = server.parse_frontmatter(LF_RULE, source="lf-rule.md")
    assert fm == {
        "activation": "semantic",
        "trigger": "do the thing",
        "trigger_keywords": ["\\bthing\\b"],
        "score_threshold": 0.4,
    }


# ---- A-6: non-dict frontmatter body ----

LIST_BODY_RULE = (
    "---\n"
    "- this\n"
    "- is\n"
    "- a list, not a mapping\n"
    "---\n"
    "Body text.\n"
)


def test_list_frontmatter_yields_empty_dict_and_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="bouncer"):
        fm = server.parse_frontmatter(LIST_BODY_RULE, source="listy-rule.md")

    assert fm == {}
    assert any(
        "listy-rule.md" in rec.message and "not a mapping" in rec.message
        for rec in caplog.records
    )


def test_list_frontmatter_does_not_raise():
    # Regression guard: previously `parsed.get(...)` on a list would raise
    # AttributeError further up the call chain in load_rules().
    fm = server.parse_frontmatter(LIST_BODY_RULE, source="listy-rule.md")
    fm.get("activation", "semantic")  # must not raise


# ---- A-7: non-numeric score_threshold ----

def _write_rule(tmp_path, rel_path, content):
    p = tmp_path / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


def test_range_score_threshold_falls_back_to_default(tmp_path, monkeypatch, caplog):
    rule_text = (
        "---\n"
        "activation: semantic\n"
        "trigger: \"range threshold\"\n"
        "trigger_keywords:\n"
        "  - '\\brange\\b'\n"
        "score_threshold: 0.32-0.35\n"
        "---\n"
        "Body.\n"
    )
    _write_rule(tmp_path, "bad-threshold.md", rule_text)

    monkeypatch.setattr(server, "RULES_DIR", tmp_path)
    with caplog.at_level(logging.WARNING, logger="bouncer"):
        always, semantic, invalid = server.load_rules()

    assert not invalid
    assert len(semantic) == 1
    assert semantic[0]["threshold"] == server.DEFAULT_THRESHOLD
    assert any(
        "bad-threshold" in rec.message and "score_threshold" in rec.message
        for rec in caplog.records
    )


def test_numeric_score_threshold_still_respected(tmp_path, monkeypatch):
    rule_text = (
        "---\n"
        "activation: semantic\n"
        "trigger: \"numeric threshold\"\n"
        "trigger_keywords:\n"
        "  - '\\bnumeric\\b'\n"
        "score_threshold: 0.5\n"
        "---\n"
        "Body.\n"
    )
    _write_rule(tmp_path, "good-threshold.md", rule_text)

    monkeypatch.setattr(server, "RULES_DIR", tmp_path)
    always, semantic, invalid = server.load_rules()

    assert not invalid
    assert len(semantic) == 1
    assert semantic[0]["threshold"] == 0.5


def test_crlf_rule_file_loads_via_load_rules(tmp_path, monkeypatch):
    """End-to-end: a CRLF-saved rule file loads its frontmatter through
    load_rules(), not just through parse_frontmatter() directly."""
    _write_rule(tmp_path, "crlf-on-disk.md", CRLF_RULE)

    monkeypatch.setattr(server, "RULES_DIR", tmp_path)
    always, semantic, invalid = server.load_rules()

    assert not invalid
    assert len(semantic) == 1
    assert semantic[0]["trigger"] == "do the thing"
    assert semantic[0]["threshold"] == 0.4
