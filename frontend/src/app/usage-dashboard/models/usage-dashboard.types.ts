/**
 * Shared TypeScript types for the "Historial y consumo" dashboard.
 *
 * The module keeps the wire-format names (``channel``,
 * ``status``, ``cost_clp`` …) so the renderer can iterate
 * over a response with no extra mapping layer; the few
 * exceptions (e.g. parsing ``period_start`` into a more
 * dashboard-friendly form) live next to the components
 * that actually consume them.
 */

/** Delivery channel the message was dispatched on. */
export type MessageChannel = 'sms' | 'whatsapp';

/** Lifecycle status reported by the platform. */
export type MessageStatus =
  | 'pending'
  | 'queued'
  | 'sent'
  | 'delivered'
  | 'failed'
  | 'unknown';

/** A single row of the message history table. */
export interface MessageRow {
  readonly id: string;
  readonly channel: MessageChannel;
  readonly status: MessageStatus;
  readonly to_number: string;
  readonly body: string;
  readonly provider: string;
  readonly provider_msg_id: string | null;
  readonly error_code: string | null;
  readonly error_message: string | null;
  readonly cost_clp: number;
  readonly fee_clp: number;
  readonly created_at: string;
}

/** One page of the message history response. */
export interface MessageListResponse {
  readonly items: ReadonlyArray<MessageRow>;
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
  readonly has_more: boolean;
}

/**
 * Filters the dashboard submits when fetching a page of
 * the history. The service translates the values into
 * query parameters; an empty string / ``null`` filter
 * means "no filter on this field".
 */
export interface MessageHistoryFilters {
  channel?: MessageChannel | null;
  status?: MessageStatus | null;
  since?: string | null;
  until?: string | null;
  limit?: number;
  offset?: number;
}

/** The shape of the balance response from ``GET /v1/billing/balance``. */
export interface BalanceSummary {
  readonly plan_code: string;
  readonly plan_name: string;
  readonly period_start: string;
  readonly period_end: string;
  readonly msg_limit: number | null;
  readonly used_msgs: number;
  readonly billable_msgs: number;
  readonly overage_msgs: number;
  readonly overage_cost_clp: number;
  readonly estimated_total_clp: number;
}

/**
 * A single (day, channel, count) bucket in the daily
 * usage chart response (``GET /v1/messages/daily``).
 *
 * The dashboard uses the value to drive the "gráfico de
 * barras" the PRD user story #8 asks for. Days the
 * customer did not send any messages are **not**
 * present in the response – the chart component fills
 * in the zeros so a sparse history does not inflate the
 * payload.
 */
export interface DailyUsageBucket {
  readonly day: string;
  readonly channel: string;
  readonly count: number;
}

/** The full ``GET /v1/messages/daily`` response. */
export interface DailyUsageResponse {
  readonly since: string;
  readonly until: string;
  readonly items: ReadonlyArray<DailyUsageBucket>;
}

/** Filters for the daily-usage endpoint. */
export interface DailyUsageFilters {
  channel?: MessageChannel | null;
  since?: string | null;
  until?: string | null;
}

/**
 * Lifecycle status of an :class:`Invoice` row, mirroring
 * the backend enum. The dashboard only displays the
 * values; the route layer is the only writer.
 */
export type InvoiceStatus =
  | 'draft'
  | 'issued'
  | 'paid'
  | 'overdue'
  | 'voided';

/** A single row of the invoice history table. */
export interface InvoiceRow {
  readonly id: string;
  readonly number: string;
  readonly plan_code: string;
  readonly period_start: string;
  readonly period_end: string;
  readonly issue_date: string;
  readonly due_date: string;
  readonly total_msgs: number;
  readonly included_msgs: number;
  readonly overage_msgs: number;
  readonly subtotal_clp: number;
  readonly iva_clp: number;
  readonly total_clp: number;
  readonly status: InvoiceStatus;
  readonly dte_number: number | null;
  readonly dte_url: string | null;
  readonly flow_invoice_id: string | null;
  readonly paid_at: string | null;
}
