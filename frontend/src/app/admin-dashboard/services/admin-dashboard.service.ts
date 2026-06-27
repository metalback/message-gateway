import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable } from '@angular/core';
import { Observable } from 'rxjs';

import { environment } from '../../../environments/environment';
import {
  AdminClientListFilters,
  AdminClientListResponse,
  AdminClientRow,
  AdminClientSuspendResponse,
  AdminClientUpdateResponse,
  AdminCreateClientRequest,
  AdminCreateClientResponse,
  AdminErrorLogResponse,
  AdminOverview,
  AdminProviderBreakdownRow,
  AdminUpdateClientRequest,
} from '../models/admin-dashboard.types';

/**
 * Service that backs the "Admin · Gestión de clientes y
 * métricas" dashboard (issue #10).
 *
 * The service is the single point of contact between the
 * Angular component tree and the ``/v1/admin/*`` FastAPI
 * surface; the components never call :class:`HttpClient`
 * directly. Two reasons for the indirection:
 *
 * 1. The API base URL is centralised here so a future move
 *    (e.g. to a micro-frontend shell) is a one-line change.
 * 2. The dashboard never needs the raw HTTP response: the
 *    components consume :class:`Observable<T>` of typed
 *    domain objects, never
 *    :class:`Observable<HttpResponse<T>>`. Centralising
 *    the conversion keeps the type noise out of the
 *    templates.
 */
@Injectable({ providedIn: 'root' })
export class AdminDashboardService {
  constructor(private readonly http: HttpClient) {}

  /**
   * Fetch one page of the admin clients table.
   *
   * ``filters`` is the dashboard's filter-form state
   * (``q`` / ``plan`` / ``status``) plus the ``limit`` /
   * ``offset`` pagination arguments. Empty strings and
   * ``null`` filters are dropped from the query string
   * so the backend does not have to special case them.
   */
  listClients(
    filters: AdminClientListFilters = {},
  ): Observable<AdminClientListResponse> {
    const url = `${environment.apiBaseUrl}/v1/admin/clients`;
    let params = new HttpParams();
    if (filters.q) {
      params = params.set('q', filters.q);
    }
    if (filters.plan) {
      params = params.set('plan', filters.plan);
    }
    if (filters.status) {
      params = params.set('status', filters.status);
    }
    if (typeof filters.limit === 'number' && filters.limit > 0) {
      params = params.set('limit', String(filters.limit));
    }
    if (typeof filters.offset === 'number' && filters.offset >= 0) {
      params = params.set('offset', String(filters.offset));
    }
    return this.http.get<AdminClientListResponse>(url, { params });
  }

  /**
   * Fetch a single client by id.
   *
   * The endpoint is the source of truth for the
   * "ver detalle" drawer on the dashboard; the route
   * returns a 404 with a stable error code when the id
   * is unknown so the component can render an
   * inline error without re-deriving the contract.
   */
  getClient(clientId: string): Observable<AdminClientRow> {
    const url = `${environment.apiBaseUrl}/v1/admin/clients/${clientId}`;
    return this.http.get<AdminClientRow>(url);
  }

  /**
   * Create a new client on behalf of the operator.
   *
   * The plain API key is returned **once** in the
   * response, mirroring the public
   * ``POST /v1/auth/register`` contract. The operator is
   * expected to surface the value to the new customer's
   * onboarding flow before navigating away; the platform
   * never stores the clear-text key.
   */
  createClient(
    payload: AdminCreateClientRequest,
  ): Observable<AdminCreateClientResponse> {
    const url = `${environment.apiBaseUrl}/v1/admin/clients`;
    return this.http.post<AdminCreateClientResponse>(url, payload);
  }

  /**
   * PATCH-style update of a single client.
   *
   * The body carries the optional ``name`` / ``plan`` /
   * ``status`` / ``markup_percent`` / ``markup_fixed_clp``
   * fields; passing ``undefined`` for a field is a
   * no-op for that field. The service uses an explicit
   * ``is not None`` check, so a falsy but valid value
   * (``markup_percent=0.0``, ``markup_fixed_clp=0``) is
   * accepted.
   */
  updateClient(
    clientId: string,
    payload: AdminUpdateClientRequest,
  ): Observable<AdminClientUpdateResponse> {
    const url = `${environment.apiBaseUrl}/v1/admin/clients/${clientId}`;
    return this.http.patch<AdminClientUpdateResponse>(url, payload);
  }

  /**
   * Suspend a client (idempotent).
   *
   * The endpoint flips the row to ``status=suspended``;
   * a second call on an already-suspended client is a
   * 200 with no observable change. Re-activating a
   * client is a regular
   * :func:`updateClient` call with
   * ``status="active"``.
   */
  suspendClient(clientId: string): Observable<AdminClientSuspendResponse> {
    const url = `${environment.apiBaseUrl}/v1/admin/clients/${clientId}/suspend`;
    return this.http.post<AdminClientSuspendResponse>(url, {});
  }

  /**
   * Fetch the aggregate counters for the admin overview card.
   *
   * The endpoint is the source of truth for the
   * "métricas agregadas" widget; the dashboard hits it
   * on first paint to render the headline tiles.
   */
  getOverview(): Observable<AdminOverview> {
    const url = `${environment.apiBaseUrl}/v1/admin/stats/overview`;
    return this.http.get<AdminOverview>(url);
  }

  /**
   * Fetch the per-provider breakdown for the
   * "desglose por proveedor" card.
   *
   * The endpoint returns one row per ``(provider, channel)``
   * pair, ordered by ``total`` descending so the
   * dashboard's largest bar is the first row.
   */
  getProviderBreakdown(): Observable<ReadonlyArray<AdminProviderBreakdownRow>> {
    const url = `${environment.apiBaseUrl}/v1/admin/stats/by-provider`;
    return this.http.get<ReadonlyArray<AdminProviderBreakdownRow>>(url);
  }

  /**
   * Fetch the most recent failed messages for the admin
   * error log.
   *
   * ``limit`` / ``offset`` mirror the platform-wide
   * pagination convention; the function returns the
   * full envelope so the component can render
   * "showing 1-50 of 247" without re-counting on the
   * client.
   */
  listErrorLog(
    limit = 50,
    offset = 0,
  ): Observable<AdminErrorLogResponse> {
    const url = `${environment.apiBaseUrl}/v1/admin/logs`;
    let params = new HttpParams();
    if (limit > 0) {
      params = params.set('limit', String(limit));
    }
    if (offset >= 0) {
      params = params.set('offset', String(offset));
    }
    return this.http.get<AdminErrorLogResponse>(url, { params });
  }

  /**
   * Wrap an :class:`HttpErrorResponse` into a stable string
   * the component can render verbatim. The platform's wire
   * format is ``{"detail": {"code": "...", "message": "..."}}``
   * for domain errors and ``{"detail": [...]}`` for
   * Pydantic validation errors; we normalise both into a
   * single string so the template does not have to branch.
   *
   * Returns ``null`` when the error is not a domain error
   * (e.g. a transport failure) so the caller can decide
   * whether to retry.
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
