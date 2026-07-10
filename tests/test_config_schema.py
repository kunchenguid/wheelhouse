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


def test_every_repo_entry_is_well_formed():
    repos = core.load_config()["repos"]
    for name, entry in repos.items():
        # name is the mapping key AND echoed on the entry; both non-empty str.
        check("repo key is a non-empty str: %r" % (name,),
              isinstance(name, str) and bool(name.strip()))
        check("%s: name field matches its key" % name, entry.get("name") == name)
        # compliance_check is either absent/null (no gate) or a non-empty string
        # naming an exact required check. Never an empty string or other type.
        comp = entry.get("compliance_check")
        check("%s: compliance_check is None or a non-empty str" % name,
              comp is None or (isinstance(comp, str) and bool(comp.strip())))
        # test_check_patterns, when present, is a list of non-empty strings.
        pats = entry.get("test_check_patterns")
        ok_pats = (
            pats is None
            or (isinstance(pats, list)
                and all(isinstance(p, str) and bool(p) for p in pats))
        )
        check("%s: test_check_patterns is a list of non-empty strs (or unset)" % name,
              ok_pats)
        # merge_method, when present, is one of the methods the executor accepts.
        mm = entry.get("merge_method")
        check("%s: merge_method is unset or squash|merge|rebase" % name,
              mm is None or mm in ("squash", "merge", "rebase"))


def test_repo_names_are_unique():
    # load_config keys by name, so a duplicate name would silently drop an entry.
    # Re-read the raw list to prove the file itself has no dup that the mapping
    # would mask.
    import yaml

    with open(core.config_path()) as f:
        raw = yaml.safe_load(f) or {}
    names = [r["name"] for r in (raw.get("repos") or []) if isinstance(r, dict) and r.get("name")]
    check("no duplicate repo names in the file", len(names) == len(set(names)))
    check("mapping keeps every listed repo", len(names) == len(core.load_config()["repos"]))


def main():
    test_real_config_loads()
    test_every_repo_entry_is_well_formed()
    test_repo_names_are_unique()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all config-schema tests passed")


if __name__ == "__main__":
    main()
