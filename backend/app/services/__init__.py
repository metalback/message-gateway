"""Domain services.

Services orchestrate repositories, providers and external APIs.
They are the only layer that should be injected with side-effectful
collaborators (DB sessions, HTTP clients, queues); routes stay thin
and call into these modules.

Conventions:

- One service per file (``messaging.py``, ``billing.py`` …).
- Constructor injection of dependencies for testability.
- Public coroutines return DTOs, never ORM rows.

Modules in this package:

- ``auth.py``     – registration, login, API-key authentication
                    (issue #3).
- ``messaging.py`` – message-sending orchestration: dispatch
                    through the right provider adapter, persist
                    the outcome, refresh provider status on
                    demand (issue #4).
"""
