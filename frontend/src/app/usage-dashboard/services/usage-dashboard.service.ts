import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { environment } from '../../../environments/environment';
import {
  BalanceSummary,
  DailyUsageBucket,
  DailyUsageFilters,
  DailyUsageResponse,
  InvoiceRow,
  InvoiceStatus,
  MessageHistoryFilters,
  MessageListResponse,
  MessageRow,
} from '../models/usage-dashboard.types';

/**
 * Service that backs the "Historial y consumo" dashboard.
 *
 * The service is the single point of contact between the
 * Angular component tree and the FastAPI surface; the
 * components never call :class:`HttpClient` directly. Two
 * reasons for the indirection:
 *
 * 1. The API base URL is centralised here so a future
 *    move (e.g. to a micro-frontend shell) is a one-line
 *    change.
 * 2. The dashboard never needs the raw HTTP response:
 *    the components consume :class:`Observable<T>` of
 *    typed domain objects, never
 *    :class:`Observable<HttpResponse<T>>`. Centralising
 *    the conversion keeps the type noise out of the
 *    templates.
 */
@Injectable({ providedIn: 'root' })
export class UsageDashboardService {
  constructor(private readonly http: HttpClient) {}

  /**
   * Fetch the headline consumption counters for the
   * current billing period. The endpoint is the same one
   * the billing / invoice screens use; the dashboard
   * hits it on first paint to show "used 1,234 of 1,000
   * messages" in the hero card.
   */
  getBalance(): Observable<BalanceSummary> {
    const url = `${environment.apiBaseUrl}/v1/billing/balance`;
    return this.http.get<BalanceSummary>(url);
  }

  /**
   * Fetch one page of the customer's message history.
   *
   * ``filters`` is the dashboard's filter-form state
   * (channel / status / date range) plus the
   * ``limit`` / ``offset`` pagination arguments. Empty
   * strings and ``null`` filters are dropped from the
   * query string so the backend does not have to
   * special case them.
   */
  listMessages(filters: MessageHistoryFilters = {}): Observable<MessageListResponse> {
    const url = `${environment.apiBaseUrl}/v1/messages`;
    let params = new HttpParams();
    if (filters.channel) {
      params = params.set('channel', filters.channel);
    }
    if (filters.status) {
      params = params.set('status', filters.status);
    }
    if (filters.since) {
      params = params.set('since', filters.since);
    }
    if (filters.until) {
      params = params.set('until', filters.until);
    }
    if (typeof filters.limit === 'number' && filters.limit > 0) {
      params = params.set('limit', String(filters.limit));
    }
    if (typeof filters.offset === 'number' && filters.offset >= 0) {
      params = params.set('offset', String(filters.offset));
    }
    return this.http.get<MessageListResponse>(url, { params });
  }

  /**
   * Fetch the per-day, per-channel message counts that
   * drive the dashboard's "gráfico de barras" (issue
   * #6 acceptance criterion: "gráfico de uso diario se
   * renderiza correctamente").
   *
   * The endpoint is the source of truth for the default
   * 31-day window: a request with no query parameters
   * returns the trailing month, and the response
   * carries the resolved ``since`` / ``until`` so the
   * chart axis can be drawn without mirroring the
   * default-window logic on the client.
   */
  getDailyUsage(filters: DailyUsageFilters = {}): Observable<DailyUsageResponse> {
    const url = `${environment.apiBaseUrl}/v1/messages/daily`;
    let params = new HttpParams();
    if (filters.channel) {
      params = params.set('channel', filters.channel);
    }
    if (filters.since) {
      params = params.set('since', filters.since);
    }
    if (filters.until) {
      params = params.set('until', filters.until);
    }
    return this.http.get<DailyUsageResponse>(url, { params });
  }

  /**
   * Fetch the customer's invoice history, newest first.
   *
   * The endpoint is the source of truth the dashboard
   * renders in the "Historial de facturación" section
   * (issue #6 acceptance criterion: "lista de facturas
   * se muestra en frontend"). The list is a flat
   * array, no pagination: an MVP customer has a handful
   * of invoices at most and a paged read would only
   * complicate the table.
   */
  listInvoices(): Observable<ReadonlyArray<InvoiceRow>> {
    const url = `${environment.apiBaseUrl}/v1/billing/invoices`;
    return this.http.get<ReadonlyArray<InvoiceRow>>(url);
  }

