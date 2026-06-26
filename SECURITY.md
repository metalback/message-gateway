# Security Policy

Message Gateway processes user-controlled phone numbers, API keys and
billing data. We take the security of the platform seriously and
encourage coordinated disclosure of any vulnerability you may find.

---

## Supported versions

The table below tracks which lines of development receive security
fixes. Out-of-scope versions are not patched; please upgrade to a
supported release.

| Version  | Supported            |
| -------- | -------------------- |
| `main`   | ✅ Active development |
| latest   | ✅ Backported fixes   |
| < latest | ❌ No backports       |

We follow [semantic versioning](https://semver.org/) and publish
release notes for every tagged version.

---

## Reporting a vulnerability

**Please do not file a public issue.** A public disclosure gives an
attacker time to exploit the issue before a fix is rolled out.

Instead, report privately:

1. **Email** — `security@msg-gateway.example.com` (PGP key in
   [`docs/security/pgp.asc`](./docs/security/pgp.asc) once published).
2. **GitHub Security Advisories** — open a *private* security
   advisory through the *Security → Advisories* tab. This is the
   preferred channel for code-related issues because it lets us
   collaborate on a fix in a private fork before disclosing publicly.
3. **Out-of-band** — if both channels above are unavailable, contact
   a maintainer directly (see `CODEOWNERS` for the security team).

Please include, to the extent you are comfortable sharing:

- A short description of the issue and its impact.
- Steps to reproduce or a proof-of-concept (link to a private
  repo, gist, or screenshot is fine).
- The commit SHA or release tag you tested against.
- The environment (`docker compose ps`, OS, Python / Node version).
- Whether the issue is currently being exploited in the wild.

---

## What to expect

We follow a 90-day disclosure window modelled on
[Google's Project Zero](https://googleprojectzero.blogspot.com/p/vulnerability-disclosure-faq.html).

| Day  | Action                                                 |
| ---- | ------------------------------------------------------ |
| 0    | Report received; acknowledgment within **2 business days**. |
| 7    | Triage: severity assigned, reporter updated.           |
| 30   | Fix in progress on a private fork; status update sent. |
| 90   | Patch released publicly; CVE assigned if applicable.   |

We will credit reporters in the release notes (unless they prefer
to remain anonymous) and coordinate the disclosure date so the
patch and the report land on the same day.

---

## Scope

The following are **in scope** for the bounty / disclosure programme:

- Authentication, authorisation and API key handling in
  `app/routes/auth.py` and the `app.api` aggregator.
- Cross-tenant data leaks in the SQLAlchemy models under
  `app/models/`.
- Webhook signature / replay protection in `app/routes/webhooks.py`.
- PII redaction failures in `app/observability/redact.py` (real
  phone numbers, RUTs, API keys).
- Rate-limiting / quota bypass in the Redis-backed limiter.
- Sandbox escape in the provider adapter layer
  (`app/adapters/`).

The following are **out of scope**:

- Vulnerabilities in third-party providers (Meta Cloud API, the
  local SMS aggregator, Flow) – please report those upstream.
- Denial-of-service via API key flooding without bypassing the
  rate limiter.
- Reports from automated scanners without a working PoC.
- Theoretical issues that require an attacker to already have
  shell access on the host or the DB.

---

## Hardening checklist for contributors

Before opening a PR, confirm:

- [ ] No secrets in code, fixtures, screenshots or commit messages.
- [ ] No PII (real phone numbers, RUTs, customer names) in tests.
- [ ] New dependencies are pinned with `~=` and listed in
      `requirements.txt` *and* `requirements-dev.txt` if test-only.
- [ ] Database migrations have both `upgrade()` and `downgrade()`.
- [ ] The CI workflow (`.github/workflows/ci.yml`) is green for
      the new code paths (lint, typecheck, tests).
- [ ] PII redactors in `app/observability/` are exercised for any
      log line that may carry a phone number, RUT or API key.

---

## Security advisories

Past advisories are published under
[GitHub Security Advisories → `msg-gateway/message-gateway`](https://github.com/msg-gateway/message-gateway/security/advisories).
Each advisory includes the affected versions, the fix, and the
CVE number when one is assigned.

---

## Contact

- **Security team** — `@msg-gateway/security` (GitHub) ·
  `security@msg-gateway.example.com`
- **On-call** — `@msg-gateway/platform` (GitHub) ·
  `oncall@msg-gateway.example.com`
- **Disclosure coordinator** — see `CODEOWNERS` for the current
  owner of `SECURITY.md`.

> _This policy is adapted from the
> [GitHub Security Lab disclosure template](https://github.com/github/securitylab/blob/main/docs/disclosure.md)
> and the [Google Project Zero disclosure FAQ](https://googleprojectzero.blogspot.com/p/vulnerability-disclosure-faq.html)._
