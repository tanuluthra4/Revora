# Database

SQLite, WAL mode, foreign keys on, autocommit connections with explicit `transaction()` (`BEGIN IMMEDIATE`, rollback on any error, a failed BEGIN never rolls back a caller-owned transaction). Schema version is `PRAGMA user_version` (latest **6**).

## Migrations (all idempotent, all preserve existing rows)
| # | Adds |
|---|---|
| 1 | `users.role`; workflow round / chain columns; hash-chain columns on `state_transitions`; `blocked_actions`; unique indexes; append-only triggers on `state_transitions` |
| 2 | `workflow_instances.reviewer_id`, `share_token` |
| 3 | `review_assignments`, `review_decisions` (append-only), `meta`, `required_approvals`, `share_revoked_at` |
| 4 | Reviewer profile columns, `reviewer_tags` |
| 5 | `submission_versions`, `review_reports`, `review_findings`, `finding_updates`, `share_links`; immutability / append-only triggers; consent columns NOT NULL |
| 6 | Replaces `share_links_guard_update`: revocation is one-way and **every** column is protected (id, workflow_id, round, kind, token, created_by, created_at), including in a combined revoke + tamper UPDATE |

Before any pending migration runs, the app copies an existing database with the SQLite online backup API to `<db>.pre-v6.bak` (a timestamped name if that exists), checks the copy with `PRAGMA integrity_check`, and **refuses to migrate if the copy fails**.

## Key invariants (enforced in the database, not only in code)
* `state_transitions`, `review_decisions`, `review_reports`, `finding_updates`, `submission_versions` cannot be updated or deleted (triggers; versions are locked once submitted).
* `share_links`: no delete; the only permitted update is setting `revoked_at` once. One active link per (workflow, round, kind) via a partial unique index.
* One decision per reviewer per round (UNIQUE). Reports carry a NOT NULL consent timestamp and version.
* Audit chain: `hash = SHA-256(fields + prev_hash)`; `verify_chain` also replays events through the DFA.

Limits: triggers can be dropped by anyone with write access to the file, and that person can recompute the chain. The design is tamper-*evident*, not tamper-proof.

## Backup / restore
`flask --app app backup-db [--out PATH]` writes a consistent, integrity-checked copy while the app runs. It rejects a missing source, never overwrites an existing file, writes through a temporary `.backup-*.partial` file and removes it on failure. Restore = stop the app, copy a backup over the database file (keep the old file), start the app; test restores on a separate copy first (see `docs/OPERATIONS.md`).
