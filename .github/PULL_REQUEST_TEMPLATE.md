<!--
Pull Request template for the Message Gateway monorepo.

The body is broken into the four sections a reviewer needs to land the
change safely: context, what changed, how it was tested, and the
checklist. Delete the comments and any section that does not apply
to your PR (for example, the "Database migrations" block is not
relevant to a docs-only change).
-->

## Summary

<!--
One or two sentences on the *why* of the change. Mention the issue
or PRD section the PR addresses; a reviewer should be able to read
the description and know what the PR is for without opening the diff.
-->

> Closes #<issue-number> · relates to <PRD / docs section>

## What changed

<!--
A bullet list of the user-visible or developer-visible changes.
Group by area (`backend/`, `frontend/`, `infra/`) when the PR
spans more than one. Keep the list to the essentials; the diff is
the source of truth for the rest.
-->

- **Backend:**
- **Frontend:**
- **Infra / docs:**

## How it was tested

<!--
List the commands you ran locally (paste the relevant output in
the linked issue, not here) and the CI jobs you expect to gate
the change. `make ci` is the canonical local gate; mention it
explicitly if you ran it.
-->

- [ ] `make backend-test` (lint + typecheck + pytest)
- [ ] `make backend-typecheck`
- [ ] `make frontend-lint`
- [ ] `make frontend-build` (production build = tsc gate)
- [ ] `make compose-validate`
- [ ] New tests cover: <list the new test cases or files>
- [ ] Coverage delta on `app/`: <X% → Y%> (must stay ≥ 80%)

## Database migrations

<!--
Skip this block for non-backend changes. If Alembic migrations are
touched, fill it in: the autogenerate rarely produces a clean
diff and reviewers want to know you read it.
-->

- [ ] `alembic revision --autogenerate -m "..."` was run
- [ ] `upgrade()` and `downgrade()` are both populated
- [ ] Migration is forward-compatible with a hot running DB
      (no `NOT NULL` columns without a default, no
      destructive `drop_column` without a separate deprecation PR)
- [ ] `make alembic-upgrade` runs cleanly on a fresh database

## Rollout & risk

<!--
For every PR, describe the rollout plan and the rollback path.
A "no-op" or docs-only PR can answer "no risk" and stop; a
backend change touching a route should describe whether the
change is backwards compatible.
-->

- Backwards compatible: **yes / no**
- Feature flag: **none / `<flag name>`**
- Rollback plan: <revert the commit / run `<sql>` / disable the flag>

## Checklist

<!--
This is the same list CONTRIBUTING.md §9 expects a reviewer to
walk through; fill it in for the author side so reviewers do
not have to re-derive the answers.
-->

- [ ] Conventional Commit title (`feat:`, `fix:`, `chore:`, …).
- [ ] Tests cover the new behaviour, including failure paths.
- [ ] No secrets in code, fixtures or screenshots.
- [ ] No PII (real phone numbers, RUTs) in fixtures.
- [ ] `make ci` is green locally; CI is the source of truth.
- [ ] Fixture tests in `test_infra.py` still pass.
- [ ] `PRD.md` updated (if the change is product-visible).
- [ ] `CHANGELOG.md` updated under the `Unreleased` section (if
      the change is user-visible).

## Screenshots

<!--
Required for any UI change. Drag a screenshot into the comment
box and describe what the reviewer is looking at; the rendered
diff is not a substitute for a visual check.
-->

<!-- Screenshot here. -->
