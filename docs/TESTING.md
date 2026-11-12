# Testing

```bash
pip install -r requirements-dev.txt          # Flask, Werkzeug, gunicorn, pytest, Pillow
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
```
Tests use temporary databases only and never open `database/revora.db`.

Result of the last full run on the audited tree (Python 3.13.16): **395 passed, 0 failed, 0 skipped, 14 warnings** (the warnings come from the image-comparison code in `test_branding.py`).

| File | Tests | Covers |
|---|---|---|
| test_automata, test_engine, test_quorum, test_integrity | 73 | DFA, guards, roles, k-of-n quorum, hash chain and tamper detection |
| test_routes, test_evidence, test_reviewer_directory | 98 | routes, validation, isolation, profiles, directory |
| test_review_engine, test_review_routes | 60 | review mode, consent, findings, versions, sharing |
| test_migrations, test_migration5 | 21 | legacy upgrade, idempotence, backup before migrate |
| test_share_links_trigger | 37 | every immutable column, one-way revoke, revoke + tamper |
| test_backup_restore, test_db_transaction, test_review_concurrency | 16 | backup rules, rollback, COMMIT failure, concurrent events |
| test_authorization_matrix | 37 | every task/review route against the wrong actor; anonymous access; project/profile privacy; edit-page regression |
| test_hardening | 12 | login throttle, CSP, dead-code regressions |
| test_progress | 4 | progress-across-versions page: counts, follow-up links, privacy, empty state |
| test_e2e_workflow | 1 | full student/reviewer journey over HTTP with negatives |
| test_branding | 36 | logo integrity and branding assets |

Manual checklist: `docs/E2E_CHECKLIST.md`.

## Not covered by automated tests
Real browser clicking of the whole flow (a scripted Playwright sweep of 20 pages x 3 widths was run once, not kept as a test); screen-reader testing; load testing; behaviour on Render itself.
