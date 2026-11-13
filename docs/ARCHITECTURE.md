# Architecture

Flask + Jinja + SQLite, no front-end build step. One process, one database file.

| Layer | Path | Responsibility |
|---|---|---|
| Automaton | `automata/` | Pure DFA: states, events, partial transition function, required actor. No database, no Flask. |
| Engine | `services/automata_engine.py` | Runs one event: unknown event, invalid transition, unauthorized actor, guard / evidence digest checks, then writes state, history and side effects in one `BEGIN IMMEDIATE` transaction with compare-and-swap on state and transition count. Refusals go to `blocked_actions`. |
| Integrity | `services/integrity.py` | Hash chain (`state_transitions`), evidence digest, `verify_chain` replay through the DFA. |
| Review services | `services/report_service.py`, `finding_service.py`, `version_service.py`, `share_service.py` | Report validation and sealing, finding lifecycle (per-finding hash chain), version snapshots, public links. |
| Directory | `services/reviewer_profiles.py` (reviewer profiles), `services/review_service.py` (review panel), `routes/reviewers.py` | Opt-in, self-reported reviewer profiles and picker. |
| Throttle | `services/login_throttle.py` | In-memory failed-login limiter. |
| Routes | `routes/` | Blueprints: auth, main, projects, tasks (review events, reviewer picker, queue), reviewers, reviews, public. Every state change is a POST behind CSRF. |
| Database | `database/` | `schema.sql` baseline, `migrations.py` (`PRAGMA user_version`, latest = 6), `backup.py`, `db.py` (`transaction()`). |

## One purpose, one engine
Revora has one workflow: CREATED -> IN_PROGRESS -> EVIDENCE_REQUIRED -> EVIDENCE_SUBMITTED -> VALIDATING -> REVIEWED (plus WITHDRAW and NEW_VERSION). REVIEWED records that the review finished; it sets no result and no completion timestamp. The first version of the project had an approve/reject verification flow. Its routes, pages and public proof links have been removed; the product no longer creates or shows it. Its transitions (VOTE_*, APPROVE, REJECT, RESTART, VERIFIED, REJECTED) are still defined in `automata/` and the engine only so that audit records written by that version replay and verify unchanged. They cannot be reached from any page. Removing them from the engine is a separate, larger change that would stop old chains from verifying.

## Authorization model
* Every task/review lookup is by id **and** relationship: owner, assigned reviewer, or nobody. Anyone else gets 404 (existence is not revealed).
* Roles are read from the database on every request, never from the session.
* Reviewers cannot see other reviewers' reports before submitting their own; an account can never review its own request.
* Public pages (`/r/<token>`) read only through the share-link table and select an explicit column list.

## Security controls
CSRF token on every POST (`hmac.compare_digest`); hashed passwords; session cookie flags (Secure in production); `Content-Security-Policy` (`script-src 'self'`, no inline scripts), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`; `noindex` on every page except the landing page; generic error pages; login throttle (limits in README).

## Out of scope (by design)
No code execution, no GitHub API access, no repository credentials, no AI reviewers, no ratings or reputation, no notifications, no payments.
