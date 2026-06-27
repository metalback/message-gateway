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
- ``webhooks.py`` – delivery-receipt subscription CRUD plus
                     the signed outbound POST helper (issue #5).
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
from app.services.webhook_delivery import (
    WebhookDeliveryClient,
    WebhookDeliveryResult,
)
from app.services.webhooks import (
    WebhookCreationResult,
    WebhookError,
    WebhookNotFoundError,
    WebhookValidationError,
    build_receipt_payload,
    create_webhook,
    delete_webhook,
    deliver_receipt,
    eligible_subscriptions,
    event_for_status,
    get_webhook,
    list_webhooks,
    sign_payload,
    update_webhook,
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
    "WebhookCreationResult",
    "WebhookDeliveryClient",
    "WebhookDeliveryResult",
    "WebhookError",
    "WebhookNotFoundError",
    "WebhookValidationError",
    "apply_flow_status",
    "build_receipt_payload",
    "compute_invoice",
    "create_payment",
    "create_webhook",
    "delete_webhook",
    "deliver_receipt",
    "eligible_subscriptions",
    "event_for_status",
    "finalize_invoice",
    "get_balance",
    "get_invoice",
    "get_plan_by_code",
    "get_webhook",
    "list_invoices",
    "list_payments",
    "list_plans",
    "list_webhooks",
    "persist_invoice_draft",
    "refresh_payment_status",
    "sign_payload",
    "switch_subscription",
    "update_webhook",
)
