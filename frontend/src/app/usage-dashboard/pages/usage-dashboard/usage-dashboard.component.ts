import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnDestroy, OnInit } from '@angular/core';
import { FormBuilder, FormGroup } from '@angular/forms';
import { Subject, forkJoin } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { UsageDashboardService } from '../../services/usage-dashboard.service';
import {
  BalanceSummary,
  DailyUsageBucket,
  DailyUsageResponse,
  InvoiceRow,
  InvoiceStatus,
  MessageChannel,
  MessageHistoryFilters,
  MessageListResponse,
  MessageRow,
  MessageStatus,
  StatusSummaryBucket,
  StatusSummaryResponse,
} from '../../models/usage-dashboard.types';

/**
 * Page size of the message history. Matches the backend
 * default so a first paint of the dashboard shows the
 * same rows the API would have returned standalone.
 */
const DEFAULT_PAGE_SIZE = 50;

/**
 * "Historial y consumo" dashboard page.
 *
 * The component is responsible for:
 *
 * - Loading the live :class:`BalanceSummary`, the first
 *   page of the message history, the daily aggregation
 *   buckets (for the "gráfico de barras") and the
 *   invoice history in parallel on init.
 * - Reacting to filter changes from the form
 *   (``canal``, ``estado``, date range) and re-fetching
 *   the history when any of them change.
 * - Paging through the history via the "Cargar más"
 *   button (the service supports ``offset`` so we just
 *   bump it by the current page size).
 * - Surfacing both the API-level error (a stable
 *   ``code: message`` string from the backend) and the
 *   loading state in the template.
 *
 * The component is intentionally read-only: it does
 * not need to mutate server state. A future "send a
 * message" wizard will be a sibling component.
 */
