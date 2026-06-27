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

from app.services.billing import (
    BalanceSummary,
    BillingError,
    InvalidBillingPeriodError,
    InvoiceAlreadyIssuedError,
    InvoiceDraft,
    InvoiceLineItem,
    InvoiceNotFoundError,
    PlanNotFoundError,
    apply_flow_status,
    compute_invoice,
    create_payment,
    finalize_invoice,
    get_balance,
    get_invoice,
    get_plan_by_code,
    list_invoices,
    list_payments,
    list_plans,
    persist_invoice_draft,
    refresh_payment_status,
    switch_subscription,
)
from app.services.dte import (
    DteDocument,
    DteError,
    DteSchemaError,
    DteService,
    FolioProvider,
)

__all__ = (
    "BalanceSummary",
    "BillingError",
    "DteDocument",
    "DteError",
    "DteSchemaError",
    "DteService",
    "FolioProvider",
    "InvoiceAlreadyIssuedError",
    "InvoiceDraft",
    "InvoiceLineItem",
    "InvoiceNotFoundError",
    "InvalidBillingPeriodError",
    "PlanNotFoundError",
    "apply_flow_status",
    "compute_invoice",
    "create_payment",
    "finalize_invoice",
    "get_balance",
    "get_invoice",
    "get_plan_by_code",
    "list_invoices",
    "list_payments",
    "list_plans",
    "persist_invoice_draft",
    "refresh_payment_status",
    "switch_subscription",
)
