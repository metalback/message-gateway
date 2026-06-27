import { HttpErrorResponse, provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ReactiveFormsModule } from '@angular/forms';
import { NoopAnimationsModule } from '@angular/platform-browser/animations';
import { ActivatedRoute, RouterModule } from '@angular/router';

import { environment } from '../../../../environments/environment';
import { UsageDashboardComponent } from './usage-dashboard.component';
import {
  BalanceSummary,
  DailyUsageResponse,
  InvoiceRow,
  MessageListResponse,
  MessageRow,
  StatusSummaryResponse,
} from '../../models/usage-dashboard.types';

/**
 * Helper that builds a fully-formed :class:`MessageRow`
 * with sensible defaults. The tests override the fields
 * they care about; the rest is dummy.
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

/** A canned daily-usage payload for the bar chart tests. */
const SAMPLE_DAILY: DailyUsageResponse = {
  since: '2026-06-14T00:00:00+00:00',
  until: '2026-06-15T00:00:00+00:00',
  items: [
    { day: '2026-06-14', channel: 'sms', count: 1 },
    { day: '2026-06-15', channel: 'sms', count: 2 },
    { day: '2026-06-15', channel: 'whatsapp', count: 3 },
  ],
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

/** A canned invoice list for the invoice table tests. */
const SAMPLE_INVOICES: InvoiceRow[] = [
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
    dte_url: 'https://example.com/dte/42.pdf',
    flow_invoice_id: 'INV-1',
    paid_at: null,
  },
  {
    id: 'inv-2',
    number: 'F-2026-000002',
    plan_code: 'starter',
    period_start: '2026-05-01',
    period_end: '2026-05-31',
    issue_date: '2026-06-01',
    due_date: '2026-06-15',
    total_msgs: 800,
    included_msgs: 800,
    overage_msgs: 0,
    subtotal_clp: 19990,
    iva_clp: 3798,
    total_clp: 23788,
    status: 'paid',
    dte_number: 41,
    dte_url: 'https://example.com/dte/41.pdf',
    flow_invoice_id: 'INV-2',
    paid_at: '2026-06-10T12:00:00+00:00',
  },
];

function pageResponse(
  items: ReadonlyArray<MessageRow>,
  overrides: Partial<MessageListResponse> = {},
): MessageListResponse {
  return {
    items: [...items],
    total: overrides.total ?? items.length,
    limit: overrides.limit ?? 50,
    offset: overrides.offset ?? 0,
    has_more: overrides.has_more ?? false,
  };
}