  /**
   * Build the URL for the CSV export endpoint with the
   * same filters the on-screen list uses.
   *
   * The function is the public surface the component
   * talks to; the implementation appends every active
   * filter as a query string parameter so a download
   * triggered from a filtered view (``status=failed``,
   * ``channel=whatsapp`` …) returns exactly the rows
   * the user can see on the table.
   *
   * The pagination knobs (``limit`` / ``offset``) are
   * intentionally omitted: the export returns the full
   * result set in one go.
   */
  buildExportUrl(filters: MessageHistoryFilters = {}): string {
    const base = `${environment.apiBaseUrl}/v1/messages/export`;
    let params = new HttpParams();
    if (filters.channel) {
      params = params.set('channel', filters.channel);
    }
    if (filters.status) {
      params = params.set('status', filters.status);
    }
    if (filters.since) {
      params = params.set('since', filters.since);
    }
    if (filters.until) {
      params = params.set('until', filters.until);
    }
    const query = params.toString();
    return query ? `${base}?${query}` : base;
  }

  /**
   * Download the customer's history as a CSV file.
   *
   * The method goes through :class:`HttpClient` (so any
   * auth interceptor the rest of the dashboard uses
   * also attaches the ``X-API-Key`` header) and turns
   * the resulting blob into a browser-driven file
   * download. The function returns the
   * :class:`Observable` the component subscribes to;
   * it is the caller's responsibility to surface a
   * success / error toast on completion.
   *
   * The HTTP request is the single source of truth for
   * the API contract: a future iteration that adds
   * progress reporting or a "stream the response
   * incrementally" optimisation can land here without
   * the component having to know.
   */
  downloadMessagesCsv(filters: MessageHistoryFilters = {}): Observable<Blob> {
    const url = this.buildExportUrl(filters);
    return this.http.get(url, { responseType: 'blob' });
  }

  /**
   * Turn a CSV blob into a browser-driven file download.
   *
   * The helper exists so the component can call it on
   * any :class:`Blob` (not just the one
   * :func:`downloadMessagesCsv` returns): a future test
   * that synthesises a canned CSV blob can exercise the
   * same code path without having to stand up a fake
   * HTTP backend.
   *
   * ``filename`` defaults to ``'mensajes.csv'`` to
   * match the ``Content-Disposition`` filename the
   * backend emits (``mensajes-YYYY-MM-DD.csv``); the
   * browser usually overrides the hint with the server
   * value, but the default keeps the helper useful in
   * tests / standalone use.
   */
  saveBlobAsDownload(blob: Blob, filename = 'mensajes.csv'): void {
    if (typeof document === 'undefined' || typeof window === 'undefined') {
      // Server-side rendering or a non-browser test
      // environment: there is no way to trigger a
      // download, so the call is a no-op. Production
      // always runs in a browser.
      return;
    }
    const objectUrl = window.URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.rel = 'noopener';
    anchor.style.display = 'none';
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    // Revoke the object URL once the click has been
    // dispatched so the browser does not leak the
    // blob's underlying memory.
    window.URL.revokeObjectURL(objectUrl);
  }

  /**
   * Project a :class:`MessageRow` into a
   * human-readable destination label. The raw
   * ``+56...`` value is preserved (and used as the
   * row's ``title`` in the template); the humanised
   * form is what the table cell actually shows.
   *
   * Kept as a pure helper on the service (rather than
   * on the component) so future views – for example a
   * "send a message" wizard – can reuse the same label
   * without having to copy the function.
   */
  formatDestination(row: MessageRow): string {
    if (!row.to_number) {
      return '—';
    }
    // Truncate long values so the table cell stays
    // narrow; the full value is in the row's ``title``
    // attribute so a hover reveals it.
    return row.to_number.length > 14
      ? `${row.to_number.slice(0, 12)}…`
      : row.to_number;
  }

