#!/usr/bin/env python3
"""
Structural load test for the checked-in `wheelhouse.config.yml`.

This is the sanity check that the scan config loader accepts the real fleet
file: it exercises `wheelhouse_core.load_config()` against the committed config
(not a mock) and asserts every `repos:` entry is well-formed. It is deliberately
NON-BRITTLE - it pins no specific repo names or fleet size, only the invariants
that must hold for ANY valid entry - so it keeps guarding the file as the fleet
grows or shrinks without needing an edit each time.

NO network, NO live LLM.

Run: python tests/test_config_schema.py
"""

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import wheelhouse_core as core  # noqa: E402

_failures = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_real_config_loads():
    cfg = core.load_config()
    check("load_config returns a dict", isinstance(cfg, dict))
    check("config exposes a repos mapping", isinstance(cfg.get("repos"), dict))
    check("fleet is non-empty", len(cfg["repos"]) > 0)


def raw_repo_entries():
    with open(core.config_path()) as f:
        raw = yaml.safe_load(f) or {}
    entries = raw.get("repos")
    check("raw repos is a list", isinstance(entries, list))
    return entries if isinstance(entries, list) else []


def test_every_repo_entry_is_well_formed():
    repos = core.load_config()["repos"]
    for index, entry in enumerate(raw_repo_entries()):
        label = "repos[%d]" % index
        check("%s: entry is a mapping" % label, isinstance(entry, dict))
        if not isinstance(entry, dict):
            continue

        name = entry.get("name")
        valid_name = isinstance(name, str) and bool(name) and name == name.strip()
        check("%s: name is a trimmed non-empty str" % label, valid_name)
        if valid_name:
            check("%s: loader preserves the entry" % label, repos.get(name) == entry)
        # compliance_check is either absent/null (no gate) or a non-empty string
        # naming an exact required check. Never an empty string or other type.
        comp = entry.get("compliance_check")
        check(
            "%s: compliance_check is None or a trimmed non-empty str" % label,
            comp is None
            or (isinstance(comp, str) and bool(comp) and comp == comp.strip()),
        )
        # test_check_patterns, when present, is a list of non-empty strings.
        pats = entry.get("test_check_patterns")
        ok_pats = pats is None or (
            isinstance(pats, list)
            and all(isinstance(p, str) and bool(p) and p == p.strip() for p in pats)
        )
        check(
            "%s: test_check_patterns is a list of trimmed non-empty strs (or unset)"
            % label,
            ok_pats,
        )
        # merge_method, when present, is one of the methods the executor accepts.
        mm = entry.get("merge_method")
        check(
            "%s: merge_method is unset or squash|merge|rebase" % label,
            mm is None or mm in ("squash", "merge", "rebase"),
        )


def test_global_auto_merge_is_enabled():
    # This fork commits the fleet-wide `auto_merge: true` switch, so the loaded
    # config must expose it as a REAL boolean True (not the string "true", which
    # `load_config` deliberately treats as false). A committed default-branch
    # VISION.md is the practical per-repo opt-in on top of this switch.
    cfg = core.load_config()
    check(
        "committed config global auto_merge is real boolean True",
        cfg.get("auto_merge") is True,
    )
    # The SHIPPED CODE DEFAULT is unchanged: an absent global key is still OFF, so
    # a fresh fork-and-own inherits auto-merge disabled - only this fork's
    # committed VALUE flipped, never the `_auto_merge_enabled` code default.
    check(
        "code default unchanged: absent auto_merge key -> disabled",
        core._auto_merge_enabled({}, False) is False,
    )
    # A repo relying purely on the fleet-wide switch is opted in, while a per-repo
    # `auto_merge: false` remains the one-repo opt-out against the true global.
    check(
        "global true opts a no-override repo in",
        core._auto_merge_enabled({}, cfg["auto_merge"]) is True,
    )
    check(
        "per-repo auto_merge:false still overrides the true global",
        core._auto_merge_enabled({"auto_merge": False}, cfg["auto_merge"]) is False,
    )


def test_repo_names_are_unique():
    # load_config keys by name, so a duplicate name would silently drop an entry.
    # Re-read the raw list to prove the file itself has no dup that the mapping
    # would mask.
    entries = raw_repo_entries()
    names = [
        entry["name"]
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"].strip()
    ]
    folded_names = [name.casefold() for name in names]
    check(
        "no case-insensitive duplicate repo names in the file",
        len(folded_names) == len(set(folded_names)),
    )
    check(
        "mapping keeps every raw repo entry",
        len(entries) == len(core.load_config()["repos"]),
    )


def main():
    test_real_config_loads()
    test_every_repo_entry_is_well_formed()
    test_global_auto_merge_is_enabled()
    test_repo_names_are_unique()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all config-schema tests passed")


if __name__ == "__main__":
    main()