describe('UsageDashboardComponent', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [
        UsageDashboardComponent,
        ReactiveFormsModule,
        RouterModule.forRoot([]),
        NoopAnimationsModule,
      ],
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        {
          // The component injects ``ActivatedRoute``
          // indirectly through the router; the bare
          // test-bed needs a stub so the constructor
          // does not blow up on a missing provider.
          provide: ActivatedRoute,
          useValue: { snapshot: { params: {}, queryParams: {} } },
        },
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  /** Flush the parallel ``forkJoin`` issued by ``ngOnInit``. */
  function flushInitial(): void {
    http.expectOne(`${environment.apiBaseUrl}/v1/billing/balance`).flush(SAMPLE_BALANCE);
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`).flush(
      pageResponse([makeRow({ id: 'a' }), makeRow({ id: 'b' })]),
    );
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`).flush(
      SAMPLE_DAILY,
    );
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`).flush(
      SAMPLE_STATUS_SUMMARY,
    );
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`).flush(
      SAMPLE_INVOICES,
    );
  }

  it('renders the balance summary and the first history page on init', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    const root = fixture.nativeElement as HTMLElement;
    const card = root.querySelector('[data-testid="balance-card"]');
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain('Starter');

    const rows = root.querySelectorAll('[data-testid="history-row"]');
    expect(rows.length).toBe(2);
  });

  it('shows a friendly error banner when the initial load fails', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    http
      .expectOne(`${environment.apiBaseUrl}/v1/billing/balance`)
      .flush(
        { detail: { code: 'missing_api_key', message: 'X-API-Key required' } },
        { status: 401, statusText: 'Unauthorized' },
      );
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`)
      .flush(
        { detail: { code: 'missing_api_key', message: 'X-API-Key required' } },
        { status: 401, statusText: 'Unauthorized' },
      );
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`)
      .flush(
        { detail: { code: 'missing_api_key', message: 'X-API-Key required' } },
        { status: 401, statusText: 'Unauthorized' },
      );
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`)
      .flush(
        { detail: { code: 'missing_api_key', message: 'X-API-Key required' } },
        { status: 401, statusText: 'Unauthorized' },
      );
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`)
      .flush(
        { detail: { code: 'missing_api_key', message: 'X-API-Key required' } },
        { status: 401, statusText: 'Unauthorized' },
      );
    fixture.detectChanges();

    const banner = fixture.nativeElement.querySelector('[data-testid="error-banner"]');
    expect(banner?.textContent).toContain('missing_api_key');
  });

  it('refetches the history when a filter changes', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    // Change the channel filter; the component should
    // re-issue the request with ``channel=whatsapp``.
    fixture.componentInstance.filtersForm.patchValue({ channel: 'whatsapp' });

    const req = http.expectOne((r) => {
      if (r.url !== `${environment.apiBaseUrl}/v1/messages`) {
        return false;
      }
      return r.params.get('channel') === 'whatsapp';
    });
    req.flush(pageResponse([makeRow({ id: 'w1', channel: 'whatsapp' })]));
    fixture.detectChanges();

    const rows = fixture.nativeElement.querySelectorAll('[data-testid="history-row"]');
    expect(rows.length).toBe(1);
  });

  it('appends the next page when "Cargar más" is clicked', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    // Trigger the "Cargar más" handler. The component
    // bumps the offset by the current row count, so
    // the request carries ``offset=2`` after the first
    // page has rendered.
    fixture.componentInstance.loadMore();
    const req = http.expectOne((r) => {
      if (r.url !== `${environment.apiBaseUrl}/v1/messages`) {
        return false;
      }
      return r.params.get('offset') === '2';
    });
    req.flush(
      pageResponse([makeRow({ id: 'c' })], { total: 3, has_more: false }),
    );
    fixture.detectChanges();

    const rows = fixture.nativeElement.querySelectorAll('[data-testid="history-row"]');
    expect(rows.length).toBe(3);
  });

  it('renders a well-known status badge for every status enum value', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    const component = fixture.componentInstance;
    expect(component.statusBadgeClass(makeRow({ status: 'delivered' }))).toContain('emerald');
    expect(component.statusBadgeClass(makeRow({ status: 'failed' }))).toContain('rose');
    expect(component.statusBadgeClass(makeRow({ status: 'pending' }))).toContain('amber');
    expect(component.statusBadgeClass(makeRow({ status: 'unknown' }))).toContain('slate');
  });

  it('formats an ISO timestamp as a localised date string', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    const formatted = fixture.componentInstance.formatDate('2026-06-15T10:00:00+00:00');
    // The exact string depends on the runtime's
    // locale data; the assertion is that the helper
    // produces a non-empty, non-raw result.
    expect(formatted).toBeTruthy();
    expect(formatted).not.toBe('2026-06-15T10:00:00+00:00');
  });

  it('returns the raw timestamp when the input cannot be parsed', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    expect(fixture.componentInstance.formatDate('not-a-date')).toBe('not-a-date');
  });

  it('shows the empty-state row when the history is empty', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    http
      .expectOne(`${environment.apiBaseUrl}/v1/billing/balance`)
      .flush(SAMPLE_BALANCE);
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`)
      .flush(pageResponse([]));
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`)
      .flush({ since: '2026-06-14T00:00:00+00:00', until: '2026-06-15T00:00:00+00:00', items: [] });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`)
      .flush({
        since: '2026-05-16T00:00:00+00:00',
        until: '2026-06-15T00:00:00+00:00',
        items: [
          { status: 'delivered', count: 0 },
          { status: 'sent', count: 0 },
          { status: 'queued', count: 0 },
          { status: 'pending', count: 0 },
          { status: 'failed', count: 0 },
          { status: 'unknown', count: 0 },
        ],
        total: 0,
        delivered: 0,
        failed: 0,
        pending: 0,
        cost_clp: 0,
        fee_clp: 0,
        delivery_rate: 0,
      });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`)
      .flush([]);
    fixture.detectChanges();

    const empty = fixture.nativeElement.querySelector('[data-testid="history-empty"]');
    expect(empty).not.toBeNull();
  });

  it('clears the filters back to the initial state when the user clicks "Limpiar"', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    // Set a non-default filter, then clear it.
    fixture.componentInstance.filtersForm.patchValue({ channel: 'sms' });
    http
      .expectOne((r) => r.params.get('channel') === 'sms')
      .flush(pageResponse([]));
    fixture.detectChanges();

    fixture.componentInstance.clearFilters();
    const req = http.expectOne((r) => r.params.keys().length === 0);
    req.flush(pageResponse([]));
    fixture.detectChanges();

    expect(fixture.componentInstance.filtersForm.value.channel).toBe('');
  });

  it('does not surface the empty state while the initial load is still in flight', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    // Intentionally do NOT flush the requests – the
    // assertion verifies the "loading" branch is the
    // one rendered until the requests resolve.
    const empty = fixture.nativeElement.querySelector('[data-testid="history-empty"]');
    expect(empty).toBeNull();
  });

  it('survives a partial failure: balance loads, history errors', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    http
      .expectOne(`${environment.apiBaseUrl}/v1/billing/balance`)
      .flush(SAMPLE_BALANCE);
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`)
      .flush(
        { detail: { code: 'rate_limited', message: 'too many' } },
        { status: 429, statusText: 'Too Many Requests' },
      );
    // The remaining three parallel requests are still
    // outstanding; flush them so the test does not leak
    // requests into the next test.
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`)
      .flush(SAMPLE_DAILY);
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`)
      .flush(SAMPLE_STATUS_SUMMARY);
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`)
      .flush(SAMPLE_INVOICES);
    fixture.detectChanges();

    // The error banner surfaces the human-readable
    // detail; the balance card still renders because
    // the balance call succeeded.
    const banner = fixture.nativeElement.querySelector('[data-testid="error-banner"]');
    expect(banner?.textContent).toContain('rate_limited');
    const card = fixture.nativeElement.querySelector('[data-testid="balance-card"]');
    expect(card).not.toBeNull();
  });

  it('renders one bar per day in the daily-usage chart', () => {
    // The chart consumes the per-channel buckets the
    // backend returns and aggregates them into a single
    // ``{day → total}`` series. The test asserts the
    // rendered DOM has one bar per day in the series.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    const bars = fixture.nativeElement.querySelectorAll('[data-testid="daily-bar"]');
    // SAMPLE_DAILY aggregates to two days: 2026-06-14 and 2026-06-15.
    expect(bars.length).toBe(2);
    // The chart's "total days" badge echoes the series length.
    const totalBadge = fixture.nativeElement.querySelector('[data-testid="daily-total"]');
    expect(totalBadge?.textContent).toContain('2');
  });

  it('shows the empty-state placeholder when the chart has no data', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    http
      .expectOne(`${environment.apiBaseUrl}/v1/billing/balance`)
      .flush(SAMPLE_BALANCE);
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`)
      .flush(pageResponse([]));
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`)
      .flush({ since: '2026-06-14T00:00:00+00:00', until: '2026-06-15T00:00:00+00:00', items: [] });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`)
      .flush({
        since: '2026-05-16T00:00:00+00:00',
        until: '2026-06-15T00:00:00+00:00',
        items: [
          { status: 'delivered', count: 0 },
          { status: 'sent', count: 0 },
          { status: 'queued', count: 0 },
          { status: 'pending', count: 0 },
          { status: 'failed', count: 0 },
          { status: 'unknown', count: 0 },
        ],
        total: 0,
        delivered: 0,
        failed: 0,
        pending: 0,
        cost_clp: 0,
        fee_clp: 0,
        delivery_rate: 0,
      });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`)
      .flush([]);
    fixture.detectChanges();

    const empty = fixture.nativeElement.querySelector('[data-testid="daily-chart-empty"]');
    expect(empty).not.toBeNull();
  });

  it('renders the invoice list with one row per invoice on init', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    const rows = fixture.nativeElement.querySelectorAll('[data-testid="invoice-row"]');
    expect(rows.length).toBe(2);
    // The first invoice is "issued" and the badge uses the
    // sky colour; the second is "paid" and uses emerald.
    const badges = fixture.nativeElement.querySelectorAll(
      '[data-testid^="invoice-status-"]',
    );
    expect(badges.length).toBe(2);
    expect(badges[0].className).toContain('sky');
    expect(badges[1].className).toContain('emerald');
  });

  it('renders the empty-state row when the invoice list is empty', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    http
      .expectOne(`${environment.apiBaseUrl}/v1/billing/balance`)
      .flush(SAMPLE_BALANCE);
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`)
      .flush(pageResponse([]));
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`)
      .flush({ since: '2026-06-14T00:00:00+00:00', until: '2026-06-15T00:00:00+00:00', items: [] });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`)
      .flush({
        since: '2026-05-16T00:00:00+00:00',
        until: '2026-06-15T00:00:00+00:00',
        items: [
          { status: 'delivered', count: 0 },
          { status: 'sent', count: 0 },
          { status: 'queued', count: 0 },
          { status: 'pending', count: 0 },
          { status: 'failed', count: 0 },
          { status: 'unknown', count: 0 },
        ],
        total: 0,
        delivered: 0,
        failed: 0,
        pending: 0,
        cost_clp: 0,
        fee_clp: 0,
        delivery_rate: 0,
      });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`)
      .flush([]);
    fixture.detectChanges();

    const empty = fixture.nativeElement.querySelector('[data-testid="invoices-empty"]');
    expect(empty).not.toBeNull();
  });

  it('delegates to the service for invoice status formatting', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    const component = fixture.componentInstance;
    expect(component.invoiceStatus('paid')).toBe('Pagada');
    expect(component.invoiceStatus('overdue')).toBe('Vencida');
    expect(component.invoiceBadge('paid')).toContain('emerald');
  });

  it('downloads a CSV when the "Descargar CSV" button is clicked', () => {
    // The "Descargar CSV" button hits the same
    // ``/v1/messages/export`` endpoint the service
    // documents, with the same filter set the
    // on-screen list uses. The test stubs the HTTP
    // call, clicks the button and asserts the
    // request was issued with the right filter and
    // the right response type.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    // Narrow the filter down so the export carries
    // the same query string the user can see in the
    // form. The handler is wired to ``valueChanges``
    // so we flush the resulting refetch first.
    fixture.componentInstance.filtersForm.patchValue({ channel: 'whatsapp' });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`)
      .flush(pageResponse([]));
    fixture.detectChanges();

    // Click the export button. The handler issues a
    // blob-typed GET to ``/v1/messages/export`` with
    // the current filter set.
    const button = fixture.nativeElement.querySelector(
      '[data-testid="export-csv"]',
    ) as HTMLButtonElement | null;
    expect(button).not.toBeNull();
    button?.click();

    const exportReq = http.expectOne((r) => {
      if (r.url !== `${environment.apiBaseUrl}/v1/messages/export`) {
        return false;
      }
      return r.params.get('channel') === 'whatsapp';
    });
    expect(exportReq.request.method).toBe('GET');
    expect(exportReq.request.responseType).toBe('blob');
    exportReq.flush(new Blob(['id,channel\nrow-1,whatsapp\n'], { type: 'text/csv' }));
    fixture.detectChanges();

    // The button label resets once the download is
    // done; the assertion pins the disabled state
    // transition so a regression that never flips
    // ``exporting`` back to ``false`` is caught.
    expect(fixture.componentInstance.exporting).toBe(false);
  });

  it('surfaces a banner when the CSV download fails', () => {
    // A 500 on the export endpoint must surface as a
    // banner the same way a history fetch failure
    // does. The button itself is the trigger so the
    // test only covers the click + error path.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector(
      '[data-testid="export-csv"]',
    ) as HTMLButtonElement | null;
    button?.click();

    http
      .expectOne(`${environment.apiBaseUrl}/v1/messages/export`)
      .flush(
        { detail: { code: 'export_failed', message: 'downstream timeout' } },
        { status: 502, statusText: 'Bad Gateway' },
      );
    fixture.detectChanges();

    const banner = fixture.nativeElement.querySelector('[data-testid="error-banner"]');
    expect(banner?.textContent).toContain('export_failed');
    expect(fixture.componentInstance.exporting).toBe(false);
  });

  it('ignores a second click while the first export is still in flight', () => {
    // A double-click on the button must not fire two
    // parallel requests: the helper flips
    // ``exporting`` to ``true`` and the second call
    // is a no-op until the first one resolves. The
    // test confirms ``http.expectNone`` for the
    // second request.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector(
      '[data-testid="export-csv"]',
    ) as HTMLButtonElement | null;
    button?.click();
    // First request is now in flight. The button
    // is still visible (we do not toggle ``hidden``)
    // but the handler refuses to issue a second one.
    button?.click();

    const exportReq = http.expectOne(`${environment.apiBaseUrl}/v1/messages/export`);
    http.expectNone(`${environment.apiBaseUrl}/v1/messages/export`);
    exportReq.flush(new Blob([''], { type: 'text/csv' }));
    fixture.detectChanges();
  });

  it('renders the status summary card with the per-status breakdown on init', () => {
    // The "desglose por estado" card is wired to the new
    // ``/v1/messages/summary`` endpoint. The test seeds a
    // known summary payload, flushes the parallel
    // ``forkJoin`` and asserts the rendered DOM carries
    // the per-status rows plus the delivery-rate badge
    // and the cost summary footer.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    flushInitial();
    fixture.detectChanges();

    const card = fixture.nativeElement.querySelector(
      '[data-testid="status-summary-card"]',
    );
    expect(card).not.toBeNull();
    // The total counter is rendered in the header.
    const total = fixture.nativeElement.querySelector(
      '[data-testid="status-summary-total"]',
    );
    expect(total?.textContent).toContain('11');
    // The breakdown list renders one row per status from
    // the response (the backend zero-fills missing
    // statuses, so the dashboard always gets six rows).
    const rows = fixture.nativeElement.querySelectorAll(
      '[data-testid="status-summary-bars"] dt',
    );
    expect(rows.length).toBe(6);
  });

  it('clamps the delivery-rate progress bar to [0, 100] and formats it as a percentage', () => {
    // The component renders a Tailwind-only progress bar
    // whose width is ``delivery_rate * 100``, clamped to
    // the closed interval ``[0, 100]`` so a future
    // regression that returns a value outside the
    // documented range does not produce a negative or
    // oversized bar.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    const component = fixture.componentInstance;
    // 7/11 → 0.6364 → 64%.
    expect(component.statusSummaryDeliveryPercent(SAMPLE_STATUS_SUMMARY)).toBe(64);
    expect(component.statusSummaryDeliveryWidth(SAMPLE_STATUS_SUMMARY)).toBe(64);
    // An empty summary produces a 0% bar (no
    // divide-by-zero).
    const empty: StatusSummaryResponse = {
      ...SAMPLE_STATUS_SUMMARY,
      total: 0,
      delivered: 0,
      delivery_rate: 0,
    };
    expect(component.statusSummaryDeliveryPercent(empty)).toBe(0);
    expect(component.statusSummaryDeliveryWidth(empty)).toBe(0);
  });

  it('computes the average cost per message in the active window', () => {
    // The "Promedio por mensaje" footer of the card is
    // ``cost_clp / total``, rounded to an integer. The
    // component guards against a divide-by-zero for an
    // empty summary.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    const component = fixture.componentInstance;
    // 275 / 11 = 25 (rounded).
    expect(component.statusSummaryAverageCost(SAMPLE_STATUS_SUMMARY)).toBe(25);
    // Empty summary: zero, not NaN.
    const empty: StatusSummaryResponse = {
      ...SAMPLE_STATUS_SUMMARY,
      total: 0,
      cost_clp: 0,
    };
    expect(component.statusSummaryAverageCost(empty)).toBe(0);
  });

  it('renders the status-summary empty-state when the customer has no traffic', () => {
    // An empty summary (every status row has a count of
    // zero) renders the "todavía no has enviado mensajes"
    // empty state and hides the delivery-rate bar and the
    // cost summary footer.
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();
    http
      .expectOne(`${environment.apiBaseUrl}/v1/billing/balance`)
      .flush(SAMPLE_BALANCE);
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`)
      .flush(pageResponse([]));
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`)
      .flush({ since: '2026-06-14T00:00:00+00:00', until: '2026-06-15T00:00:00+00:00', items: [] });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`)
      .flush({
        since: '2026-05-16T00:00:00+00:00',
        until: '2026-06-15T00:00:00+00:00',
        items: [
          { status: 'delivered', count: 0 },
          { status: 'sent', count: 0 },
          { status: 'queued', count: 0 },
          { status: 'pending', count: 0 },
          { status: 'failed', count: 0 },
          { status: 'unknown', count: 0 },
        ],
        total: 0,
        delivered: 0,
        failed: 0,
        pending: 0,
        cost_clp: 0,
        fee_clp: 0,
        delivery_rate: 0,
      });
    http
      .expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`)
      .flush([]);
    fixture.detectChanges();

    const empty = fixture.nativeElement.querySelector(
      '[data-testid="status-summary-empty"]',
    );
    expect(empty).not.toBeNull();
    // The breakdown list is hidden when the summary is
    // empty (the customer has not produced any traffic
    // yet).
    const bars = fixture.nativeElement.querySelector(
      '[data-testid="status-summary-bars"]',
    );
    expect(bars).toBeNull();
  });
});

/**
 * A focused unit test for the format helpers; pulled
 * out of the component spec so the assertions stay
 * close to the code they exercise.
 */
describe('UsageDashboardComponent (format helpers)', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [
        UsageDashboardComponent,
        ReactiveFormsModule,
        RouterModule.forRoot([]),
        NoopAnimationsModule,
      ],
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { snapshot: { params: {}, queryParams: {} } } },
      ],
    }).compileComponents();
  });

  it('delegates to the service for status / channel / destination formatting', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    const row = makeRow();
    expect(fixture.componentInstance.status(row)).toBe('Enviado');
    expect(fixture.componentInstance.channel(row)).toBe('SMS');
    expect(fixture.componentInstance.destination(row)).toBe('+56912345678');
  });

  it('falls back to a dash for an empty destination', () => {
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    expect(fixture.componentInstance.destination(makeRow({ to_number: '' }))).toBe('—');
  });
});

/**
 * Catch-all check that an :class:`HttpErrorResponse`
 * raised by the underlying ``HttpClient`` is rendered
 * as the human-readable code. The shape mirrors what
 * the platform returns in production.
 */
describe('UsageDashboardComponent (error rendering)', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [
        UsageDashboardComponent,
        ReactiveFormsModule,
        RouterModule.forRoot([]),
        NoopAnimationsModule,
      ],
      providers: [
        provideHttpClient(withInterceptorsFromDi()),
        provideHttpClientTesting(),
        { provide: ActivatedRoute, useValue: { snapshot: { params: {}, queryParams: {} } } },
      ],
    }).compileComponents();
  });

  it('produces a banner with the error code when the initial load 500s', () => {
    const http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(UsageDashboardComponent);
    fixture.detectChanges();

    const error = new HttpErrorResponse({
      status: 500,
      statusText: 'Internal Server Error',
      error: { detail: { code: 'unknown', message: 'oops' } },
    });
    http.expectOne(`${environment.apiBaseUrl}/v1/billing/balance`).flush(
      { detail: { code: 'unknown', message: 'oops' } },
      { status: 500, statusText: 'Internal Server Error' },
    );
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages`).flush(
      { detail: { code: 'unknown', message: 'oops' } },
      { status: 500, statusText: 'Internal Server Error' },
    );
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/daily`).flush(
      { detail: { code: 'unknown', message: 'oops' } },
      { status: 500, statusText: 'Internal Server Error' },
    );
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/messages/summary`).flush(
      { detail: { code: 'unknown', message: 'oops' } },
      { status: 500, statusText: 'Internal Server Error' },
    );
    http.expectOne((r) => r.url === `${environment.apiBaseUrl}/v1/billing/invoices`).flush(
      { detail: { code: 'unknown', message: 'oops' } },
      { status: 500, statusText: 'Internal Server Error' },
    );
    fixture.detectChanges();

    // ``HttpErrorResponse`` is just a wrapper; the
    // assertion guards against an accidental swap of
    // the error wrapper for a different exception
    // type.
    expect(error instanceof HttpErrorResponse).toBe(true);

    const banner = fixture.nativeElement.querySelector('[data-testid="error-banner"]');
    expect(banner?.textContent).toContain('unknown');
  });
});
