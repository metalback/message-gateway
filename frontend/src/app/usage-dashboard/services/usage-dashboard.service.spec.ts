import { HttpErrorResponse, HttpParams, provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { environment } from '../../../environments/environment';
import {
  BalanceSummary,
  DailyUsageBucket,
  DailyUsageResponse,
  InvoiceRow,
  MessageListResponse,
  MessageRow,
  StatusSummaryResponse,
} from '../models/usage-dashboard.types';
import { UsageDashboardService } from './usage-dashboard.service';

/**
 * Helper that builds a fully-formed :class:`MessageRow`
 * with sensible defaults; the tests only override the
 * fields they care about.
 */
function makeRow(overrides: Partial<MessageRow> = {}): MessageRow {
  return {
    id: overrides.id ?? 'row-1',
    channel: overrides.channel ?? 'sms',
    status: overrides.status ?? 'sent',
    to_number: overrides.to_number ?? '+56912345678',
    body: overrides.body ?? 'hola',
    provider: overrides.provider ?? 'sms_aggregator',
    provider_msg_id: overrides.provider_msg_id ?? 'p-1',
    error_code: overrides.error_code ?? null,
    error_message: overrides.error_message ?? null,
    cost_clp: overrides.cost_clp ?? 25,
    fee_clp: overrides.fee_clp ?? 5,
    created_at: overrides.created_at ?? '2026-06-15T10:00:00+00:00',
  };
}

/** A canned balance payload for the "happy path" tests. */
const SAMPLE_BALANCE: BalanceSummary = {
  plan_code: 'starter',
  plan_name: 'Starter',
  period_start: '2026-06-01',
  period_end: '2026-06-30',
  msg_limit: 1000,
  used_msgs: 250,
  billable_msgs: 240,
  overage_msgs: 0,
  overage_cost_clp: 0,
  estimated_total_clp: 23788,
};

/** A canned status-summary payload for the breakdown card tests. */
const SAMPLE_STATUS_SUMMARY: StatusSummaryResponse = {
  since: '2026-05-16T00:00:00+00:00',
  until: '2026-06-15T00:00:00+00:00',
  items: [
    { status: 'delivered', count: 7 },
    { status: 'sent', count: 2 },
    { status: 'queued', count: 0 },
    { status: 'pending', count: 1 },
    { status: 'failed', count: 1 },
    { status: 'unknown', count: 0 },
  ],
  total: 11,
  delivered: 7,
  failed: 1,
  pending: 1,
  cost_clp: 275,
  fee_clp: 35,
  delivery_rate: 7 / 11,
};

describe('UsageDashboardService', () => {
  let service: UsageDashboardService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        UsageDashboardService,
      ],
    });
    service = TestBed.inject(UsageDashboardService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    // Verify no request was left dangling: a forgotten
    // ``flush()`` is the most common cause of "test
    // passes locally, fails in CI" flakiness.
    http.verify();
  });

  describe('getBalance', () => {
    it('hits the /v1/billing/balance endpoint with the configured base URL', () => {
      let observed: BalanceSummary | undefined;
      service.getBalance().subscribe((value) => (observed = value));

      const req = http.expectOne(`${environment.apiBaseUrl}/v1/billing/balance`);
      expect(req.request.method).toBe('GET');
      req.flush(SAMPLE_BALANCE);

      expect(observed).toEqual(SAMPLE_BALANCE);
    });
  });

  describe('listMessages', () => {
    it('hits /v1/messages with no query string when no filters are set', () => {
      const response: MessageListResponse = {
        items: [makeRow()],
        total: 1,
        limit: 50,
        offset: 0,
        has_more: false,
      };
      let observed: MessageListResponse | undefined;
      service.listMessages().subscribe((value) => (observed = value));

      const req = http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`);
      expect(req.request.method).toBe('GET');
      // ``HttpParams`` is empty when no filter is set.
      expect(req.request.params instanceof HttpParams).toBe(true);
      expect(req.request.params.keys().length).toBe(0);
      req.flush(response);

      expect(observed).toEqual(response);
    });

    it('passes every active filter as a query parameter', () => {
      const response: MessageListResponse = {
        items: [],
        total: 0,
        limit: 50,
        offset: 0,
        has_more: false,
      };
      let observed: MessageListResponse | undefined;
      service
        .listMessages({
          channel: 'whatsapp',
          status: 'failed',
          since: '2026-06-01T00:00:00+00:00',
          until: '2026-06-30T23:59:59+00:00',
          limit: 25,
          offset: 50,
        })
        .subscribe((value) => (observed = value));

      const req = http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`);
      const params = req.request.params;
      expect(params.get('channel')).toBe('whatsapp');
      expect(params.get('status')).toBe('failed');
      expect(params.get('since')).toBe('2026-06-01T00:00:00+00:00');
      expect(params.get('until')).toBe('2026-06-30T23:59:59+00:00');
      expect(params.get('limit')).toBe('25');
      expect(params.get('offset')).toBe('50');
      req.flush(response);
      expect(observed).toEqual(response);
    });

    it('drops empty / null filters from the query string', () => {
      service
        .listMessages({
          channel: null,
          status: undefined,
          since: '',
          until: null,
        })
        .subscribe();

      const req = http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`);
      expect(req.request.params.keys().length).toBe(0);
      req.flush({ items: [], total: 0, limit: 50, offset: 0, has_more: false });
    });
  });

  describe('buildExportUrl / downloadMessagesCsv', () => {
    it('builds the export URL with no query string when no filters are set', () => {
      const url = service.buildExportUrl();
      expect(url).toBe(`${environment.apiBaseUrl}/v1/messages/export`);
    });

    it('builds the export URL with every active filter as a query parameter', () => {
      const url = service.buildExportUrl({
        channel: 'whatsapp',
        status: 'failed',
        since: '2026-06-01T00:00:00+00:00',
        until: '2026-06-30T23:59:59+00:00',
      });
      // ``URLSearchParams`` sorts the keys, so the order
      // here is deterministic and the assertion does not
      // depend on the implementation detail of the
      // ``HttpParams`` iteration order.
      const params = new URLSearchParams(url.split('?')[1] ?? '');
      expect(params.get('channel')).toBe('whatsapp');
      expect(params.get('status')).toBe('failed');
      expect(params.get('since')).toBe('2026-06-01T00:00:00+00:00');
      expect(params.get('until')).toBe('2026-06-30T23:59:59+00:00');
    });

    it('omits the limit and offset pagination knobs', () => {
      // The export returns the full result set in one
      // shot, so the query string must not carry the
      // ``limit`` / ``offset`` the list endpoint uses.
      const url = service.buildExportUrl({
        channel: 'sms',
        limit: 50,
        offset: 100,
      });
      const params = new URLSearchParams(url.split('?')[1] ?? '');
      expect(params.has('limit')).toBe(false);
      expect(params.has('offset')).toBe(false);
      expect(params.get('channel')).toBe('sms');
    });

    it('downloads the CSV as a blob through HttpClient', () => {
      let observed: Blob | undefined;
      service.downloadMessagesCsv({ channel: 'sms' }).subscribe((value) => (observed = value));

      const req = http.expectOne((r) => {
        if (r.url !== `${environment.apiBaseUrl}/v1/messages/export`) {
          return false;
        }
        return r.params.get('channel') === 'sms';
      });
      expect(req.request.method).toBe('GET');
      // The body is a ``Blob`` because the response type
      // is binary; a JSON parser would not be able to
      // consume the CSV the backend emits.
      expect(req.request.responseType).toBe('blob');
      const payload = new Blob(['id,channel\nrow-1,sms\n'], { type: 'text/csv' });
      req.flush(payload);
      expect(observed).toBe(payload);
    });
  });

  describe('saveBlobAsDownload', () => {
    // ``saveBlobAsDownload`` is a thin wrapper around the
    // browser's ``URL.createObjectURL`` + anchor-click
    // pattern. The tests assert the DOM mutations are
    // made against a clean slate: a fresh anchor is
    // appended, clicked, and removed in a single tick so
    // the dashboard never leaks DOM nodes.

    it('triggers a click on a synthetic anchor and removes it from the DOM', () => {
      const blob = new Blob(['hello'], { type: 'text/csv' });
      let createdUrl: string | null = null;
      let revokedUrl: string | null = null;
      // The ``URL.createObjectURL`` and
      // ``URL.revokeObjectURL`` calls hit the jsdom
      // stubs by default; intercept them so the test
      // can assert the helper called the platform
      // APIs in the right order.
      const createSpy = spyOn(URL, 'createObjectURL').and.callFake((value: unknown) => {
        createdUrl = `blob:test/${(value as Blob).size}`;
        return createdUrl;
      });
      const revokeSpy = spyOn(URL, 'revokeObjectURL').and.callFake((value: string) => {
        revokedUrl = value;
      });
      const anchor = document.createElement('a');
      let appendedNode: Node | null = null;
      const appendSpy = spyOn(document.body, 'appendChild').and.callFake(<T extends Node>(node: T) => {
        // Capture the appended node so the test can
        // assert against its ``download`` attribute
        // (the anchor is the actual element the helper
        // builds). Returning the node itself keeps the
        // helper's contract intact.
        appendedNode = node;
        return node;
      });
      const removeSpy = spyOn(document.body, 'removeChild').and.callFake(<T extends Node>(node: T) => node);
      const clickSpy = spyOn(anchor, 'click').and.callThrough();

      service.saveBlobAsDownload(blob, 'mensajes-2026-06.csv');

      expect(createSpy).toHaveBeenCalledWith(blob);
      expect(appendSpy).toHaveBeenCalled();
      expect(clickSpy).toHaveBeenCalled();
      expect(removeSpy).toHaveBeenCalled();
      expect((appendedNode as HTMLAnchorElement | null)?.download).toBe('mensajes-2026-06.csv');
      expect(revokeSpy).toHaveBeenCalledWith(createdUrl!);
      expect(revokedUrl).toBe(createdUrl);
    });

    it('falls back to a default filename when none is provided', () => {
      const blob = new Blob([''], { type: 'text/csv' });
      const captured: { value: HTMLAnchorElement | null } = { value: null };
      spyOn(URL, 'createObjectURL').and.returnValue('blob:test/default');
      spyOn(URL, 'revokeObjectURL');
      spyOn(document.body, 'appendChild').and.callFake(<T extends Node>(node: T) => {
        captured.value = node as unknown as HTMLAnchorElement;
        return node;
      });
      spyOn(document.body, 'removeChild').and.callFake(<T extends Node>(node: T) => node);

      service.saveBlobAsDownload(blob);

      expect(captured.value?.download).toBe('mensajes.csv');
    });
  });

  describe('formatDestination', () => {
    it('returns a dash when the destination is empty', () => {
      expect(service.formatDestination(makeRow({ to_number: '' }))).toBe('—');
    });

    it('preserves short destinations verbatim', () => {
      expect(service.formatDestination(makeRow({ to_number: '+56912345678' }))).toBe(
        '+56912345678',
      );
    });

    it('truncates long destinations with an ellipsis', () => {
      const long = '+569123456789012345';
      const formatted = service.formatDestination(makeRow({ to_number: long }));
      expect(formatted.endsWith('…')).toBe(true);
      expect(formatted.length).toBeLessThan(long.length);
    });
  });

  describe('formatStatus / formatChannel', () => {
    it('projects every known status into a Spanish label', () => {
      const cases: Array<[MessageRow['status'], string]> = [
        ['pending', 'Pendiente'],
        ['queued', 'En cola'],
        ['sent', 'Enviado'],
        ['delivered', 'Entregado'],
        ['failed', 'Fallido'],
        ['unknown', 'Desconocido'],
      ];
      for (const [status, label] of cases) {
        expect(service.formatStatus(status)).toBe(label);
      }
    });

    it('falls back to the raw value for an unknown status', () => {
      // Cast through ``unknown`` to bypass the type
      // system; the service contract guarantees the
      // function tolerates unrecognised values.
      const rogue = 'teleported' as unknown as MessageRow['status'];
      expect(service.formatStatus(rogue)).toBe('teleported');
    });

    it('projects the two known channels into short labels', () => {
      expect(service.formatChannel('sms')).toBe('SMS');
      expect(service.formatChannel('whatsapp')).toBe('WhatsApp');
    });
  });

  describe('describeError', () => {
    it('returns null for non-HTTP errors', () => {
      expect(service.describeError(new Error('boom'))).toBeNull();
      expect(service.describeError('plain string')).toBeNull();
      expect(service.describeError(null)).toBeNull();
    });

    it('formats a domain error payload as "code: message"', () => {
      const error = new HttpErrorResponse({
        status: 422,
        statusText: 'Unprocessable Entity',
        error: { detail: { code: 'invalid_channel', message: 'unknown channel' } },
      });
      expect(service.describeError(error)).toBe('invalid_channel: unknown channel');
    });

    it('falls back to the message when the code is missing', () => {
      const error = new HttpErrorResponse({
        status: 502,
        statusText: 'Bad Gateway',
        error: { detail: { message: 'upstream down' } },
      });
      expect(service.describeError(error)).toBe('upstream down');
    });

    it('flattens a Pydantic-style detail array to the first msg', () => {
      const error = new HttpErrorResponse({
        status: 422,
        statusText: 'Unprocessable Entity',
        error: {
          detail: [
            { type: 'enum', loc: ['query', 'channel'], msg: "Input should be 'sms'" },
          ],
        },
      });
      expect(service.describeError(error)).toBe("Input should be 'sms'");
    });

    it('falls back to the HTTP status text when the body is empty', () => {
      const error = new HttpErrorResponse({
        status: 500,
        statusText: 'Internal Server Error',
        error: null,
      });
      // ``HttpErrorResponse`` exposes the status text on
      // ``message``; the helper should pick it up rather
      // than returning ``null``.
      expect(service.describeError(error)).toContain('Internal Server Error');
    });
  });

  describe('getDailyUsage', () => {
    it('hits /v1/messages/daily with no query string when no filters are set', () => {
      const response: DailyUsageResponse = {
        since: '2026-05-15T00:00:00+00:00',
        until: '2026-06-15T00:00:00+00:00',
        items: [],
      };
      let observed: DailyUsageResponse | undefined;
      service.getDailyUsage().subscribe((value) => (observed = value));

      const req = http.expectOne(`${environment.apiBaseUrl}/v1/messages/daily`);
      expect(req.request.method).toBe('GET');
      expect(req.request.params.keys().length).toBe(0);
      req.flush(response);
      expect(observed).toEqual(response);
    });

    it('passes the active filter as a query parameter', () => {
      const response: DailyUsageResponse = {
        since: '2026-06-01T00:00:00+00:00',
        until: '2026-06-30T23:59:59+00:00',
        items: [],
      };
      let observed: DailyUsageResponse | undefined;
      service
        .getDailyUsage({
          channel: 'whatsapp',
          since: '2026-06-01T00:00:00+00:00',
          until: '2026-06-30T23:59:59+00:00',
        })
        .subscribe((value) => (observed = value));

      const req = http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`);
      const params = req.request.params;
      expect(params.get('channel')).toBe('whatsapp');
      expect(params.get('since')).toBe('2026-06-01T00:00:00+00:00');
      expect(params.get('until')).toBe('2026-06-30T23:59:59+00:00');
      req.flush(response);
      expect(observed).toEqual(response);
    });

    it('drops empty / null filters from the query string', () => {
      service
        .getDailyUsage({ channel: null, since: undefined, until: '' })
        .subscribe();
      const req = http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`);
      expect(req.request.params.keys().length).toBe(0);
      req.flush({ since: '', until: '', items: [] });
    });
  });

  describe('listInvoices', () => {
    it('hits /v1/billing/invoices and returns the parsed array', () => {
      const invoices: InvoiceRow[] = [
        {
          id: 'inv-1',
          number: 'F-2026-000001',
          plan_code: 'starter',
          period_start: '2026-06-01',
          period_end: '2026-06-30',
          issue_date: '2026-07-01',
          due_date: '2026-07-15',
          total_msgs: 1234,
          included_msgs: 1000,
          overage_msgs: 234,
          subtotal_clp: 25830,
          iva_clp: 4908,
          total_clp: 30738,
          status: 'issued',
          dte_number: 42,
          dte_url: 'https://example.com/dte/42',
          flow_invoice_id: 'INV-1',
          paid_at: null,
        },
      ];
      let observed: ReadonlyArray<InvoiceRow> | undefined;
      service.listInvoices().subscribe((value) => (observed = value));

      const req = http.expectOne(`${environment.apiBaseUrl}/v1/billing/invoices`);
      expect(req.request.method).toBe('GET');
      req.flush(invoices);
      expect(observed).toEqual(invoices);
    });
  });

  describe('dailyTotals', () => {
    it('aggregates per-channel buckets into a sorted {day → total} series', () => {
      const buckets: DailyUsageBucket[] = [
        { day: '2026-06-15', channel: 'sms', count: 2 },
        { day: '2026-06-15', channel: 'whatsapp', count: 3 },
        { day: '2026-06-14', channel: 'sms', count: 1 },
      ];
      const series = service.dailyTotals(buckets);
      expect(series).toEqual([
        { day: '2026-06-14', count: 1 },
        { day: '2026-06-15', count: 5 },
      ]);
    });

    it('returns an empty series for an empty bucket list', () => {
      expect(service.dailyTotals([])).toEqual([]);
    });
  });

  describe('getStatusSummary', () => {
    it('hits /v1/messages/summary with no query string when no filters are set', () => {
      let observed: StatusSummaryResponse | undefined;
      service.getStatusSummary().subscribe((value) => (observed = value));

      const req = http.expectOne(
        `${environment.apiBaseUrl}/v1/messages/summary`,
      );
      expect(req.request.method).toBe('GET');
      expect(req.request.params.keys().length).toBe(0);
      req.flush(SAMPLE_STATUS_SUMMARY);
      expect(observed).toEqual(SAMPLE_STATUS_SUMMARY);
    });

    it('passes every active filter as a query parameter', () => {
      let observed: StatusSummaryResponse | undefined;
      service
        .getStatusSummary({
          channel: 'whatsapp',
          since: '2026-06-01T00:00:00+00:00',
          until: '2026-06-30T23:59:59+00:00',
        })
        .subscribe((value) => (observed = value));

      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`,
      );
      const params = req.request.params;
      expect(params.get('channel')).toBe('whatsapp');
      expect(params.get('since')).toBe('2026-06-01T00:00:00+00:00');
      expect(params.get('until')).toBe('2026-06-30T23:59:59+00:00');
      req.flush(SAMPLE_STATUS_SUMMARY);
      expect(observed).toEqual(SAMPLE_STATUS_SUMMARY);
    });

    it('drops empty / null filters from the query string', () => {
      service
        .getStatusSummary({ channel: null, since: undefined, until: '' })
        .subscribe();
      const req = http.expectOne(
        (r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`,
      );
      expect(req.request.params.keys().length).toBe(0);
      req.flush(SAMPLE_STATUS_SUMMARY);
    });
  });

  describe('formatInvoiceStatus / invoiceStatusBadgeClass', () => {
    it('projects every known status into a Spanish label', () => {
      const cases: Array<[InvoiceRow['status'], string]> = [
        ['draft', 'Borrador'],
        ['issued', 'Emitida'],
        ['paid', 'Pagada'],
        ['overdue', 'Vencida'],
        ['voided', 'Anulada'],
      ];
      for (const [status, label] of cases) {
        expect(service.formatInvoiceStatus(status)).toBe(label);
      }
    });

    it('falls back to the raw value for an unknown invoice status', () => {
      const rogue = 'teleported' as unknown as InvoiceRow['status'];
      expect(service.formatInvoiceStatus(rogue)).toBe('teleported');
    });

    it('maps paid / issued / overdue / voided / draft to distinct badge classes', () => {
      const cases: Array<[InvoiceRow['status'], string]> = [
        ['paid', 'emerald'],
        ['issued', 'sky'],
        ['overdue', 'rose'],
        ['voided', 'slate'],
        ['draft', 'amber'],
      ];
      for (const [status, colour] of cases) {
        expect(service.invoiceStatusBadgeClass(status)).toContain(colour);
      }
    });
  });
});
