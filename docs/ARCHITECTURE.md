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
| Routes | `routes/` | Blueprints: auth, main, projects, tasks (legacy verification), reviewers, reviews, public. Every state change is a POST behind CSRF. |
| Database | `database/` | `schema.sql` baseline, `migrations.py` (`PRAGMA user_version`, latest = 6), `backup.py`, `db.py` (`transaction()`). |

## Two modes on one engine
* **Verification mode** (original): CREATED -> IN_PROGRESS -> EVIDENCE_REQUIRED -> EVIDENCE_SUBMITTED -> VALIDATING -> VERIFIED / REJECTED.
* **Review mode** (`review_mode` sealed in the SUBMIT_EVIDENCE meta; absent = verification): adds SUBMIT_REPORT, COMPLETE_REVIEW, NEW_VERSION and the non-terminal REVIEWED state. REVIEWED records that the review finished; it sets no result and no completion time. Replay rejects verdict events in a review-mode chain.

## Authorization model
* Every task/review lookup is by id **and** relationship: owner, assigned reviewer, or nobody. Anyone else gets 404 (existence is not revealed).
* Roles are read from the database on every request, never from the session.
* Reviewers cannot see other reviewers' decisions before deciding; an account can never review its own request.
* Public pages (`/r/<token>`, `/proof/<token>`, `/u/<name>`) read only through the share-link table and select an explicit column list.

## Security controls
CSRF token on every POST (`hmac.compare_digest`); hashed passwords; session cookie flags (Secure in production); `Content-Security-Policy` (`script-src 'self'`, no inline scripts), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`; `noindex` on every page except the landing page; generic error pages; login throttle (limits in README).

## Out of scope (by design)
No code execution, no GitHub API access, no repository credentials, no AI reviewers, no ratings or reputation, no notifications, no payments.
