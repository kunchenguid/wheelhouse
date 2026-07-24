# Observation-bound PR-review card projection

Wheelhouse PR-review cards use the captain-authorized Option B architecture. The cutover keeps GitHub Issues as the state store while separating four responsibilities:

1. `scripts/wheelhouse_core.py` reduces target state into a revision-bound `ReviewObservation`.
2. `scripts/decision_context.py` builds bounded, neutral related-work context.
3. `scripts/assessment_admission.py` admits only typed, observation-bound advisory claims. The source-bound class-B semantic admission landed in PR 1631 remains authoritative.
4. `scripts/card_projection.py` plans a complete card, and `scripts/projection_writer.py` is the verified PR-review projection writer.

Target actions and action locks stay separate. Displayed criteria and related work never authorize an action.

## Production data flow

### ReviewObservation

`wheelhouse.review-observation/v2` binds:

- owner, repository, PR number, head SHA, base SHA, source, time, and observation ID;
- exact completeness dimensions for the target, checks, configured-check rows, mergeability, changed paths, action-required runs, and expected-head match;
- classifier facts, including open/draft/fork state, bucket, mergeability, compliance, tests, approval phase, and check phase;
- compact compliance, test, and informational rows from the same `check_status()` reduction that produces aggregate compliance and tests;
- a digest of the complete immutable changed-path set plus bounded path facts.

Incomplete or internally contradictory evidence is not current. `card_projection.py` renders explicit unknown values rather than reusing raw green or approval-needed facts.

A concrete `wheelhouse.target-observation/v1` persisted record remains readable only in `scripts/target_observation.py`. Migration marks configured checks and changed paths incomplete. Remove this compatibility reader only after normal telemetry proves that no trusted open or reusable card contains v1.

### DecisionContext

`wheelhouse.decision-context/v1` is bounded advisory context. It carries the ReviewObservation identity, repository snapshot identity and completeness, current candidate identities/heads/card links, and only these neutral relations:

- same closing issue;
- explicit target metadata reference;
- exact shared path from complete immutable lists.

Candidate and path bounds are fixed in `scripts/decision_context.py`. Truncation or incomplete comparison renders as incomplete, never as "no related work." DecisionContext is absent from auto-merge evaluation and final action guards.

### Assessment admission

PR triage returns a typed recommendation basis bound to the exact observation and context IDs. Trusted admission rejects stale, incomplete, malformed, or contradictory input. In particular, a configured-tests-not-run or configured-tests-not-green claim cannot survive complete green test rows.

A non-admitted assessment can remain visible with a content-free reason code, but it cannot:

- render an Accept recommendation checkbox;
- satisfy the G6 recommendation or behavior gates;
- trigger a target action.

PR 1631's source-bound class-B restoration evidence and contract-change contradiction checks remain the only class-B admission path. Invalid behavior class has tri-state dependent facts: the class row is UNMET and class-C mode is UNAVAILABLE. Valid A/B make class-C mode not-applicable MET. Valid C is MET only when strictly opt-in and default-off.

### CardProjection and writer

`wheelhouse.card-projection/v2` is a pure complete plan for:

- title;
- Situation, related context, triage, criteria, lifecycle, and controls;
- hidden state and source identities;
- exact managed labels;
- cause code and queue effect.

The owner comes from ReviewObservation, not process environment. Identical normalized inputs produce byte-identical output. The golden fixture is `tests/fixtures/option_b_card_projection.json`.

Observation, repository-snapshot, and context identities are semantic and revision-bound: collection timestamps remain visible provenance but do not change an identity when the target facts and related-work facts are unchanged. Same-closing-issue relations are repository-qualified.

The writer verifies the card by number before mutation, including identity, trusted author, lifecycle state, body, title, labels, `updatedAt`, and comment activity. It sends one issue-resource PATCH for title, body, and the complete final label set, then rereads and verifies the result. Human labels are preserved. PR-review direct body writers fail closed, including legacy paths without current observation evidence. Triage shares the `wheelhouse-backstop` concurrency group with scan, ingest, and the decision handler, while the handler consumes the immutable owner webhook body and its prior body. Handler mutations cannot interleave a projection, and an owner edit during a projection is still processed from its queued webhook snapshot.

A supported `decision:*` label erased between the writer's reread and PATCH has one narrow recovery path in `decision_label_recovery.py`. It requires the already-authorized webhook actor, exact card repository and node identity, identical target/head/observation/context binding, the exact label-set delta, and a complete label-event sequence consisting only of the triggering human `labeled` event followed by the trusted projection `unlabeled` event. Event and claim histories use fixed page caps and require a short terminal page; incomplete or oversized histories fail closed. A verified bot-authored claim comment makes that event single-use. The workflow rereads the claim and event sequence after taking `processing` and immediately before target action. Old, duplicate, replayed, new-head, relabeled, explicitly removed, ambiguous, malformed, or unsupported label events remain inert.

