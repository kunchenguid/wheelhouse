"""One authoritative per-action size-budget table for agent contracts.

Every byte bound that shapes a model interaction is owned here so the bounds
can be proven coherent against each other instead of drifting apart in copies
(the R1/F1/F4/F5 size-contract audit class):

- Action output schemas (``agent_runtime/schemas/actions/*.json``) carry the
  CONTENT contract: per-field character maxima sized by what each field may
  legitimately hold. ``tests/test_size_budget.py`` asserts the schema files
  match the character maxima recorded here.
- ``max_final_bytes`` is the TRANSPORT contract: the canonical-encoding byte
  cap the bridge enforces on a delivered final value. Each action's cap MUST
  dominate the worst-case canonical encoding of any schema-valid value
  (``schema_worst_case_canonical_bytes``) plus ``FINAL_HEADROOM_BYTES``, so a
  schema-valid result can never be rejected by its own byte bound.
- ``repair_candidate_max_bytes`` bounds how much of a FAILED candidate is
  retained for the one correction/repair turn. Retention is truncation with an
  explicit marker, never a silent drop, so oversized delivered candidates stay
  correction-eligible in bounded form.
- Prompt budgets: an env-carried prompt (the pinned action lane re-packs
  ``prompt:`` into a single process string) must stay under the kernel's
  per-string ``execve`` limit ``MAX_ARG_STRLEN``; a compiled prompt artifact
  (the direct stdin lane) must stay under ``COMPILED_PROMPT_MAX_BYTES``. The
  NL repair prompt must fit BOTH lanes so the reviewed action-lane rollback
  profile keeps working: ``NL repair candidate cap + prompt overhead <=
  ENV_PROMPT_MAX_BYTES``.
- Inline-context budgets: the NL prompt inlines the card's trusted
  conversation history, so that history is bounded by turn count, per-turn
  bytes, and total bytes with explicit elision markers (the F1 E2BIG class).

Character-vs-byte accounting: JSON Schema ``maxLength`` counts characters,
while every cap here counts UTF-8 bytes of the canonical encoding
(``contract.canonical_json_bytes``, ``ensure_ascii=False``). One character
costs at most ``JSON_STRING_CHAR_MAX_BYTES`` = 6 encoded bytes (a control
character escapes to ``\\u00XX``; astral code points cost 4). The worst-case
walker uses that factor for unconstrained strings and exact ASCII character
classes for pattern-bound strings.

The deliberate router/schema contract changes recorded here:

- ``nl-decision-v1`` ``answer`` is capped at ``NL_ANSWER_MAX_CHARS`` and
  ``free_text`` at ``NL_FREE_TEXT_MAX_CHARS`` so the worst-case schema-valid
  candidate (104,985 canonical bytes) always fits the repair prompt COMPLETE
  in both repair lanes. Longer replies were never postable ambitions: both
  fields are conversational text, and the caps stay far above observed use.
- ``deep-review-text-v1`` ``text`` is capped at ``GITHUB_COMMENT_MAX_CHARS``:
  a longer verdict could never post to the card (GitHub's comment bound), so
  schema-validity now matches consumer reality.

Consumers: ``task_builder.ACTION_LIMITS`` (task construction),
``claude_bridge`` (bridge validation, canonical output bounds, bounded
candidate retention), ``apply_decision`` (NL history budget, NL repair
candidate cap), ``render_card`` (triage repair candidate cap), ``worker``
(compiled prompt admission), ``consumer`` (result artifact reads), and the
v1alpha1 contract schemas (``maxFinalBytes`` / ``delivered.bytes`` /
``final.bytes`` ceilings). ``tests/test_size_budget.py`` holds the property
tests that keep every relationship above true.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# Platform and transport constants
# --------------------------------------------------------------------------- #

# Kernel per-string execve limit: one argv/environment string may not exceed
# this many bytes (including the terminating NUL). Any prompt that crosses a
# process boundary as a single env value is bound by it (cards #517/#555).
MAX_ARG_STRLEN = 131072

# Budget for env-carried prompts, leaving explicit headroom under the kernel
# limit for the action harness's own env packing.
ENV_PROMPT_HEADROOM_BYTES = 4096
ENV_PROMPT_MAX_BYTES = MAX_ARG_STRLEN - ENV_PROMPT_HEADROOM_BYTES

# Compiled prompt artifact cap for the content-addressed handoff (the direct
# claude-cli lane delivers the prompt over stdin, so only this bound applies).
COMPILED_PROMPT_MAX_BYTES = 262144

# Raw execution transcript / event stream retention bound (also the maximum
# size of one transcript event the bridge will read).
TRANSCRIPT_MAX_BYTES = 8388608

# One AgentResult artifact (result.json) read bound. Must dominate the largest
# possible envelope: a result can carry the same value under both `delivered`
# and `final` plus bounded metadata.
RESULT_ARTIFACT_MAX_BYTES = 8388608

# Contract-schema ceiling for any action's max_final_bytes (and therefore for
# `delivered.bytes` / `final.bytes` in agent-result). A sanity bound, not a
# tuning knob: per-action caps live in SIZE_BUDGETS.
MAX_FINAL_BYTES_CEILING = 2097152

# Every action's byte cap must exceed its schema worst case by at least this
# explicit headroom (and caps are kept at 4096-byte multiples for review).
FINAL_HEADROOM_BYTES = 4096

# Worst-case canonical-encoding cost of one schema character: a control
# character serializes as a six-byte \u00XX escape (astral code points cost
# four UTF-8 bytes; quote/backslash escapes cost two).
JSON_STRING_CHAR_MAX_BYTES = 6

# GitHub issue/PR comment body bound (characters).
GITHUB_COMMENT_MAX_CHARS = 65536
COMMENT_TRUNCATION_TEMPLATE = "\n\n[truncated to fit GitHub comment limit]"

# --------------------------------------------------------------------------- #
# NL router/schema character contract (nl-decision-v1)
# --------------------------------------------------------------------------- #

NL_ANSWER_MAX_CHARS = 11264
NL_FREE_TEXT_MAX_CHARS = 6144
NL_ACTION_MAX_CHARS = 80

# Deep-review verdict character contract (deep-review-text-v1). Final consumer
# admission still accounts for qualification, the claim marker, and separators.
DEEP_REVIEW_TEXT_MAX_CHARS = GITHUB_COMMENT_MAX_CHARS

# --------------------------------------------------------------------------- #
# NL trusted-history inline budget (assemble_history)
# --------------------------------------------------------------------------- #

# Only the most recent turns are inlined, each truncated to a per-turn byte
# bound, and the rendered history never exceeds the total bound. Elision is
# always explicit so the model knows context was dropped. The trusted-author
# filter is unrelated to these bounds and unchanged.
NL_HISTORY_MAX_TURNS = 20
NL_HISTORY_TURN_MAX_BYTES = 4096
NL_HISTORY_MAX_TOTAL_BYTES = 32768
NL_HISTORY_ELISION_TEMPLATE = "[earlier conversation elided: %d turn%s, %d bytes]"
NL_HISTORY_TURN_TRUNCATION_TEMPLATE = "[truncated: retained %d of %d bytes]"

# Marker appended when a failed delivered candidate is retained truncated.
CANDIDATE_TRUNCATION_TEMPLATE = "\n[candidate truncated: retained %d of %d bytes]"
# Upper bound on the rendered marker text itself.
CANDIDATE_TRUNCATION_MARKER_MAX_BYTES = 96


# --------------------------------------------------------------------------- #
# Per-action size budgets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ActionSizeBudget:
    """The authoritative byte budget for one agent-runtime action."""

    # Bound action schema file under agent_runtime/schemas/actions/.
    schema_file: str
    # Canonical-encoding byte cap for the delivered final value. Dominates the
    # schema worst case plus FINAL_HEADROOM_BYTES (proven by the property test).
    max_final_bytes: int
    # Raw-byte retention bound for a FAILED delivered candidate (correction /
    # repair context). Larger candidates are truncated with an explicit marker.
    repair_candidate_max_bytes: int


# Repair/correction candidate bounds. The triage correction turn re-runs the
# ORIGINAL task with full tools and evidence, so its candidate is advisory
# context and a compact embed suffices. The NL repair turn is no-tool: the
# candidate is ALL it sees, so its bound must cover the worst-case schema-valid
# candidate (104,985 canonical bytes) and still fit the env-carried rollback
# lane: TRIAGE_REPAIR_CANDIDATE_MAX_BYTES is embedded into the original prompt
# (~<= 16 KiB by test_triage_prompt_size) and NL_REPAIR_CANDIDATE_MAX_BYTES
# after action JSON packing stays under ENV_PROMPT_MAX_BYTES.
TRIAGE_REPAIR_CANDIDATE_MAX_BYTES = 24000
NL_REPAIR_CANDIDATE_MAX_BYTES = 106496

# max_final_bytes values are round 4096 multiples chosen as the smallest
# 65536-multiple at least schema-worst-case + FINAL_HEADROOM_BYTES:
#   triage-pr-v1       worst 1,626,555 -> 1,638,400
#   triage-issue-v1    worst   144,113 ->   196,608
#   deep-review-text   worst   393,227 ->   458,752
#   nl-decision-v1     worst   104,985 ->   131,072
SIZE_BUDGETS: dict[str, ActionSizeBudget] = {
    "triage.pr.local": ActionSizeBudget(
        "triage-pr-v1.schema.json", 1638400, TRIAGE_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "triage.pr.search": ActionSizeBudget(
        "triage-pr-v1.schema.json", 1638400, TRIAGE_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "triage.issue.local": ActionSizeBudget(
        "triage-issue-v1.schema.json", 196608, TRIAGE_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "triage.issue.search": ActionSizeBudget(
        "triage-issue-v1.schema.json", 196608, TRIAGE_REPAIR_CANDIDATE_MAX_BYTES
    ),
    # The disabled direct-lane rollback surface repairs a pr or issue triage
    # candidate; its output contract is the larger of the two triage schemas.
    "triage.schema-repair": ActionSizeBudget(
        "triage-pr-v1.schema.json", 1638400, TRIAGE_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "deep-review.local": ActionSizeBudget(
        "deep-review-text-v1.schema.json", 458752, TRIAGE_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "deep-review.search": ActionSizeBudget(
        "deep-review-text-v1.schema.json", 458752, TRIAGE_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "nl-decision.local": ActionSizeBudget(
        "nl-decision-v1.schema.json", 131072, NL_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "nl-decision.search": ActionSizeBudget(
        "nl-decision-v1.schema.json", 131072, NL_REPAIR_CANDIDATE_MAX_BYTES
    ),
    "nl-decision.schema-repair": ActionSizeBudget(
        "nl-decision-v1.schema.json", 131072, NL_REPAIR_CANDIDATE_MAX_BYTES
    ),
}


def max_final_bytes(action: str) -> int:
    return SIZE_BUDGETS[action].max_final_bytes


def repair_candidate_max_bytes(action: str) -> int:
    return SIZE_BUDGETS[action].repair_candidate_max_bytes


def claude_action_packed_prompt_bytes(prompt: str) -> int:
    return len(json.dumps(prompt, ensure_ascii=False).encode("utf-8"))


def delivered_retention_canonical_max_bytes(action: str) -> int:
    """Canonical-encoding bound for a retained (possibly truncated) candidate.

    A retained candidate is either a value whose canonical encoding fits the
    action's ``max_final_bytes``, or a raw-text truncation bounded by
    ``repair_candidate_max_bytes`` plus the marker; encoding that text as one
    JSON string costs at most six bytes per retained byte plus quotes.
    """

    budget = SIZE_BUDGETS[action]
    truncated_worst = 2 + JSON_STRING_CHAR_MAX_BYTES * (
        budget.repair_candidate_max_bytes + CANDIDATE_TRUNCATION_MARKER_MAX_BYTES
    )
    return max(budget.max_final_bytes, truncated_worst)


def bounded_candidate_text(text: str, max_raw_bytes: int) -> str:
    """Truncate candidate text to a raw-byte bound with an explicit marker.

    Never a silent drop: the marker records exactly how much was retained so
    the correction/repair turn (and any human reader) can see the truncation.
    """

    raw = text.encode("utf-8")
    if len(raw) <= max_raw_bytes:
        return text
    retained = raw[:max_raw_bytes].decode("utf-8", "ignore")
    return retained + (
        CANDIDATE_TRUNCATION_TEMPLATE % (len(retained.encode("utf-8")), len(raw))
    )


def bounded_github_comment(text: str, suffix: str) -> str:
    """Return a final GitHub comment body within the consumer character cap."""

    separator = "\n\n" if suffix else ""
    tail = separator + suffix
    if len(tail) > GITHUB_COMMENT_MAX_CHARS:
        raise SizeBudgetError("GitHub comment suffix exceeds the consumer bound")
    if len(text) + len(tail) <= GITHUB_COMMENT_MAX_CHARS:
        return text + tail
    marker = COMMENT_TRUNCATION_TEMPLATE
    available = GITHUB_COMMENT_MAX_CHARS - len(tail) - len(marker)
    if available < 0:
        raise SizeBudgetError("GitHub comment metadata leaves no verdict capacity")
    return text[:available] + marker + tail


# --------------------------------------------------------------------------- #
# Schema worst-case walker
# --------------------------------------------------------------------------- #

# Exact maximum character counts for the ASCII-class patterns the action
# schemas use. A pattern listed here is proven ASCII (one byte per character,
# no escaping) with the recorded maximum match length; any pattern NOT listed
# makes the walker fail loudly instead of guessing.
_PATTERN_MAX_CHARS = {
    r"^sha256:[0-9a-f]{64}$": 71,
    r"^[0-9a-f]{64}$": 64,
    r"^[0-9a-f]{7,64}$": 64,
    r"^[0-9a-f]{40,64}$": 64,
    r"^[a-z0-9][a-z0-9._-]{0,63}$": 64,
    r"^(target\.txt|target-src/[A-Za-z0-9._/-]{1,900})$": 911,
    r"^(target\.txt|vision\.md|target-src/[A-Za-z0-9._/-]{1,900})$": 911,
}

_BOOLEAN_MAX_BYTES = 5


class SizeBudgetError(ValueError):
    """A schema shape the worst-case walker cannot soundly bound."""


def _canonical_len(value: Any) -> int:
    from .contract import canonical_json_bytes

    return len(canonical_json_bytes(value))


def schema_worst_case_canonical_bytes(
    schema: dict[str, Any], defs: dict[str, Any] | None = None
) -> int:
    """A sound upper bound on the canonical encoding of any schema-valid value.

    Walks the strict schema subset the action contracts use. Every string must
    be bounded by ``maxLength`` (costed at ``JSON_STRING_CHAR_MAX_BYTES`` per
    character) or by a known ASCII pattern; every array by ``maxItems``.
    Unknown or unbounded shapes raise ``SizeBudgetError`` rather than
    returning an unsound bound.
    """

    if defs is None:
        defs = schema.get("$defs", {}) or {}
    if "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        if name not in defs:
            raise SizeBudgetError("unresolved $ref %r" % schema["$ref"])
        return schema_worst_case_canonical_bytes(defs[name], defs)
    if "oneOf" in schema:
        return max(
            schema_worst_case_canonical_bytes(branch, defs)
            for branch in schema["oneOf"]
        )
    if "const" in schema:
        return _canonical_len(schema["const"])
    if "enum" in schema:
        return max(_canonical_len(option) for option in schema["enum"])
    node_type = schema.get("type")
    if node_type == "string":
        pattern = schema.get("pattern")
        if pattern is not None:
            chars = _PATTERN_MAX_CHARS.get(pattern)
            if chars is None:
                raise SizeBudgetError("pattern %r has no recorded bound" % pattern)
            return 2 + chars
        if "maxLength" not in schema:
            raise SizeBudgetError("string schema without maxLength or pattern")
        return 2 + JSON_STRING_CHAR_MAX_BYTES * schema["maxLength"]
    if node_type == "integer":
        if "minimum" not in schema or "maximum" not in schema:
            raise SizeBudgetError("integer schema without finite minimum and maximum")
        minimum = schema["minimum"]
        maximum = schema["maximum"]
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or minimum > maximum
        ):
            raise SizeBudgetError("integer schema has invalid finite bounds")
        return max(_canonical_len(minimum), _canonical_len(maximum))
    if node_type == "boolean":
        return _BOOLEAN_MAX_BYTES
    if node_type == "array":
        if "maxItems" not in schema:
            raise SizeBudgetError("array schema without maxItems")
        count = schema["maxItems"]
        item = schema_worst_case_canonical_bytes(schema["items"], defs)
        return 2 + count * item + max(0, count - 1)
    if node_type == "object":
        properties = schema.get("properties", {}) or {}
        if schema.get("additionalProperties", False) is not False:
            raise SizeBudgetError("object schema without additionalProperties: false")
        total = 2 + max(0, len(properties) - 1)
        for key, sub in properties.items():
            total += _canonical_len(key) + 1 + schema_worst_case_canonical_bytes(sub, defs)
        return total
    raise SizeBudgetError("unsupported schema node %r" % (schema,))