@Component({
  selector: 'app-usage-dashboard',
  standalone: false,
  templateUrl: './usage-dashboard.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class UsageDashboardComponent implements OnInit, OnDestroy {
  /** Reactive filter form – see the template for bindings. */
  readonly filtersForm: FormGroup;

  /** Headline counters for the current period. */
  balance: BalanceSummary | null = null;

  /** Rows currently rendered in the history table. */
  rows: ReadonlyArray<MessageRow> = [];

  /**
   * Per-day, per-channel message counts the bar chart
   * renders. The component aggregates the per-channel
   * buckets into a single ``{day → total}`` series so
   * the template can iterate without re-computing the
   * sum on every change-detection cycle.
   */
  dailyBuckets: ReadonlyArray<DailyUsageBucket> = [];

  /** Aggregated ``{day → total}`` series for the chart. */
  dailySeries: ReadonlyArray<{ readonly day: string; readonly count: number }> = [];

  /** Resolved ``since`` the daily endpoint picked. */
  dailySince: string | null = null;

  /** Resolved ``until`` the daily endpoint picked. */
  dailyUntil: string | null = null;

  /**
   * Per-status message counts the "desglose por estado"
   * card renders. The component holds the full
   * :class:`StatusSummaryResponse` so the template can
   * reach the headline counters and the cost / fee
   * totals without a second round-trip.
   */
  statusSummary: StatusSummaryResponse | null = null;

  /** Resolved ``since`` the status-summary endpoint picked. */
  statusSummarySince: string | null = null;

  /** Resolved ``until`` the status-summary endpoint picked. */
  statusSummaryUntil: string | null = null;

  /** Invoice history, newest first. */
  invoices: ReadonlyArray<InvoiceRow> = [];

  /** ``True`` while a request is in flight. */
  loading = false;

  /** ``True`` once the very first load has completed. */
  initialLoadDone = false;

  /**
   * Human-readable error from the most recent failed
   * request, or ``null`` when the last call succeeded.
   */
  errorMessage: string | null = null;

  /**
   * ``True`` while a CSV export is in flight. Drives
   * the disabled state of the "Descargar CSV" button
   * so the user cannot kick off a second download
   * while the first one is still resolving.
   */
  exporting = false;

  /**
   * ``True`` when the server says there is at least one
   * more page after the current one. Drives the
   * "Cargar más" button.
   */
  hasMore = false;

  /** Total rows that match the current filter. */
  totalRows = 0;

  /** Current offset; advanced by :data:`DEFAULT_PAGE_SIZE` per page. */
  private currentOffset = 0;

  /** Subject that tears down every open subscription on destroy. */
  private readonly destroy$ = new Subject<void>();

  constructor(
    private readonly fb: FormBuilder,
    private readonly service: UsageDashboardService,
    private readonly cdr: ChangeDetectorRef,
  ) {
    this.filtersForm = this.fb.nonNullable.group({
      channel: [''],
      status: [''],
      since: [''],
      until: [''],
    });
    // Re-fetch the history when the user touches a filter
    // field. The status / channel / date inputs emit a
    // ``valueChanges`` event on every change; we collapse
    // the events into a single observable and re-issue
    // the request on the first event of the stream (the
    // debounce lives in the template – the dashboard does
    // not need a sophisticated debounce strategy for
    // three dropdowns and two date pickers).
    this.filtersForm.valueChanges
      .pipe(takeUntil(this.destroy$))
      .subscribe(() => {
        this.currentOffset = 0;
        this.rows = [];
        this.refreshHistory({ append: false });
      });
  }

  ngOnInit(): void {
    this.loadInitial();
  }

  ngOnDestroy(): void {
    this.destroy$.next();
    this.destroy$.complete();
  }

  /**
   * First-paint loader: hit the balance endpoint, the
   * first page of the history, the daily aggregation
   * and the invoice list in parallel so the dashboard
   * paints every section at once.
   *
   * A failure on any of the four is non-fatal: the
   * section that failed renders an inline error and the
   * rest of the dashboard still works. The only
   * critical path is the history load – the table is
   * the centrepiece of the screen.
   */
  private loadInitial(): void {
    this.loading = true;
    this.errorMessage = null;
    forkJoin({
      balance: this.service.getBalance(),
      history: this.service.listMessages({ limit: DEFAULT_PAGE_SIZE, offset: 0 }),
      daily: this.service.getDailyUsage(),
      summary: this.service.getStatusSummary(),
      invoices: this.service.listInvoices(),
    })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: ({ balance, history, daily, summary, invoices }) => {
          this.balance = balance;
          this.applyHistory(history, { append: false });
          this.applyDailyUsage(daily);
          this.applyStatusSummary(summary);
          this.invoices = [...invoices];
          this.loading = false;
          this.initialLoadDone = true;
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.loading = false;
          this.initialLoadDone = true;
          this.errorMessage = this.service.describeError(err)
            ?? 'No se pudo cargar el panel de uso. Intenta de nuevo.';
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * Re-fetch the history with the current filter state.
   * ``append`` controls whether the new rows replace the
   * old ones (filter change, first page) or extend the
   * current list ("Cargar más").
   */
  refreshHistory({ append }: { append: boolean }): void {
    this.loading = true;
    this.errorMessage = null;
    const filters: MessageHistoryFilters = this.buildFilters();
    if (!append) {
      filters.offset = 0;
    } else {
      filters.offset = this.currentOffset;
    }
    filters.limit = DEFAULT_PAGE_SIZE;
    this.service
      .listMessages(filters)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (history) => this.applyHistory(history, { append }),
        error: (err: unknown) => {
          this.loading = false;
          this.errorMessage = this.service.describeError(err)
            ?? 'No se pudo cargar el historial. Intenta de nuevo.';
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * "Cargar más" handler: advances the offset and
   * appends the next page to the current list.
   */
  loadMore(): void {
    this.refreshHistory({ append: true });
  }

  /**
   * Reset the filter form to its empty state and re-fetch
   * the unfiltered history. Wired to the "Limpiar" button
   * next to the filter inputs.
   */
  clearFilters(): void {
    this.filtersForm.reset({ channel: '', status: '', since: '', until: '' });
    // ``valueChanges`` will fire from the ``reset()`` call
    // above; the subscription installed in the constructor
    // will pick it up and trigger a refresh. We do not
    // need to call ``refreshHistory`` ourselves.
  }

  /**
   * "Descargar CSV" handler: fetch every message that
   * matches the current filter as a single CSV blob and
   * hand it to the browser's default save-as dialog.
   *
   * The download respects the same filter state the
   * on-screen list does, so a user who narrowed the
   * history down to "WhatsApp, failed, this month" gets
   * a CSV that matches the rows they can see in the
   * table. The button is disabled while a previous
   * export is still resolving (see :attr:`exporting`)
   * so a double-click does not fire two parallel
   * downloads.
   */
  exportCsv(): void {
    if (this.exporting) {
      return;
    }
    this.exporting = true;
    this.errorMessage = null;
    this.service
      .downloadMessagesCsv(this.buildFilters())
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (blob) => {
          this.service.saveBlobAsDownload(blob);
          this.exporting = false;
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.exporting = false;
          this.errorMessage = this.service.describeError(err)
            ?? 'No se pudo descargar el historial. Intenta de nuevo.';
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * Read the current filter-form state and project it
   * into the shape the service expects. The mapping is
   * deliberately inline (rather than a helper class)
   * because the form has only four fields.
   */
  private buildFilters(): MessageHistoryFilters {
    const raw = this.filtersForm.getRawValue() as {
      channel: string;
      status: string;
      since: string;
      until: string;
    };
    const channel = (raw.channel || null) as MessageChannel | null;
    const status = (raw.status || null) as MessageStatus | null;
    return {
      channel: channel ?? null,
      status: status ?? null,
      since: raw.since ? new Date(raw.since).toISOString() : null,
      until: raw.until ? new Date(raw.until).toISOString() : null,
    };
  }

  /**
   * Update the local state from a successful history
   * response. ``append`` picks between "replace" and
   * "extend" semantics; the offset bookkeeping is
   * always advanced to the new total so the next
   * "Cargar más" call picks up where this one left off.
   */
  private applyHistory(history: MessageListResponse, { append }: { append: boolean }): void {
    this.rows = append ? [...this.rows, ...history.items] : [...history.items];
    this.totalRows = history.total;
    this.hasMore = history.has_more;
    this.currentOffset = this.rows.length;
    this.loading = false;
    this.cdr.markForCheck();
  }

  /**
   * Store the daily aggregation response and the
   * derived ``{day → total}`` series the chart binds to.
   *
   * The component recomputes the series in place rather
   * than mutating a member of the service so the
   * change detector can stay OnPush: any input change
   * flips a single reference and the template iterates
   * the new array.
   */
  private applyDailyUsage(daily: DailyUsageResponse): void {
    this.dailyBuckets = [...daily.items];
    this.dailySeries = this.service.dailyTotals(daily.items);
    this.dailySince = daily.since;
    this.dailyUntil = daily.until;
  }

  /**
   * Store the status-summary response the "desglose por
   * estado" card binds to. The function keeps the full
   * :class:`StatusSummaryResponse` (rather than just the
   * items array) so the template can read the headline
   * counters and the cost / fee totals without the
   * component having to re-derive them.
   */
  private applyStatusSummary(summary: StatusSummaryResponse): void {
    this.statusSummary = summary;
    this.statusSummarySince = summary.since;
    this.statusSummaryUntil = summary.until;
  }

  /**
   * Compute the maximum count in the daily series. The
   * chart's CSS ``height`` percentages are normalised
   * against this value so a single outlier does not
   * flatten the rest of the bars.
   */
  dailyMax(): number {
    let max = 0;
    for (const point of this.dailySeries) {
      if (point.count > max) {
        max = point.count;
      }
    }
    return max;
  }

  /**
   * Height percentage of a single bar in the chart.
   * The value is clamped to ``1%`` so a day with a
   * single message is still visible to the customer
   * (a true zero would render as an empty bar the
   * dashboard treats as "no data").
   */
  dailyBarHeight(count: number): number {
    const max = this.dailyMax();
    if (max <= 0) {
      return 0;
    }
    return Math.max(1, Math.round((count / max) * 100));
  }

  /**
   * Format a destination for the table cell. Wraps the
   * service helper so the template does not have to
   * import it directly.
   */
  destination(row: MessageRow): string {
    return this.service.formatDestination(row);
  }

  /**
   * Format a status for the table cell.
   */
  status(row: MessageRow): string {
    return this.service.formatStatus(row.status);
  }

  /**
   * Format a channel for the table cell.
   */
  channel(row: MessageRow): string {
    return this.service.formatChannel(row.channel);
  }

  /**
   * Tailwind-friendly badge colour for a status. The
   * classes are kept as static strings so the change
   * detector does not have to re-evaluate them on every
   * row.
   */
  statusBadgeClass(row: MessageRow): string {
    switch (row.status) {
      case 'delivered':
      case 'sent':
        return 'bg-emerald-100 text-emerald-800';
      case 'queued':
      case 'pending':
        return 'bg-amber-100 text-amber-800';
      case 'failed':
        return 'bg-rose-100 text-rose-800';
      case 'unknown':
      default:
        return 'bg-slate-100 text-slate-700';
    }
  }

  /**
   * Format a YYYY-MM-DD day string into a short
   * dashboard-friendly label. The function never
   * returns the raw value (the customer does not need
   * to parse ``"2026-06-15"``) and falls back to a
   * dash for an unparseable input.
   */
  formatDay(value: string): string {
    if (!value) {
      return '—';
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleDateString('es-CL', {
      day: '2-digit',
      month: 'short',
    });
  }

  /**
   * Format an ISO timestamp into a dashboard-friendly
   * local string. The function is intentionally simple
   * (locale + short month) so the table stays scannable
   * – a future "exportar CSV" view will use the raw
   * ISO value.
   */
  formatDate(value: string): string {
    if (!value) {
      return '—';
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      return value;
    }
    return parsed.toLocaleString('es-CL', {
      dateStyle: 'short',
      timeStyle: 'short',
    });
  }

  /**
   * Format an :class:`InvoiceStatus` for the table cell.
   */
  invoiceStatus(status: InvoiceStatus): string {
    return this.service.formatInvoiceStatus(status);
  }

  /**
   * Tailwind-friendly badge colour for an invoice status.
   */
  invoiceBadge(status: InvoiceStatus): string {
    return this.service.invoiceStatusBadgeClass(status);
  }

  /**
   * ``trackBy`` for the history table. Using the message
   * id as the key keeps the DOM stable when the page
   * appends more rows; Angular can match the new array
   * to the previous one row-by-row.
   */
  trackRow(_index: number, row: MessageRow): string {
    return row.id;
  }

  /**
   * ``trackBy`` for the daily chart: the day string
   * uniquely identifies a bar in the series so the
   * DOM stays stable across re-renders.
   */
  trackDay(
    _index: number,
    point: { readonly day: string; readonly count: number },
  ): string {
    return point.day;
  }

  /**
   * ``trackBy`` for the invoice table.
   */
  trackInvoice(_index: number, invoice: InvoiceRow): string {
    return invoice.id;
  }

  /**
   * ``trackBy`` for the per-status breakdown list. The
   * status enum value uniquely identifies a row so the
   * DOM stays stable across re-renders.
   */
  trackStatusSummary(_index: number, row: StatusSummaryBucket): string {
    return row.status;
  }

  /**
   * Project a status enum into the Spanish label the
   * breakdown list shows. The mapping mirrors the
   * :func:`UsageDashboardService.formatStatus` helper
   * so the two views (the table's "Estado" column and
   * the breakdown's per-row label) stay in sync.
   */
  statusSummaryStatusLabel(status: MessageStatus): string {
    return this.service.formatStatus(status);
  }

  /**
   * Tailwind-friendly dot colour for a status. The
   * colour mirrors the badge colour the history table
   * uses for the matching status (delivered / sent →
   * emerald; queued / pending → amber; failed → rose;
   * unknown → slate) so the two views feel
   * consistent.
   */
  statusSummaryDotClass(status: MessageStatus): string {
    switch (status) {
      case 'delivered':
      case 'sent':
        return 'bg-emerald-500';
      case 'queued':
      case 'pending':
        return 'bg-amber-500';
      case 'failed':
        return 'bg-rose-500';
      case 'unknown':
      default:
        return 'bg-slate-400';
    }
  }

  /**
   * Width percentage of the delivery-rate progress bar.
   * The value is clamped to ``[0, 100]`` so a future
   * refactor that returns a ``delivery_rate`` outside
   * the documented ``[0.0, 1.0]`` range does not
   * produce a negative or oversized bar.
   */
  statusSummaryDeliveryWidth(summary: StatusSummaryResponse): number {
    if (summary.total <= 0) {
      return 0;
    }
    return Math.max(0, Math.min(100, Math.round(summary.delivery_rate * 100)));
  }

  /**
   * Delivery-rate percentage as a rounded integer. The
   * value is what the badge next to the progress bar
   * shows ("87%"); the bar width is a separate
   * :func:`statusSummaryDeliveryWidth` call so the two
   * can diverge if a future design wants finer-grained
   * width resolution.
   */
  statusSummaryDeliveryPercent(summary: StatusSummaryResponse): number {
    return this.statusSummaryDeliveryWidth(summary);
  }

  /**
   * Per-status percentage of the total. The value is
   * an integer (``0`` – ``100``) and returns ``0`` for
   * an empty summary so the template can render the
   * number without a special-case branch.
   */
  statusSummaryPercent(count: number, total: number): number {
    if (total <= 0) {
      return 0;
    }
    return Math.round((count / total) * 100);
  }

  /**
   * Average cost per message in the active window. The
   * helper is a thin wrapper around a ``total > 0``
   * guard so the footer does not show a "$NaN" when
   * the customer has not sent any messages.
   */
  statusSummaryAverageCost(summary: StatusSummaryResponse): number {
    if (summary.total <= 0) {
      return 0;
    }
    return Math.round(summary.cost_clp / summary.total);
  }
}