New-card creation still uses one complete Issue creation followed by direct-number admission verification. Stable legacy cards are not mass-rewritten. A real material/current, context, assessment, or lifecycle trigger migrates an open pure card through the v2 planner. Processing, blocked, and resolved cards retain the existing no-refresh rule.

### Async results and recovery

A terminal triage attempt persists a bounded `wheelhouse.assessment-result/v1` comment before projection. The comment visibly says whether projection is pending or complete and includes a content digest through its result identity. The projected card binds that result ID. If immediate projection fails, scheduled reconcile finds the exact revision record and retries without dispatching a model or reserving more budget. If only the final comment update failed after a verified card write, reconcile completes that finalization without rewriting the card.

The no-trusted-source security fallback remains the sole direct exception. It clears the queued cache, preserves its visible warning, and cannot fabricate a current assessment or criteria result.

## Action lane and lifecycle

Auto-merge now runs complete read-only G0-G6 before any card claim. A denied or unavailable preclaim produces `card_writes=0`. Only exact passers receive action-lock labels. Under claim, Wheelhouse rereads the card and reruns the authoritative gates. G7, current checks, current overlap, pinned head/base, VISION revision, and the unchanged final `do_merge` workflow gate still run immediately before action.

Manual merge and request-changes retain pinned-head verification. Fork CI approval retains the pwn-request hold.

On the first complete scheduled observation that an open target is outside the worklist, Wheelhouse writes a complete visible lifecycle projection:

- the current observation and reason are shown;
- decision controls are removed;
- checkbox, slash, and NL parsing are inert;
- the card remains open and refreshable;
- `wheelhouse:confirming-target-state` marks the state.

`scripts/scheduled_epoch.py` advances a dedicated trusted epoch only for scheduled workflow events. Manual runs cannot advance or reset progress. The next adjacent complete scheduled observation persists trusted soft-close provenance and closes the card. Failed, truncated, incomplete, UNKNOWN, or CI-wait scans do not close and can break adjacency safely.

## Six retained reproductions

The cutover was reproduced offline from retained source and fixtures. No live cards, workflows, targets, or production APIs were used.

| Finding | Retained mechanism and smallest counterfactual | Disconfirming evidence |
|---|---|---|
| WH-AUD-01 | The old `claim_cards` path added action locks before G3. A local candidate with no prior merged PR produced one card write. Moving complete G0-G6 before claim produced `card_writes=0`, after which the due visible projection refreshed once. | G7 and target acting were already fail-closed. The defect was queue mutation and refresh starvation, not an unauthorized merge. |
| WH-AUD-02 | The old first absence changed only hidden state while stale Merge controls remained visible. Replacing it with a complete lifecycle projection made the first absence visible and inert. A manual-run interleave did not change the scheduled epoch. | Existing two-run hysteresis prevented immediate close. It did not make the first queue promotion honest. |
| WH-AUD-03 | The triage task had no reducer-owned configured-check basis. The card-514 shape with green Ubuntu, macOS, Windows, and E2E rows admitted a prose tests-not-run premise. Supplying exact rows and a typed basis made the contradiction reject. | `check_status()` already reduced current checks correctly. The missing boundary was admission and task input, not check aggregation. |
| WH-AUD-04 | `behavior_class=INELIGIBLE` previously yielded a MET class-C dependent row. The smallest pure probe changed only dependent tri-state derivation and now yields UNAVAILABLE. | Overall eligibility was already false because the class row was UNMET. The bug was affirmative false evidence, not a successful merge. |
| WH-AUD-05 | The exact card-1620 / Firstmate PR-902 class-B fixture said an existing delivery contract was tightened while claiming unchanged restoration. PR 1631 now rejects the source-bound contradiction. The Option B layer preserves that admitted result and withholds G6/claim. | Phase 0 fixed class-B semantic admission. It did not supply observations, contexts, one projection owner, lifecycle truth, or preclaim ordering. |
| WH-AUD-06 | Optional model search could omit Firstmate PRs 901/905 and the tasks-axi PR 21 dependency. Deterministic shared paths and explicit references now produce reciprocal related sections even when the model omits them. | Same-closing-issue overlap already had an authoritative acting gate. Broad shared-path or dependency overlap intentionally remains advisory. |

Relevant history remains useful context: `60bac6e` introduced guarded scan-time auto-merge, `961f900` added shared criteria, `9a69b8c` added soft-close hysteresis, `214b85f` added activity reflection, `4cfd6d4` synchronized triage and criteria, `8363ecc` bound VISION sources, and `1cde3c6` established observation-bound CI-wait projection.

## Invariants

The production contract is encoded in `tests/test_option_b_architecture.py` and the existing action suites:

