/**
 * Shared TypeScript types for the "Admin" dashboard
 * (issue #10).
 *
 * The module mirrors the wire format the FastAPI
 * ``/v1/admin/*`` surface returns so the renderer can
 * iterate over a response without an extra mapping layer.
 * The two ``*_list_filters`` interfaces encode the
 * dashboard's filter form state into the query string the
 * service expects.
 */

/** Commercial plan the customer is on. */
export type AdminClientPlan = 'starter' | 'growth' | 'enterprise';

/** Lifecycle status of a :class:`Client` row. */
export type AdminClientStatus = 'active' | 'suspended' | 'pending';

/** Role a :class:`Client` row has on the platform. */
export type AdminClientRole = 'client' | 'admin';

/** Delivery channel a :class:`Message` was dispatched on. */
export type AdminMessageChannel = 'sms' | 'whatsapp';

/** A single row of the admin clients table. */
export interface AdminClientRow {
  readonly id: string;
  readonly name: string;
  readonly email: string;
  readonly rut: string;
  readonly plan: AdminClientPlan;
  readonly status: AdminClientStatus;
  readonly role: AdminClientRole;
  readonly api_key_last4: string;
  readonly markup_percent: number;
  readonly markup_fixed_clp: number;
  readonly created_at: string;
  readonly updated_at: string | null;
}

/** Envelope of the ``GET /v1/admin/clients`` response. */
export interface AdminClientListResponse {
  readonly items: ReadonlyArray<AdminClientRow>;
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
  readonly has_more: boolean;
}

/** Filters the dashboard submits to ``GET /v1/admin/clients``. */
export interface AdminClientListFilters {
  q?: string | null;
  plan?: AdminClientPlan | null;
  status?: AdminClientStatus | null;
  limit?: number;
  offset?: number;
}

/** Body of ``POST /v1/admin/clients``. */
export interface AdminCreateClientRequest {
  name: string;
  email: string;
  rut: string;
  password: string;
  plan?: AdminClientPlan | null;
}

/** Response of a successful ``POST /v1/admin/clients``. */
export interface AdminCreateClientResponse {
  readonly client: AdminClientRow;
  readonly api_key: string;
  readonly api_key_last4: string;
}

/** Body of ``PATCH /v1/admin/clients/{id}``. */
export interface AdminUpdateClientRequest {
  name?: string | null;
  plan?: AdminClientPlan | null;
  status?: AdminClientStatus | null;
  markup_percent?: number | null;
  markup_fixed_clp?: number | null;
}

/** Response of a successful ``PATCH /v1/admin/clients/{id}``. */
export interface AdminClientUpdateResponse {
  readonly client: AdminClientRow;
}

/** Response of a successful ``POST /v1/admin/clients/{id}/suspend``. */
export interface AdminClientSuspendResponse {
  readonly client: AdminClientRow;
  readonly suspended_at: string;
}

/** Aggregate counters for the admin overview card. */
export interface AdminOverview {
  readonly period_start: string;
  readonly period_end: string;
  readonly total_clients: number;
  readonly active_clients: number;
  readonly suspended_clients: number;
  readonly pending_clients: number;
  readonly admin_users: number;
  readonly total_messages: number;
  readonly billable_messages: number;
  readonly delivered_messages: number;
  readonly failed_messages: number;
  readonly pending_messages: number;
  readonly total_revenue_clp: number;
}

/** A single ``(provider, channel)`` bucket in the
 *  ``GET /v1/admin/stats/by-provider`` response.
 *
 *  ``avg_latency_ms`` is the mean wall-clock duration of
 *  a successful ``provider.send`` call, in milliseconds,
 *  across the rows in the bucket. The value is ``null``
 *  for buckets that have no observed dispatches (a
 *  freshly-rolled deployment, for example); the
 *  dashboard renders a "—" placeholder in that case. */
export interface AdminProviderBreakdownRow {
  readonly provider: string;
  readonly channel: AdminMessageChannel;
  readonly total: number;
  readonly delivered: number;
  readonly failed: number;
  readonly pending: number;
  readonly cost_clp: number;
  readonly fee_clp: number;
  readonly avg_latency_ms: number | null;
}

/** A single row of the admin error log table. */
export interface AdminErrorLogEntry {
  readonly message_id: string;
  readonly client_id: string;
  readonly client_name: string;
  readonly client_email: string;
  readonly channel: AdminMessageChannel;
  readonly to_number: string;
  readonly provider: string;
  readonly error_code: string | null;
  readonly error_message: string | null;
  readonly created_at: string;
}

/** Envelope of the ``GET /v1/admin/logs`` response. */
export interface AdminErrorLogResponse {
  readonly items: ReadonlyArray<AdminErrorLogEntry>;
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
  readonly has_more: boolean;
}
