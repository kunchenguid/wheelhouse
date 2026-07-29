#!/usr/bin/env python3
"""One coherent current triage state per card (advisory telemetry consistency).

Offline only. Reproduces the production residual shape from the nine-card
advisory recovery report:

- primary model validation failed (`output.schema_invalid`)
- `triage_consumption=advisory`
- AND a current admitted assessment with a live Accept control

Before the fix, owner-facing `### Triage` still warned that the result was only
advisory while `### Recommended action` said the assessment was current and
Acceptable. Production authority predicates already treated those cards as
actionable; this suite locks the presentation to that truth without
manufacturing authority for true stuck-advisory cards.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import card_projection  # noqa: E402
import render_card as rc  # noqa: E402
import wheelhouse_core as core  # noqa: E402
import test_option_b_architecture as ob  # noqa: E402

FAILURES = []

HEAD = "8e54125f81f0bf29924e82c5d617a4ee63f03774"
BASE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def check(name, condition):
    print(("ok   " if condition else "FAIL ") + name)
    if not condition:
        FAILURES.append(name)


def _admitted_payload(obs, context, action="merge", reason="Bounded restoration."):
    return {
        "summary": "Restores Bash 3.2 parsing of one ship-brief heredoc.",
        "product_implications": "Routine maintainer merge of a narrow fix.",
        "recommended_action": action,
        "recommended_reason": reason,
        "evidence": "target.txt: \"restore Bash 3.2 parsing\"",
        "recommendation_basis": {
            "kind": "other",
            "observation_id": obs["observation_id"],
            "context_id": context["context_id"],
        },
    }


def _invalid_payload(obs, context):
    payload = _admitted_payload(obs, context)
    payload["recommendation_basis"] = {
        "kind": "configured-tests",
        "observation_id": obs["observation_id"],
        "context_id": context["context_id"],
        "check_names": ["Ubuntu"],
    }
    payload["automerge"] = {
        "behavior_class": "A",
        "changes_existing_or_default_behavior": False,
    }
    return payload


def _world(head=HEAD):
    obs = ob.observation(number=1074, head=head, base=BASE)
    context = ob.context_for(obs, rows=[])
    item = ob.item_for(obs, context)
    item["title"] = "fix(bin): restore Bash 3.2 parsing of ship brief scaffolding"
    item["author"] = "willronchetti"
    base_body = rc.render(item, owner="kunchenguid")["body"]
    return obs, context, item, base_body


def authority_present_body():
    """Failed primary + advisory consumption + current admitted assessment.

    Mirrors the production residual (card #1735 class): authority predicates
    already grant Accept, while historical primary/consumption telemetry remains
    in non-material state.
    """
    obs, context, item, base_body = _world()
    body = rc.body_with_triage_result(
        base_body,
        HEAD,
        triage=_admitted_payload(
            obs,
            context,
            "merge",
            "Confirmed class-B restoration with green configured checks.",
        ),
        owner="kunchenguid",
        base_sha=item["base_sha"],
        primary_error_code="output.schema_invalid",
        # Default authority_allowed=True admits the assessment; consumption
        # defaults to advisory when primary_error_code is set - the residual
        # telemetry shape the presentation must not treat as current outcome.
    )
    return obs, context, item, body


def authority_absent_body():
    """Same historical telemetry with no current authority (card #1759 class)."""
    obs, context, item, base_body = _world(head="cf7f065de35b9e2931ec883ff650008b6ddfd39e")
    body = rc.body_with_triage_result(
        base_body,
        "cf7f065de35b9e2931ec883ff650008b6ddfd39e",
        triage=_invalid_payload(obs, context),
        owner="kunchenguid",
        base_sha=item["base_sha"],
        primary_error_code="output.schema_invalid",
        authority_allowed=False,
        consumption="advisory",
    )
    return obs, context, item, body


def test_authority_present_renders_one_coherent_actionable_state():
    _obs, _context, _item, body = authority_present_body()
    state = core.parse_state_block(body)

    check(
        "authority-present: production predicates grant current authority + Accept",
        rc.assessment_current_admitted(state) is True
        and rc.accept_recommendation_available(state) is True
        and rc.current_triage_authority_present(state) is True,
    )
    check(
        "authority-present: historical primary/consumption telemetry stays inspectable",
        state.get(rc.TRIAGE_PRIMARY_STATUS_FIELD) == "failed"
        and state.get(rc.TRIAGE_PRIMARY_ERROR_FIELD) == "output.schema_invalid"
        and state.get(rc.TRIAGE_CONSUMPTION_FIELD) == "advisory",
    )
    check(
        "authority-present: owner-facing triage does not present advisory failure",
        "consumed for advisory triage" not in body
        and "not a primary validation success" not in body
        and not rc.contradictory_advisory_telemetry(body, state),
    )
    check(
        "authority-present: analysis and Accept language are current",
        "- **Summary:** Restores Bash 3.2 parsing of one ship-brief heredoc." in body
        and "### Recommended action" in body
        and "- **Agent recommendation:** `merge`" in body
        and "Tick **Accept recommendation**" in body
        and "<!-- opt:accept-recommendation -->" in body,
    )
    check(
        "authority-present: admission warning is absent under current authority",
        "The advisory assessment was not admitted" not in body,
    )


def test_authority_absent_stays_explicitly_unavailable():
    _obs, _context, _item, body = authority_absent_body()
    state = core.parse_state_block(body)

    check(
        "authority-absent: production predicates grant no authority and no Accept",
        rc.assessment_current_admitted(state) is False
        and rc.accept_recommendation_available(state) is False
        and rc.current_triage_authority_present(state) is False,
    )
    check(
        "authority-absent: historical telemetry remains",
        state.get(rc.TRIAGE_PRIMARY_STATUS_FIELD) == "failed"
        and state.get(rc.TRIAGE_PRIMARY_ERROR_FIELD) == "output.schema_invalid"
        and state.get(rc.TRIAGE_CONSUMPTION_FIELD) == "advisory",
    )
    check(
        "authority-absent: owner-facing copy stays explicitly advisory-only",
        "Primary model validation failed (`output.schema_invalid`), but the "
        "delivered candidate was consumed for advisory triage." in body
        and "This advisory result is not a primary validation success" in body
        and "The advisory assessment was not admitted" in body,
    )
    check(
        "authority-absent: never manufactures Accept or recommendation authority",
        "### Recommended action" not in body
        and "<!-- opt:accept-recommendation -->" not in body
        and "triage_recommendation" not in state
        and not rc.contradictory_advisory_telemetry(body, state),
    )


def test_admitted_assessment_without_working_accept_stays_unavailable():
    obs, context, item, base_body = _world()
    payload = _admitted_payload(obs, context, "close", "")
    body = rc.body_with_triage_result(
        base_body,
        HEAD,
        triage=payload,
        owner="kunchenguid",
        base_sha=item["base_sha"],
        primary_error_code="output.schema_invalid",
        consumption="advisory",
    )
    state = core.parse_state_block(body)
    check(
        "no-working-accept: admission alone does not become display authority",
        rc.assessment_current_admitted(state)
        and not rc.accept_recommendation_available(state)
        and not rc.current_triage_authority_present(state)
        and "consumed for advisory triage" in body
        and "<!-- opt:accept-recommendation -->" not in body,
    )

    item["triage"] = payload
    item["assessment"] = state.get(rc.ASSESSMENT_FIELD)
    item["triaged_sha"] = HEAD
    item[rc.TRIAGE_PRIMARY_ERROR_FIELD] = "output.schema_invalid"
    item[rc.TRIAGE_CONSUMPTION_FIELD] = "advisory"
    rendered = rc.render(item, owner="kunchenguid")["body"]
    rendered_state = core.parse_state_block(rendered)
    check(
        "no-working-accept: fresh render preserves the unavailable warning",
        rc.assessment_current_admitted(rendered_state)
        and not rc.accept_recommendation_available(rendered_state)
        and "consumed for advisory triage" in rendered
        and "<!-- opt:accept-recommendation -->" not in rendered,
    )


def test_corrected_authority_keeps_explicit_correction_copy():
    obs, context, item, base_body = _world()
    body = rc.body_with_triage_result(
        base_body,
        HEAD,
        triage=_admitted_payload(obs, context, "merge", "Corrected result."),
        owner="kunchenguid",
        base_sha=item["base_sha"],
        primary_error_code="output.schema_invalid",
        consumption="corrected",
    )
    state = core.parse_state_block(body)
    check(
        "corrected: authority present with Accept",
        rc.assessment_current_admitted(state)
        and rc.accept_recommendation_available(state)
        and state.get(rc.TRIAGE_CONSUMPTION_FIELD) == "corrected",
    )
    check(
        "corrected: current outcome explains authority came from the correction",
        "its single correction passed complete trusted validation" in body
        and "Recommendation authority comes from that corrected result" in body
        and "consumed for advisory triage" not in body
        and "<!-- opt:accept-recommendation -->" in body,
    )


def test_projection_refresh_heals_without_changing_authority():
    obs, context, item, body = authority_present_body()
    state = core.parse_state_block(body)
    # Stamp as the pre-migration residual presentation.
    legacy_state = dict(state)
    legacy_state["render_version"] = rc.ADVISORY_TELEMETRY_CONSISTENCY_SOURCE_VERSION
    legacy = body
    # Inject the contradictory warning a v14 card would still show.
    legacy_section = rc._existing_triage_section(legacy)
    injected = legacy_section.replace(
        rc.TRIAGE_END,
        "\n".join(
            [
                "",
                "> [!WARNING]",
                "> Primary model validation failed (`output.schema_invalid`), "
                "but the delivered candidate was consumed for advisory triage.",
                "> This advisory result is not a primary validation success; "
                "existing authority gates still apply.",
                rc.TRIAGE_END,
            ]
        ),
        1,
    )
    legacy = legacy.replace(legacy_section, injected, 1)
    legacy = rc._replace_state_block(legacy, legacy_state)
    check(
        "migration fixture: residual contradiction is present",
        rc.contradictory_advisory_telemetry(legacy) is True
        and rc.render_stale(core.parse_state_block(legacy)) is True,
    )

    admitted = rc.assessment_admission.normalize_assessment(
        state.get(rc.ASSESSMENT_FIELD)
    )
    prior = ob.issue_from_projection(
        {
            "title": "[firstmate#1074] fix",
            "body": legacy,
            "managed_labels": [
                "needs-decision",
                "kind:pr-review",
                "priority:med",
                "repo:firstmate",
                "target:firstmate-1074",
            ],
        },
        number=1735,
    )
    projection = card_projection.plan_card_projection(
        ob.item_for(obs, context, admitted),
        prior=prior,
        cause="migration-current",
        preserve_same_revision=True,
    )
    healed = projection["body"]
    healed_state = core.parse_state_block(healed)

    check(
        "migration: contradiction is gone after ordinary projection refresh",
        not rc.contradictory_advisory_telemetry(healed, healed_state)
        and "consumed for advisory triage" not in healed
        and "Tick **Accept recommendation**" in healed
        and "<!-- opt:accept-recommendation -->" in healed,
    )
    check(
        "migration: authority predicates and diagnostic state are unchanged",
        rc.assessment_current_admitted(healed_state) is True
        and rc.accept_recommendation_available(healed_state) is True
        and healed_state.get(rc.TRIAGE_PRIMARY_STATUS_FIELD) == "failed"
        and healed_state.get(rc.TRIAGE_PRIMARY_ERROR_FIELD) == "output.schema_invalid"
        and healed_state.get(rc.TRIAGE_CONSUMPTION_FIELD) == "advisory"
        and healed_state.get("triage_recommendation")
        == state.get("triage_recommendation")
        and healed_state.get(rc.ASSESSMENT_FIELD) == state.get(rc.ASSESSMENT_FIELD),
    )
    check(
        "migration: render version advances and second pass is a noop",
        healed_state.get("render_version") == rc.CARD_RENDER_VERSION
        and rc.CARD_RENDER_VERSION == 16
        and card_projection.plan_card_projection(
            ob.item_for(obs, context, admitted),
            prior=ob.issue_from_projection(
                {
                    "title": projection["title"],
                    "body": healed,
                    "managed_labels": projection["managed_labels"],
                },
                number=1735,
            ),
            preserve_same_revision=True,
        )["cause"]
        == "noop",
    )


def test_pure_body_heal_and_census_are_bounded_and_idempotent():
    _obs, _context, _item, present = authority_present_body()
    present_state = dict(core.parse_state_block(present))
    present_state["render_version"] = rc.ADVISORY_TELEMETRY_CONSISTENCY_SOURCE_VERSION
    section = rc._existing_triage_section(present)
    injected = section.replace(
        rc.TRIAGE_END,
        "\n".join(
            [
                "",
                "> [!WARNING]",
                "> Primary model validation failed (`output.schema_invalid`), "
                "but the delivered candidate was consumed for advisory triage.",
                "> This advisory result is not a primary validation success; "
                "existing authority gates still apply.",
                rc.TRIAGE_END,
            ]
        ),
        1,
    )
    residual = rc._replace_state_block(
        present.replace(section, injected, 1), present_state
    )
    _a, _c, _i, absent = authority_absent_body()

    healed = rc.body_with_coherent_advisory_telemetry(residual)
    healed_again = rc.body_with_coherent_advisory_telemetry(healed)
    absent_healed = rc.body_with_coherent_advisory_telemetry(absent)

    check(
        "heal: authority-present residual becomes coherent once",
        rc.contradictory_advisory_telemetry(residual) is True
        and rc.contradictory_advisory_telemetry(healed) is False
        and "consumed for advisory triage" not in healed
        and "<!-- opt:accept-recommendation -->" in healed
        and core.parse_state_block(healed).get("render_version")
        == rc.CARD_RENDER_VERSION,
    )
    check(
        "heal: second pass is idempotent and changes no authority keys",
        healed_again == healed
        and core.parse_state_block(healed).get("triage_recommendation")
        == core.parse_state_block(residual).get("triage_recommendation")
        and core.parse_state_block(healed).get(rc.ASSESSMENT_FIELD)
        == core.parse_state_block(residual).get(rc.ASSESSMENT_FIELD)
        and core.parse_state_block(healed).get(rc.TRIAGE_CONSUMPTION_FIELD)
        == "advisory",
    )
    check(
        "heal: authority-absent body is not rewritten into authority",
        absent_healed == absent
        or (
            # source-version stamp alone is allowed; never invent Accept
            "<!-- opt:accept-recommendation -->" not in absent_healed
            and "consumed for advisory triage" in absent_healed
            and not rc.accept_recommendation_available(
                core.parse_state_block(absent_healed)
            )
        ),
    )

    report = rc.contradictory_advisory_telemetry_census(
        [
            {
                "number": 1735,
                "body": residual,
                "labels": [{"name": "needs-decision"}, {"name": "kind:pr-review"}],
                "url": "https://example.test/1735",
            },
            {
                "number": 1759,
                "body": absent,
                "labels": [{"name": "needs-decision"}, {"name": "kind:pr-review"}],
                "url": "https://example.test/1759",
            },
            {
                "number": 999,
                "body": residual,
                "labels": [{"name": "processing"}, {"name": "kind:pr-review"}],
                "url": "https://example.test/999",
            },
        ]
    )
    post = rc.contradictory_advisory_telemetry_census(
        [
            {
                "number": 1735,
                "body": healed,
                "labels": [{"name": "needs-decision"}, {"name": "kind:pr-review"}],
            },
            {
                "number": 1759,
                "body": absent,
                "labels": [{"name": "needs-decision"}, {"name": "kind:pr-review"}],
            },
        ]
    )
    check(
        "census: reports the exact affected refreshable card and heals it",
        report["total"] == 3
        and [row["number"] for row in report["affected"]] == [1735]
        and report["healed_under_renderer"] == 1
        and report["clean"] == 1
        and any(row.get("number") == 999 for row in report["skipped"]),
    )
    check(
        "census: post-heal cohort is clean",
        post["affected"] == [] and post["clean"] == 2 and post["healed_under_renderer"] == 0,
    )


def test_census_ignores_model_prose_with_warning_phrase():
    _obs, _context, _item, body = authority_present_body()
    body = body.replace(
        "Restores Bash 3.2 parsing of one ship-brief heredoc.",
        "Explains why an earlier candidate was consumed for advisory triage.",
        1,
    )
    report = rc.contradictory_advisory_telemetry_census(
        [
            {
                "number": 1735,
                "body": body,
                "labels": [{"name": "needs-decision"}, {"name": "kind:pr-review"}],
            }
        ]
    )
    check(
        "census: model prose cannot impersonate the deterministic warning",
        rc.current_triage_authority_present(core.parse_state_block(body))
        and "consumed for advisory triage" in rc._existing_triage_section(body)
        and not rc.contradictory_advisory_telemetry(body)
        and report["affected"] == []
        and report["clean"] == 1,
    )


def test_preserve_same_revision_path_strips_contradiction():
    obs, context, item, body = authority_present_body()
    state = core.parse_state_block(body)
    legacy_state = dict(state, render_version=14)
    section = rc._existing_triage_section(body)
    injected = section.replace(
        rc.TRIAGE_END,
        "\n> [!WARNING]\n"
        "> Primary model validation failed (`output.schema_invalid`), but the "
        "delivered candidate was consumed for advisory triage.\n"
        "> This advisory result is not a primary validation success; existing "
        "authority gates still apply.\n"
        + rc.TRIAGE_END,
        1,
    )
    legacy = rc._replace_state_block(body.replace(section, injected, 1), legacy_state)
    fresh = rc.render(item, owner="kunchenguid")["body"]
    healed = rc._preserve_same_revision_triage(
        fresh, legacy, item, legacy_state, owner="kunchenguid"
    )
    healed_state = core.parse_state_block(healed)
    check(
        "preserve: contradiction removed while diagnostic state survives",
        "consumed for advisory triage" not in healed
        and healed_state.get(rc.TRIAGE_PRIMARY_STATUS_FIELD) == "failed"
        and healed_state.get(rc.TRIAGE_CONSUMPTION_FIELD) == "advisory"
        and rc.accept_recommendation_available(healed_state)
        and "<!-- opt:accept-recommendation -->" in healed,
    )


def main():
    test_authority_present_renders_one_coherent_actionable_state()
    test_authority_absent_stays_explicitly_unavailable()
    test_admitted_assessment_without_working_accept_stays_unavailable()
    test_corrected_authority_keeps_explicit_correction_copy()
    test_projection_refresh_heals_without_changing_authority()
    test_pure_body_heal_and_census_are_bounded_and_idempotent()
    test_census_ignores_model_prose_with_warning_phrase()
    test_preserve_same_revision_path_strips_contradiction()
    if FAILURES:
        print("\n%d failure(s):" % len(FAILURES))
        for name in FAILURES:
            print(" - " + name)
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
