import { HttpErrorResponse, HttpParams, provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { environment } from '../../../environments/environment';
import {
  AdminClientListResponse,
  AdminClientRow,
  AdminCreateClientResponse,
  AdminErrorLogResponse,
  AdminOverview,
  AdminProviderBreakdownRow,
  AdminUpdateClientRequest,
} from '../models/admin-dashboard.types';
import { AdminDashboardService } from './admin-dashboard.service';

/**
 * Helper that builds a fully-formed
 * :class:`AdminClientRow` with sensible defaults; the
 * tests only override the fields they care about.
 */
function makeRow(overrides: Partial<AdminClientRow> = {}): AdminClientRow {
  return {
    id: overrides.id ?? 'row-1',
    name: overrides.name ?? 'Acme SpA',
    email: overrides.email ?? 'ops@acme.cl',
    rut: overrides.rut ?? '12345678-5',
    plan: overrides.plan ?? 'starter',
    status: overrides.status ?? 'active',
    role: overrides.role ?? 'client',
    api_key_last4: overrides.api_key_last4 ?? '7a3f',
    markup_percent: overrides.markup_percent ?? 0.0,
    markup_fixed_clp: overrides.markup_fixed_clp ?? 0,
    created_at: overrides.created_at ?? '2026-06-15T10:00:00+00:00',
    updated_at: overrides.updated_at ?? null,
  };
}

/** Canned overview payload for the "happy path" tests. */
const SAMPLE_OVERVIEW: AdminOverview = {
  period_start: '2026-06-01',
  period_end: '2026-06-30',
  total_clients: 3,
  active_clients: 2,
  suspended_clients: 1,
  pending_clients: 0,
  admin_users: 1,
  total_messages: 100,
  billable_messages: 90,
  delivered_messages: 80,
  failed_messages: 5,
  pending_messages: 5,
  total_revenue_clp: 50000,
};

describe('AdminDashboardService', () => {
  let service: AdminDashboardService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        AdminDashboardService,
      ],
    });
    service = TestBed.inject(AdminDashboardService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    // Verify no request was left dangling: a forgotten
    // ``flush()`` is the most common cause of "test
    // passes locally, fails in CI" flakiness.
    http.verify();
  });

  describe('listClients', () => {
    it('hits /v1/admin/clients with no query string when no filters are set', () => {
      const response: AdminClientListResponse = {
        items: [makeRow()],
        total: 1,
        limit: 50,
        offset: 0,
        has_more: false,
      };
      let observed: AdminClientListResponse | undefined;
      service.listClients().subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/admin/clients`,
      );
      expect(req.request.method).toBe('GET');
      // ``HttpParams`` is empty when no filter is set.
      expect(req.request.params instanceof HttpParams).toBe(true);
      expect(req.request.params.keys().length).toBe(0);
      req.flush(response);
      expect(observed).toEqual(response);
    });

    it('passes every active filter as a query parameter', () => {
      const response: AdminClientListResponse = {
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
        has_more: false,
      };
      let observed: AdminClientListResponse | undefined;
      service
        .listClients({
          q: 'acme',
          plan: 'growth',
          status: 'suspended',
          limit: 25,
          offset: 50,
        })
        .subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/admin/clients`,
      );
      const params = req.request.params;
      expect(params.get('q')).toBe('acme');
      expect(params.get('plan')).toBe('growth');
      expect(params.get('status')).toBe('suspended');
      expect(params.get('limit')).toBe('25');
      expect(params.get('offset')).toBe('50');
      req.flush(response);
      expect(observed).toEqual(response);
    });

    it('drops empty / null filters from the query string', () => {
      service
        .listClients({
          q: null,
          plan: undefined,
          status: null,
        })
        .subscribe();
      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/admin/clients`,
      );
      expect(req.request.params.keys().length).toBe(0);
      req.flush({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
    });
  });

  describe('getClient', () => {
    it('hits /v1/admin/clients/{id} with the configured base URL', () => {
      const row = makeRow({ id: 'row-7' });
      let observed: AdminClientRow | undefined;
      service.getClient('row-7').subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) =>
          r.url === `${environment.apiBaseUrl}/v1/admin/clients/row-7`,
      );
      expect(req.request.method).toBe('GET');
      req.flush(row);
      expect(observed).toEqual(row);
    });
  });

  describe('createClient', () => {
    it('POSTs to /v1/admin/clients and returns the envelope with the plain API key', () => {
      const response: AdminCreateClientResponse = {
        client: makeRow(),
        api_key: 'mgw_live_abcdef1234567890',
        api_key_last4: '7890',
      };
      let observed: AdminCreateClientResponse | undefined;
      service
        .createClient({
          name: 'Acme SpA',
          email: 'ops@acme.cl',
          rut: '12345678-5',
          password: 'sup3r-secret',
          plan: 'starter',
        })
        .subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/admin/clients`,
      );
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({
        name: 'Acme SpA',
        email: 'ops@acme.cl',
        rut: '12345678-5',
        password: 'sup3r-secret',
        plan: 'starter',
      });
      req.flush(response);
      expect(observed).toEqual(response);
    });
  });

  describe('updateClient', () => {
    it('PATCHes /v1/admin/clients/{id} with the supplied body', () => {
      const payload: AdminUpdateClientRequest = {
        plan: 'enterprise',
        markup_percent: 0.25,
        markup_fixed_clp: 10,
      };
      const response = { client: makeRow({ plan: 'enterprise' }) };
      let observed: { client: AdminClientRow } | undefined;
      service.updateClient('row-1', payload).subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) =>
          r.url === `${environment.apiBaseUrl}/v1/admin/clients/row-1`,
      );
      expect(req.request.method).toBe('PATCH');
      expect(req.request.body).toEqual(payload);
      req.flush(response);
      expect(observed).toEqual(response);
    });
  });

  describe('suspendClient', () => {
    it('POSTs to /v1/admin/clients/{id}/suspend', () => {
      const response = {
        client: makeRow({ status: 'suspended' }),
        suspended_at: '2026-06-15T12:00:00+00:00',
      };
      let observed: typeof response | undefined;
      service.suspendClient('row-1').subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) =>
          r.url ===
          `${environment.apiBaseUrl}/v1/admin/clients/row-1/suspend`,
      );
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({});
      req.flush(response);
      expect(observed).toEqual(response);
    });
  });

  describe('getOverview', () => {
    it('hits /v1/admin/stats/overview', () => {
      let observed: AdminOverview | undefined;
      service.getOverview().subscribe((value) => (observed = value));

      const req = http.expectOne(
        `${environment.apiBaseUrl}/v1/admin/stats/overview`,
      );
      expect(req.request.method).toBe('GET');
      req.flush(SAMPLE_OVERVIEW);
      expect(observed).toEqual(SAMPLE_OVERVIEW);
    });
  });

  describe('getProviderBreakdown', () => {
    it('hits /v1/admin/stats/by-provider', () => {
      const rows: AdminProviderBreakdownRow[] = [
        {
          provider: 'meta_whatsapp',
          channel: 'whatsapp',
          total: 10,
          delivered: 8,
          failed: 1,
          pending: 1,
          cost_clp: 800,
          fee_clp: 50,
        },
      ];
      let observed: ReadonlyArray<AdminProviderBreakdownRow> | undefined;
      service.getProviderBreakdown().subscribe((value) => (observed = value));

      const req = http.expectOne(
        `${environment.apiBaseUrl}/v1/admin/stats/by-provider`,
      );
      expect(req.request.method).toBe('GET');
      req.flush(rows);
      expect(observed).toEqual(rows);
    });
  });

  describe('listErrorLog', () => {
    it('hits /v1/admin/logs with the supplied pagination knobs', () => {
      const response: AdminErrorLogResponse = {
        items: [
          {
            message_id: 'msg-1',
            client_id: 'row-1',
            client_name: 'Acme SpA',
            client_email: 'ops@acme.cl',
            channel: 'whatsapp',
            to_number: '+56912345678',
            provider: 'meta_whatsapp',
            error_code: 'rate_limited',
            error_message: '429 Too Many Requests',
            created_at: '2026-06-15T10:00:00+00:00',
          },
        ],
        total: 1,
        limit: 25,
        offset: 50,
        has_more: false,
      };
      let observed: AdminErrorLogResponse | undefined;
      service.listErrorLog(25, 50).subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/admin/logs`,
      );
      expect(req.request.method).toBe('GET');
      expect(req.request.params.get('limit')).toBe('25');
      expect(req.request.params.get('offset')).toBe('50');
      req.flush(response);
      expect(observed).toEqual(response);
    });

    it('omits the pagination knobs when defaults are used', () => {
      service.listErrorLog().subscribe();
      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/admin/logs`,
      );
      // The service always emits the defaults so the
      // backend's pagination math is consistent across
      // calls; the test pins the contract so a future
      // refactor that drops the query string does not
      // regress.
      expect(req.request.params.get('limit')).toBe('50');
      expect(req.request.params.get('offset')).toBe('0');
      req.flush({
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
        has_more: false,
      });
    });
  });

  describe('describeError', () => {
    it('returns null for a non-HttpErrorResponse', () => {
      expect(service.describeError(new Error('boom'))).toBeNull();
    });

    it('returns the string detail when the backend responds with a plain string', () => {
      const error = new HttpErrorResponse({
        status: 401,
        statusText: 'Unauthorized',
        error: { detail: 'missing api key' },
      });
      expect(service.describeError(error)).toBe('missing api key');
    });

    it('returns ``code: message`` for the platform wire format', () => {
      const error = new HttpErrorResponse({
        status: 403,
        statusText: 'Forbidden',
        error: { detail: { code: 'admin_required', message: 'no admin' } },
      });
      expect(service.describeError(error)).toBe('admin_required: no admin');
    });

    it('returns the message even when the code is missing', () => {
      const error = new HttpErrorResponse({
        status: 422,
        statusText: 'Unprocessable Entity',
        error: { detail: { message: 'invalid email' } },
      });
      expect(service.describeError(error)).toBe('invalid email');
    });

    it('returns the first validation error message for Pydantic payloads', () => {
      const error = new HttpErrorResponse({
        status: 422,
        statusText: 'Unprocessable Entity',
        error: { detail: [{ msg: 'value is not a valid email address' }] },
      });
      expect(service.describeError(error)).toBe(
        'value is not a valid email address',
      );
    });
  });
});