  /**
   * Project a status enum into a Spanish label
   * suitable for the dashboard's "Estado" column. The
   * mapping is intentionally narrow: the platform
   * never mints a status value outside the six
   * documented in :class:`MessageStatus`, so an
   * unknown value falls back to the raw string (the
   * dashboard renders the value verbatim rather than
   * crashing on a typo).
   */
  formatStatus(status: MessageRow['status']): string {
    const labels: Record<MessageRow['status'], string> = {
      pending: 'Pendiente',
      queued: 'En cola',
      sent: 'Enviado',
      delivered: 'Entregado',
      failed: 'Fallido',
      unknown: 'Desconocido',
    };
    return labels[status] ?? status;
  }

  /**
   * Same idea as :func:`formatStatus` but for the
   * delivery channel. The mapping lives on the
   * service for symmetry.
   */
  formatChannel(channel: MessageRow['channel']): string {
    const labels: Record<MessageRow['channel'], string> = {
      sms: 'SMS',
      whatsapp: 'WhatsApp',
    };
    return labels[channel] ?? channel;
  }

  /**
   * Aggregate the per-day buckets into a ``{day → total}``
   * shape the chart can render. Days with no traffic are
   * not present in the response; the helper does not
   * zero-fill them (the chart component does that, so a
   * sparse history does not pay the cost twice).
   *
   * The function is a pure aggregator kept on the
   * service so a future view (e.g. an admin console)
   * can reuse the same logic without copy-paste.
   */
  dailyTotals(
    buckets: ReadonlyArray<DailyUsageBucket>,
  ): ReadonlyArray<{ readonly day: string; readonly count: number }> {
    const totals = new Map<string, number>();
    for (const bucket of buckets) {
      totals.set(bucket.day, (totals.get(bucket.day) ?? 0) + bucket.count);
    }
    return Array.from(totals.entries())
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([day, count]) => ({ day, count }));
  }

  /**
   * Project an :class:`InvoiceStatus` value into the
   * Spanish label the dashboard's invoice table shows.
   * The mapping mirrors :func:`formatStatus` – any
   * unrecognised value falls back to the raw string so
   * a future enum member is rendered verbatim rather
   * than crashing the template.
   */
  formatInvoiceStatus(status: InvoiceStatus): string {
    const labels: Record<InvoiceStatus, string> = {
      draft: 'Borrador',
      issued: 'Emitida',
      paid: 'Pagada',
      overdue: 'Vencida',
      voided: 'Anulada',
    };
    return labels[status] ?? status;
  }

  /**
   * Tailwind-friendly badge colour for an invoice
   * status. Lives on the service so the class strings
   * are co-located with the labels – a future status
   * only needs to be added in one place.
   */
  invoiceStatusBadgeClass(status: InvoiceStatus): string {
    switch (status) {
      case 'paid':
        return 'bg-emerald-100 text-emerald-800';
      case 'issued':
        return 'bg-sky-100 text-sky-800';
      case 'overdue':
        return 'bg-rose-100 text-rose-800';
      case 'voided':
        return 'bg-slate-100 text-slate-700';
      case 'draft':
      default:
        return 'bg-amber-100 text-amber-800';
    }
  }

  /**
   * Wrap an :class:`HttpErrorResponse` into a stable
   * string the component can render verbatim. The
   * platform's wire format is ``{"detail": {"code":
   * "...", "message": "..."}}`` for domain errors and
   * ``{"detail": [...]}`` for Pydantic validation
   * errors; we normalise both into a single string so
   * the template does not have to branch.
   *
   * Returns ``null`` when the error is not a domain
   * error (e.g. a transport failure) so the caller can
   * decide whether to retry.
   */
  describeError(error: unknown): string | null {
    if (!(error instanceof HttpErrorResponse)) {
      return null;
    }
    const detail = (error.error as { detail?: unknown } | null)?.detail;
    if (typeof detail === 'string') {
      return detail;
    }
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      const code = typeof (detail as { code?: unknown }).code === 'string'
        ? (detail as { code: string }).code
        : null;
      const message = typeof (detail as { message?: unknown }).message === 'string'
        ? (detail as { message: string }).message
        : null;
      if (code && message) {
        return `${code}: ${message}`;
      }
      return message ?? code;
    }
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first.msg === 'string') {
        return first.msg;
      }
    }
    return error.message || null;
  }
}
