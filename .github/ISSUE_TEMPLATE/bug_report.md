---
name: Bug report
description: Report a defect in the Message Gateway backend, frontend or infra.
title: "[bug]: "
labels: ["bug", "triage"]
assignees: []
---

<!--
A good bug report is reproducible. Fill in the *minimum* information
needed for a maintainer to reproduce the issue, and link the
relevant PRD / docs section when the bug is a deviation from
documented behaviour.
-->

## What happened

<!-- One or two sentences describing the defect. -->

## What I expected

<!-- The behaviour you expected, with a link to the relevant docs / PRD
section when applicable. -->

## Reproduction steps

<!--
A numbered list of the steps a maintainer can follow on a fresh
checkout. The output of `docker compose ps` and the request/response
of the failing call are the most useful artefacts.
-->

1.
2.
3.

## Environment

<!--
Fill in what applies; the maintainer can use the data below to
reproduce. Use code blocks for command output.
-->

- **Branch / commit SHA**: `git rev-parse HEAD`
- **Backend image**: `msg-gateway-backend:<tag>` (or `python --version` if running natively)
- **Frontend image**: `msg-gateway-frontend:<tag>` (or `node --version`)
- **OS**: `uname -a`
- **Browser** (if UI): `<name> <version>`
- **API key type** (if auth issue): `test / live`

## Logs & artefacts

<!--
Paste the relevant log lines (use a code block; redact API keys,
phone numbers and RUTs) and attach screenshots / HAR files when
they help. Use the `priv` tag for sensitive attachments.
-->

```
# Backend log:
...

# Frontend log / browser console:
...
```

## Impact

<!--
How many users / tenants are affected? Is there a workaround? Is
this a regression from a previous release?
-->

- **Severity**: <S0 outage | S1 degraded | S2 minor | S3 cosmetic>
- **Workaround**: <none / describe the manual steps>
- **Regression from**: <version or `not a regression`>

## Acceptance criteria

<!--
What does "fixed" look like? Reviewers can use this as the
definition of done.
-->

- [ ] The reproduction steps above no longer fail.
- [ ] A regression test covers the failure path.
- [ ] The CHANGELOG entry mentions the fix.
