# Revora

**Revora gets one version of your project reviewed.** An author submits a version (repository link and commit), chosen community reviewers each write a structured report with findings, the author fixes the findings, and a new version can be reviewed again. Every step is recorded in a tamper-evident history.

Live site: https://revora-414x.onrender.com/ . Nothing in this repository deploys or modifies it. See `docs/OPERATIONS.md` for rollout and rollback.

## How it works

1. **Author** creates a project and requests a review of one version (repository address, commit SHA, tech stack, goal: *production readiness* or *interview preparation*), then chooses 1-5 reviewers and how many must report.
2. **Reviewers** open the version and each submit a report: summary, strengths, what they assessed and did **not** assess, and findings (severity, category, explanation, suggested fix). They must first acknowledge that the author may publish the non-private text if they choose. Private notes stay with their author.
3. When enough reports exist the request becomes **Review complete**. That means the review is finished. It is not a pass or fail and not a statement that the project is ready. There is no approve or reject step.
4. **The author fixes findings**, each moving open, in progress, fixed. Only the reviewer who wrote a finding can mark it resolved.
5. **New version**: the next round starts from a copy of the last version. Earlier reports stay with their own version, and "Progress across versions" shows how findings moved.
6. **Optional sharing** of one version at `/r/<token>`, revocable at any time (revoked links return 410 with no data).

## States

`CREATED -> IN_PROGRESS -> EVIDENCE_REQUIRED -> EVIDENCE_SUBMITTED -> VALIDATING -> REVIEWED`, with `WITHDRAW` back to in progress and `NEW_VERSION` from reviewed. The state names are kept from the project's first version so older audit records still verify; the screens call them *Start, Choose reviewers, Sent to reviewers, Under review, Review complete*. The automaton (`automata/`) allows only listed transitions, checks the actor's role, and writes each transition into a hash chain.

## Principles

- No repository access: nothing is fetched or run, and repository credentials are never requested. The commit SHA is what the author typed.
- No ratings, endorsements, credentials or reputation scores. Reviewer information is self-reported and labelled so.
- The same account can never review its own request. Registration is open, so one person with several accounts cannot be detected.
- Reviews are community reviews, not certification.

## Configuration

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Session signing. **Required** when `REVORA_ENV=production` |
| `REVORA_ENV` | `production` enables Secure cookies and the SECRET_KEY check |
| `DATABASE_PATH` | SQLite file (default `database/revora.db`) |
| _(removed)_ `REVIEWER_EMAILS`, `REVIEWER_INVITE_CODE`, `REVIEWER_OPEN_SIGNUP` | No longer read. Delete them from the Render dashboard; leaving them set is harmless |
| `DEFAULT_REQUIRED_APPROVALS` | Pre-filled approvals required in the form (default `2`) |
| `PUBLIC_BASE_URL` | Optional, e.g. `https://your-service.onrender.com`. Only if set are canonical/`og:url` tags emitted |
| `PORT` | Used by `python app.py` |

Commands: `flask --app app init-db` (create/upgrade; takes a verified pre-migration copy first), `flask --app app backup-db [--out PATH]` (consistent verified copy, safe while running), `flask --app app set-role EMAIL ROLE`.

## Progress across versions

Each review request has a read-only page showing how findings moved between versions: counts by recorded status, which finding a later one follows up, and which critical or high findings are still unresolved. It uses only stored data and shows no score or grade.

## More documentation