1. Situation, checks, context, assessment, behavior facts, and criteria share one revision or say unavailable.
2. Incomplete, malformed, mismatched, stale, or unknown input never becomes current green or MET authorization.
3. Displayed criteria and related context never authorize an action.
4. Manual target actions keep their existing current-head and security gates.
5. A failed G0-G6 preclaim performs no card write.
6. Title, body, and managed labels change through one complete verified projection update.
7. Every automated card mutation has an allowed visible or intentional ordering cause.
8. First absence is visible and inert, and only adjacent qualifying scheduled epochs soft-close.
9. A check-basis contradiction cannot produce Accept or satisfy G6.
10. Behavior class dependent facts remain tri-state.
11. Class B requires admitted source-bound restoration evidence.
12. Related work remains neutral and cannot auto-close, hold, or merge a target.
13. Projection drift and migration never spend by themselves.
14. Owner body, checkbox, label, comment, or close activity wins a race.
15. Fleet reads/actions, card writes, and model credentials retain their separate token boundaries.

## Offline acceptance matrix

`python tests/test_option_b_architecture.py` composes the required cases:

- E2E-01: G3 and unavailable preclaim denial, zero claim writes, one visible refresh, then no-op;
- E2E-02: exact visible first absence, manual interleave, second scheduled close;
- E2E-03: green current rows reject tests-not-run, while a failing test control admits;
- E2E-04: invalid-class dependent tri-state and A/B/C controls;
- E2E-05: exact card-1620 class-B semantic fixture;
- E2E-06: 901/905 reciprocal shared paths plus 901/21 dependency, with no acting gate;
- E2E-07: durable result recovery without spend and owner-race writer deferral.

The same test also covers strict v2/v1 contracts, timestamp-stable semantic identities, repository-qualified context relations, assessment binding, projection golden bytes, writer verification, migration ownership, workflow serialization, and token separation. Full validation remains the command list in `CONTRIBUTING.md`.

## Observability

Logs are structured and content-free. They never include target bodies, model prose, card comments, or credentials.

- `wheelhouse observation produced|incomplete`
- `wheelhouse context complete|truncated|unavailable`
- `wheelhouse assessment admitted|rejected|stale|unavailable`
- `wheelhouse projection-event planned|noop|deferred|committed|verification_failed`
- `wheelhouse card-write` with cause, changed sections, old/new time, queue effect, and verification
- `wheelhouse automerge preclaim_denied|preclaim_passed|claimed|released`
- `wheelhouse soft-close` with scheduled epoch, prior epoch, count, and completeness
- `wheelhouse run-summary` with projection outcomes, owner-race deferrals, incomplete inputs, assessment denials, recovery lag, and stuck held-card counts

DecisionContext incompleteness is counted and rendered but does not make the repository scan unhealthy or freeze unrelated maintenance. Projection verification failure, malformed acting evidence, and persistent result-to-projection lag remain correctness or liveness failures.

## Migration and removal condition

This cutover does not run a shadow writer and does not mass-rewrite cards.

- New and normally refreshed PR-review cards use v2 projection.
- Stable old cards remain untouched until a real trigger.
- Concrete v1 observation and state markers are dual-read under their existing owners.
- Historical provider results that predate `recommendation_basis` or typed behavior assertions remain readable as advisory triage, but their assessment and G6 admission are unavailable. Legacy Accept and class-B records are never grandfathered into admission.
- Closed cards, historical comments, and target repositories are not rewritten.
- Old PR-review body writers are reachable only for a concrete persisted legacy card that lacks a current v2 observation. Normal scan, ingest, triage-result, CI-wait, first-absence, activity, auto-merge hold/release, and reconcile paths use the verified writer once the card enters v2.

Remove the remaining v1 readers and legacy compatibility mutation fallback only when normal scheduled telemetry shows zero trusted open/reusable v1 cards through the reviewed observation window. Removal must preserve legacy state-marker parsing needed by closed-card trust checks.

## Rollback

Fix forward is the default. A rollback is one architecture-unit revert, not a piecemeal return to mixed writers.

1. Disable `auto_merge` globally before reverting any admission or projection code.
2. Preserve PR 1631's WH-AUD-05 semantic denial. Do not revert it with the architecture cutover.
3. Stop new v2 projection consumption by reverting this architecture PR as one unit while retaining the concrete v1 readers.
4. Leave cards at their last verified projection. Do not mass-rewrite or restore old bodies.
5. Ignore unsupported optional v2 context/projection fields strictly. Malformed data stays unavailable and never becomes MET.
6. Keep pinned-head manual action guards, G7, fork-CI security holds, and target token boundaries enabled.
7. Let the next scheduled run retry durable pending assessment records after the fixed writer is restored. Never replay provider spend to repair presentation.
8. Re-enable auto-merge only after the exact Option B test, full repository validation, and required GitHub checks pass on the rollback/fix-forward head.

If the writer alone is unavailable, no rollback is needed: cards remain at their last verified projection, stale target actions deny, and scheduled reconcile retries. If context alone is unavailable, maintenance continues with an explicit unavailable related-work section.
