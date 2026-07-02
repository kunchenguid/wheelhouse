#!/usr/bin/env python3
"""
Direct unit tests for `wheelhouse_core.qualify_issue_refs`, the shared
deterministic rewrite that fully qualifies bare GitHub-autolink issue/PR
references (`#N`) in model-generated free text so they point at the TARGET
repo instead of the CARDS repo the text is actually posted in.

NO network, NO live LLM.

Run: python tests/test_qualify_refs.py
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


def test_bare_ref_is_qualified():
    check(
        "bare #N -> owner/repo#N",
        core.qualify_issue_refs("landed in #127 per the comment", "acme", "repo")
        == "landed in acme/repo#127 per the comment",
    )
    check(
        "bare #N at start of string is qualified",
        core.qualify_issue_refs("#127 fixed it", "acme", "repo") == "acme/repo#127 fixed it",
    )
    check(
        "bare #N inside parens is qualified",
        core.qualify_issue_refs("(#127)", "acme", "repo") == "(acme/repo#127)",
    )


def test_multiple_refs_in_one_string():
    check(
        "every bare ref in the string is qualified",
        core.qualify_issue_refs("#127 and #128 both landed", "acme", "repo")
        == "acme/repo#127 and acme/repo#128 both landed",
    )


def test_already_qualified_refs_untouched():
    check(
        "owner/repo#N already qualified stays untouched",
        core.qualify_issue_refs("see owner/other#5 for context", "acme", "repo")
        == "see owner/other#5 for context",
    )
    check(
        "same owner/repo#N (already correct) is not double-qualified",
        core.qualify_issue_refs("see acme/repo#5 for context", "acme", "repo")
        == "see acme/repo#5 for context",
    )


def test_urls_and_markdown_links_untouched():
    check(
        "a full github URL is untouched",
        core.qualify_issue_refs("see https://github.com/o/r/issues/127", "acme", "repo")
        == "see https://github.com/o/r/issues/127",
    )
    check(
        "a URL fragment (word-adjacent #N) is untouched",
        core.qualify_issue_refs("see page#123 for details", "acme", "repo")
        == "see page#123 for details",
    )
    check(
        "a markdown link URL containing #N is untouched",
        core.qualify_issue_refs("[link](url#127)", "acme", "repo") == "[link](url#127)",
    )


def test_non_reference_hash_uses_untouched():
    check(
        "GH-123 style refs (no bare #) are untouched",
        core.qualify_issue_refs("see GH-123 for the old tracker", "acme", "repo")
        == "see GH-123 for the old tracker",
    )
    check(
        "#123abc (digits followed by a word char) is not a ref",
        core.qualify_issue_refs("#123abc is not a ref", "acme", "repo")
        == "#123abc is not a ref",
    )
    check(
        "foo#127 (word char before #) is untouched",
        core.qualify_issue_refs("foo#127 stays", "acme", "repo") == "foo#127 stays",
    )


def test_empty_and_none_safe():
    check("None text -> empty string", core.qualify_issue_refs(None, "acme", "repo") == "")
    check("empty text -> empty string", core.qualify_issue_refs("", "acme", "repo") == "")
    check(
        "missing owner -> text returned unchanged",
        core.qualify_issue_refs("#127", "", "repo") == "#127",
    )
    check(
        "missing repo -> text returned unchanged",
        core.qualify_issue_refs("#127", "acme", "") == "#127",
    )
    check(
        "missing owner and repo -> text returned unchanged",
        core.qualify_issue_refs("#127", "", "") == "#127",
    )


def test_idempotent():
    samples = [
        "landed in #127 per the comment",
        "already-qualified owner/other#5 stays",
        "see https://github.com/o/r/issues/127",
        "[link](url#127)",
        "GH-123 unrelated",
        "#123abc not a ref",
        "#127 and #128 both",
        "",
        None,
        "foo#127 stays",
        "page#123 fragment",
        "(#127)",
    ]
    for sample in samples:
        once = core.qualify_issue_refs(sample, "acme", "repo")
        twice = core.qualify_issue_refs(once, "acme", "repo")
        check("idempotent for %r" % (sample,), once == twice)


def test_target_slug_drives_qualification_never_model_text():
    """The caller's owner/repo (deterministic card state) decides the
    qualification target - text claiming a different repo does not redirect
    it, and text with no refs at all is unaffected."""
    text = "I think this belongs in other-owner/other-repo, see #9."
    check(
        "qualification always uses the caller-supplied slug, not any repo named in the text",
        core.qualify_issue_refs(text, "acme", "repo")
        == "I think this belongs in other-owner/other-repo, see acme/repo#9.",
    )


def main():
    test_bare_ref_is_qualified()
    test_multiple_refs_in_one_string()
    test_already_qualified_refs_untouched()
    test_urls_and_markdown_links_untouched()
    test_non_reference_hash_uses_untouched()
    test_empty_and_none_safe()
    test_idempotent()
    test_target_slug_drives_qualification_never_model_text()
    print()
    if _failures:
        print("%d FAILED: %s" % (len(_failures), ", ".join(_failures)))
        sys.exit(1)
    print("all qualify_issue_refs tests passed")


if __name__ == "__main__":
    main()
