# Revora — Audit, Stabilization and Submission Readiness Report

Audited tree: the supplied `proof-of-work-os.zip` (Stage 2 review platform), worked on in an isolated copy. The original project and its database were not modified (SHA-256 of `pow_os.db` checked unchanged before and after). Nothing was committed, pushed or deployed.

## A. Audit findings

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | `share_links_guard_update` trigger (schema v5) only guarded part of the row; a revoked link could be un-revoked and `token`/`workflow_id`/`round`/`kind` etc. could be edited, including in the same UPDATE that revokes | High | **Fixed** (migration 6), 37 tests incl. one per immutable column and revoke + tamper |
| 2 | `GET /tasks/<id>/edit` returned HTTP 500 for every task (`sqlite3.Row` has no `.get` in the template). Present in the original code | High (feature unusable) | **Fixed** (`dict(task)`), regression test |
| 3 | Backup could fail where hard links are unsupported | Medium | **Fixed** (fallback to exclusive-create copy, fsync, cleanup), tests |
| 4 | No brake on password guessing | Medium | **Mitigated**: in-memory throttle, 10 failures / 15 min per address+email. Limits documented |
| 5 | No Content-Security-Policy; inline `onclick` in a template | Medium | **Fixed**: CSP added, handler moved to `main.js` |
| 6 | Finding statuses shown with internal names | Low | **Fixed**: display labels only; stored values unchanged |
| 7 | Unreferenced files (`add_task.html`, `static/favicon.svg`), unused imports | Low | Removed |
| 8 | Pages without an `<h1>` (login, signup, task form, new project, error); 7-column table overflowed at 375px; small touch targets | Low | **Fixed** (verified in a browser at 375/768/1280) |
| 9 | `requirements-dev.txt` lacked Pillow, which `tests/test_branding.py` imports (a clean install would fail) | Low | **Fixed** |
| 10 | No project edit/delete pages exist | Info | Not added (scope); README corrected; tested that no such route exists |
| 11 | Signup reveals whether an email/username is already registered | Low | Not changed; documented limitation |

Verified as already correct (by reading the code and by tests): transaction helper (`BEGIN IMMEDIATE`, rollback, COMMIT failure, caller-owned transactions); engine check order; owner cannot verify own finding; only the original reviewer can verify; history preserved; invalid transitions rejected; consent enforced server-side; public page excludes private notes, e-mails and session data; revoked link returns 410 with no data; self-review blocked.

## Post-audit changes (rename and one enhancement)
* **Rename Revisely -> Revora** in all text, templates, docs, tests and logo filenames (`revora-logo-*`). The logo image is an "R" mark with no wordmark; its bytes are unchanged (the master-hash test still passes). The repository and Render service names are unchanged.
* **New: "Progress across versions"** (`/reviews/<id>/progress`, `services/progress_service.py`, `templates/review_progress.html`, 4 tests). Read-only view for the author and the assigned reviewers: findings per version grouped by recorded status, follow-up links via the existing `prior_finding_id`, and a list of critical/high findings still unresolved. Counts only: no score, grade or badge. A reviewer sees only their own findings; the author sees all; no private notes. No schema change.

* **Landing page:** the two role cards looked different because the directory's `.reviewer-card` CSS class was also applied to the landing card and overrode its border, padding and margin. The landing cards now have their own classes and measure identically (382 px tall, buttons at the same offset, checked in Chromium at 1280 px). Copy was rewritten for Revora, with a principles strip and numbered step cards.
* **Full UI pass (all pages, 1280 px and 375 px, screenshots reviewed):** mobile header no longer uses a third of the screen (brand and user on one row, navigation scrolls sideways); finding-update form rebuilt as an aligned grid; landing sections share one width, step cards are a clean 5/2/1 grid, footer aligned; login, signup and shared-review pages now carry the logo; user-facing "developer" wording became "author" (the stored role value is unchanged); old verification wording removed from the dashboard, reviewer queue, projects and auth pages; review-request cards no longer show the legacy "Mark done" / "self-reported" controls; reviewer queue showed "None UTC" for an undecided request, now shows a dash.
  **Known cosmetic limits:** some edge labels in the workflow diagram still sit close to node borders; automated tests cover wording and controls, but layout itself was checked by screenshots, not by tests; one test for the "None UTC" fix does not exercise a decided row.