`docs/ARCHITECTURE.md`, `docs/DATABASE.md`, `docs/OPERATIONS.md` (deployment, backup, restore, rollback), `docs/TESTING.md`, `docs/E2E_CHECKLIST.md`, `docs/AUDIT_REPORT.md` (audit, changes, security, readiness, limitations).

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider          # everything
python3 -m pytest -q tests/test_migration5.py                                # migration 5 + backups
python3 -m pytest -q tests/test_review_engine.py tests/test_review_routes.py # review mode, consent, sharing
```
Tests use temporary databases and never touch `database/revora.db`. They cover transitions, invalid moves, role/guard enforcement, rollback, concurrency, k-of-n quorum, tamper detection, migrations, open reviewer signup, profiles and privacy, directory and picker, public-page privacy and revocation, SEO headers, HTTP flows, and for review mode: consent bypass by direct POST, report digests, mode guards, finding lifecycle, version locking, per-version sharing and revocation.

## Deployment (Render)

See **`docs/OPERATIONS.md`** for the approved plan: a paid Render web service with a persistent disk and SQLite, staging first, backups, restore and rollback. `render.yaml` carries the disk settings only as commented guidance; applying them is a manual step that needs the owner's explicit approval. **Without a disk, SQLite on Render's free plan is ephemeral** and data is lost on every deploy or restart. Migration 5 runs on start, after an automatic verified copy (`<db>.pre-v5.bak`); if that copy cannot be made, the app does not migrate.

## Known limitations / honest claims

- **Open registration means no gatekeeping.** Anyone can become a reviewer and write any profile. Identity, employer, certificates and skills are *not* verified, and the interface says so. A developer can register extra reviewer accounts and select them (the backend only blocks the owner's own account, it cannot detect aliases). Counts of recorded decisions can be inflated this way, which is why they are shown as plain counts and not as reputation. Multiple reviewers do not equal organisational trust.
- **Not built (future work):** ratings, endorsements and reputation (would need abuse resistance and enough data), e-mail verification, link ownership verification (e.g. proving control of a GitHub account), reporting/moderation of profiles, rate limiting, reviewer notifications.
- **Tamper-evident, not tamper-proof.** Anyone who can write the SQLite file can recompute the whole chain. Private notes and sharing columns are outside the hash chain. Anchoring `chain_head` externally is future work.
- **The completeness checklist is not proof of authorship** and does not measure code quality. No automated GitHub checks.
- Share tokens are stored in plain text (unguessable, 144+ bits, revocable). Login has a small in-memory throttle (10 failures per address+email per 15 minutes; resets on restart, not shared between workers); sign-up has none; no e-mail verification or password reset.
- No reviewer reminders or expiry; an unavailable reviewer is handled by manual replacement or withdrawal.
- Contrast was computed for the colour tokens (see below), but no screen-reader or full WCAG audit has been performed, and no Lighthouse run is claimed.
- SQLite on Render's free plan is ephemeral; with a disk it is a single-instance database (no horizontal scaling, no zero-downtime deploys, whole-disk restore only). A monthly off-platform copy is recommended and its limits are in `docs/OPERATIONS.md`.
- The commit SHA and repository address are what the author typed; Revora does not check that they exist or match.
- Separate accounts do not prove reviewer independence (see above).

## Accessibility and SEO

Skip link, visible `:focus-visible` outlines, labelled form controls, landmarks, reduced-motion support. The only token pair that failed 4.5:1 (light-theme muted text on page and tinted backgrounds, 4.40 and 4.26) was darkened to `#56637a` (>= 5.29 on every background used). Only the landing page is indexable (title, description, Open Graph, favicon); every other page sends `noindex` (meta and `X-Robots-Tag`) and `robots.txt` disallows everything but `/`.

## Viva notes

- *Why an automaton?* Allowed lifecycle moves are data (a partial function δ); anything undefined is refused and logged, and the audit trail can be re-validated by replaying it through the same δ.
- *Why are votes self-loops and APPROVE a system event?* The outcome is a function of the recorded votes and the sealed policy, so nobody can assert "verified" directly; the replay recomputes it.
- *What stops double votes or lost updates?* A UNIQUE(workflow, round, reviewer) constraint, guards, `BEGIN IMMEDIATE`, and compare-and-swap on state and count.
- *What if evidence changes?* The digest sealed at submission must match at every vote; changing evidence requires withdrawing, which opens a new round.
- *Why is REVIEWED not terminal and not a verdict?* Quorum of reports and readiness are different questions. REVIEWED records that the review finished; only the owner's NEW_VERSION leaves it, and replay refuses any verdict event in a review-mode chain.
- *How is the mode tamper-evident?* It is sealed in the SUBMIT_EVIDENCE meta (absent means the original verification mode), so old chains replay unchanged and a flipped column is reported as MODE_MISMATCH.
- *Is it secure?* Tamper-evident, role-checked, CSRF-protected, with server-side validation of every reviewer assignment. Reviewer registration is open, so it is not tamper-proof and reviewer identity is not verified.

## Author
Tanu Luthra
