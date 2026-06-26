---
name: Feature request
description: Suggest a new feature for the Message Gateway platform.
title: "[feat]: "
labels: ["enhancement", "triage"]
assignees: []
---

<!--
A feature request links to the relevant PRD user story when one
exists. If you are raising a brand-new idea, the "problem" section
is the most important: a feature without a problem rarely lands.
-->

## Problem

<!--
What user pain does this address? Tie it to a user story from
`PRD.md` when one applies (e.g. *"Como desarrollador…",* US-04
for delivery reports). If no PRD story exists, describe the
behaviour gap and who is asking for it.
-->

> PRD reference: US-# / not in PRD yet

## Proposed solution

<!--
A high-level description of the feature: what the API / UI / config
would look like, and how the user would interact with it. Show a
sample request / response when the change is API-shaped.
-->

```http
POST /v1/feature HTTP/1.1
Content-Type: application/json

{
  "..."
: "..."
}
```

## Alternatives considered

<!--
What other approaches were considered? Why is the proposed
solution the best one? Listing rejected alternatives is the most
useful part of a feature request for the reviewer.
-->

- **<Alternative A>**: rejected because …
- **<Alternative B>**: rejected because …

## Out of scope

<!--
What is explicitly *not* part of this feature. Use this to bound
the discussion; reviewers can challenge the scope here.
-->

## Acceptance criteria

<!--
A checklist a reviewer can use to mark the PR as "done". The
first three should always be filled in; the rest are nice-to-haves.
-->

- [ ] Documented in `PRD.md` (or a follow-up issue if PRD is unchanged).
- [ ] API contract reviewed by `@msg-gateway/backend`.
- [ ] UI flow reviewed by `@msg-gateway/frontend`.
- [ ] Backwards-compatible: yes / no (justify if no).
- [ ] Tests cover happy path **and** the failure paths listed below.
- [ ] Failure paths to cover: <list>
- [ ] Observability: log lines / metrics / traces added.
- [ ] Documentation: OpenAPI schema + a usage example.

## Additional context

<!--
Mockups, links to similar features in other platforms, customer
quotes, etc.
-->