* **Names deliberately left as `proof-of-work-os` / `pow_os`:** the Render service name, the GitHub repository link and the database filename. Changing them in this tree could create a second Render service with an empty database, break the repository link, or point the app at an empty database. `docs/OPERATIONS.md` explains how to rename each safely, with your approval. This report keeps the word "Revisely" as the record of the rename.

## B. Change report (relative to the supplied ZIP)

Modified: `app.py` (CSP header, unused import), `database/migrations.py` (migration 6), `database/backup.py` (publish-without-overwrite fallback), `routes/auth.py` (throttle), `routes/tasks.py` (edit-page fix, unused import), `services/finding_service.py` (display labels), `templates/{error,login,signup,task_form,profile,review_queue,task_detail,projects/new_project}.html`, `static/css/style.css`, `static/js/main.js`, `requirements-dev.txt`, `README.md`, `docs/OPERATIONS.md`, `tests/{conftest,test_db_transaction,test_migration5}.py`.
New: `services/login_throttle.py`, `docs/{ARCHITECTURE,DATABASE,TESTING,AUDIT_REPORT}.md`, `tests/{test_share_links_trigger,test_backup_restore,test_hardening,test_review_concurrency,test_authorization_matrix,test_e2e_workflow}.py`.
Removed: `templates/add_task.html`, `static/favicon.svg` (both unreferenced).
Schema: **one new migration (6)**, trigger replacement only, no table or data change. Dependencies: no runtime change; `Pillow` added to dev requirements. The logo and brand assets are untouched.

## C. Test report
Baseline (supplied tree): 290 passed. Final: **395 passed, 0 failed, 0 skipped** (`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider`, Python 3.13.16). Mutation checks: removing the task-ownership check makes 13 authorization tests fail; the trigger tests were checked against the old trigger.
Also done: migration 5 -> 6 on a **copy** of your real database: integrity_check ok, foreign_key_check empty, all 21 table row counts unchanged, `state_transitions` content identical, `verify_chain` ok for all 3 workflows, `.pre-v6.bak` created, and the new trigger blocked token edit, un-revoke and revoke+tamper on that copy. Browser sweep: 20 pages x 3 widths (375/768/1280), no 500s, no JS errors, no horizontal overflow.
**Not performed:** screen-reader testing, formal WCAG audit, Lighthouse, load testing, any test on the live Render service, a restore drill on Render, cross-browser testing (Chromium only).

## D. Security and data integrity
Controls are listed in `docs/ARCHITECTURE.md` and `docs/DATABASE.md`. Residual risks: tamper-evident not tamper-proof; alias accounts cannot be detected under open registration; throttle is per-process; share tokens stored in plain text (unguessable, revocable); no e-mail verification or password reset; account/email enumeration at sign-up; commit SHAs and repo URLs are not verified; no code is run, so findings are opinions.

## E. Deployment readiness
| Item | Status |
|---|---|
| Tests, migration on a copy, backup/restore on separate files, browser sweep | Verified locally |
| Staging deployment | Not done |
| Live Render service, its disk/plan, live database | **Not touched, not verified**; no deployment was made |
| Production rollout | Waiting for your approval. Follow `docs/OPERATIONS.md` (staging first, backup first). Render facts there come from its documentation as read on 2026-10-26; no prices verified. |

## F. Submission package
`revora-submission.zip`: source, README, ARCHITECTURE, DATABASE, OPERATIONS (deployment, backup, restore, rollback), TESTING, E2E checklist, this report. Excludes `.env`, databases, backups, venvs, caches and git data.

## G. Honest assessment — **Ready with limitations**
1. *Functionally complete?* Yes for the scoped workflow; project edit/delete are not implemented.
2. *Core workflow working?* Yes: the 20-step journey passes end to end over HTTP.
3. *Database safe?* Yes locally: transactions, immutability triggers, verified pre-migration backup. Live data not verified.
4. *Authorization and privacy enforced?* Yes, per code review and a 37-test matrix; public pages tested for leakage.
5. *Automata meaningful and integrated?* Yes: every transition and the audit replay go through the DFA.
6. *Explainable in a viva?* Yes; see README "Viva notes". Be ready to state the limits above plainly.
7. *Ready for submission?* Yes as a local/demonstrable project. Not yet verified as a live deployment.

After submission: external anchoring of the chain head, e-mail verification and password reset, shared rate limiting, project edit/delete, a kept browser test suite, a screen-reader pass.
