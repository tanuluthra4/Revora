# Manual end-to-end checklist (run on a local or staging copy, never production data)

Setup: `python app.py` with a fresh `DATABASE_PATH`. Use three browsers/profiles: **Author** (A), **Reviewer 1** (R1), **Reviewer 2** (R2), plus a logged-out window (L).

1. L: open `/`. Landing says Revora, shows the logo, two review goals, "not a certification", FAQ. No dead links.
2. Sign up R1 and R2 as reviewers; in each profile set visibility to *listed*. Sign up A as author; add a project.
3. A: **Request a review** with an empty project, a bad SHA, and a URL containing `user:pass@`: each is refused with a clear message. Then create it properly (goal *production*).
4. A: Start, continue, **Choose reviewers** (R1 and R2, 2 required), submit. The version shows *locked*; editing it is refused.
5. R2 (not yet chosen? use a third reviewer R3 unassigned): `/reviews/<id>` shows 404.
6. R1: Begin review. Open the report form. **Try to submit without ticking the disclosure**: the browser blocks it. Remove the `required` attribute in dev tools and submit: the server refuses it ("Report not submitted"), nothing is stored.
7. R1: tick the box and submit a report with one *critical* finding and a private note. R2: submit a second report. After the second one the page says **Review complete**, with the note that this is not a readiness verdict.
8. R1 cannot see R2's report. A sees both reports and **no private notes**.
9. A: mark the finding *fixed* with a note. R1: *reopen*, A: *fixed* again, R1: *confirmed*. A can no longer change it.
10. A: **Create public link** for version 1. L: open it: reports, all findings (critical visible), "not a certification", no e-mail, no private notes, no questions. The page is `noindex`.
11. A: **Revoke**. L: reload: "This link has been revoked", HTTP 410, nothing else. A: share again: new URL; the old URL still shows revoked.
12. A: **Start a new version**, edit the commit, choose/keep reviewers, submit; R1 reports with a follow-up finding. Share version 2 only: version 1 content is absent.
13. Integrity box reads "Integrity verified" for every request. Optional tamper demo from the README: it must turn red.
14. Old data (staging copy of a v4 database): an old verified proof still shows its history, replays OK, and its `/proof/<token>` link still works; a revoked old link still returns 410.
15. Narrow the window to 375 px: no horizontal scrolling on landing, request form, detail page, public page.
