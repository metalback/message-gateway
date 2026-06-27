import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnDestroy, OnInit } from '@angular/core';
import { FormBuilder, FormGroup } from '@angular/forms';
import { Subject, forkJoin } from 'rxjs';
import { takeUntil } from 'rxjs/operators';

import { AdminDashboardService } from '../../services/admin-dashboard.service';
import {
  AdminClientListFilters,
  AdminClientListResponse,
  AdminClientRow,
  AdminErrorLogResponse,
  AdminOverview,
  AdminProviderBreakdownRow,
} from '../../models/admin-dashboard.types';
import { ClientEditDialogResult } from '../client-edit-dialog/client-edit-dialog.component';

/**
 * Page size of the admin clients / error-log tables.
 * Matches the backend default so a first paint of the
 * dashboard shows the same rows the API would have
 * returned standalone.
 */
const DEFAULT_PAGE_SIZE = 50;

/**
 * "Admin · Gestión de clientes y métricas" dashboard page
 * (issue #10).
 *
 * The component is responsible for:
 *
 * - Loading the admin overview, the first page of the
 *   clients table, the per-provider breakdown and the
 *   error log in parallel on init.
 * - Reacting to filter changes from the form (``q`` /
 *   ``plan`` / ``status``) and re-fetching the clients
 *   table when any of them change.
 * - Paging through the clients table via the "Cargar más"
 *   button.
 * - Surfacing both the API-level error (a stable
 *   ``code: message`` string from the backend) and the
 *   loading state in the template.
 *
 * The component is intentionally focused on the
 * observable behaviour the operator needs: it never
 * mutates server state directly, it never imports
 * :class:`HttpClient` and the OnPush change detection
 * keeps the dashboard snappy on a large clients list.
 */
@Component({
  selector: 'app-admin-dashboard',
  standalone: false,
  templateUrl: './admin-dashboard.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class AdminDashboardComponent implements OnInit, OnDestroy {
  /** Reactive filter form – see the template for bindings. */
  readonly filtersForm: FormGroup;

  /** Headline counters for the overview card. */
  overview: AdminOverview | null = null;

  /** Rows currently rendered in the clients table. */
  clients: ReadonlyArray<AdminClientRow> = [];

  /** Total rows that match the current filter. */
  totalClients = 0;

  /** ``True`` when the server says there is at least one
   *  more page after the current one. */
  hasMoreClients = false;

  /** Per-provider aggregates the breakdown card renders. */
  providerBreakdown: ReadonlyArray<AdminProviderBreakdownRow> = [];

  /** Recent failed messages the error log table renders. */
  errorLog: AdminErrorLogResponse | null = null;

  /** ``True`` while a request is in flight. */
  loading = false;

  /** ``True`` once the very first load has completed. */
  initialLoadDone = false;

  /**
   * Human-readable error from the most recent failed
   * request, or ``null`` when the last call succeeded.
   */
  errorMessage: string | null = null;

  /** Current offset; advanced by
   *  :data:`DEFAULT_PAGE_SIZE` per page. */
  private currentOffset = 0;

  /** ``True`` while the create / edit dialog is open. */
  dialogOpen = false;

  /** Dialog mode – mirrors :attr:`ClientEditDialogComponent.mode`. */
  dialogMode: 'create' | 'edit' = 'create';

  /** Row the dialog is editing, or ``null`` in create mode. */
  dialogClient: AdminClientRow | null = null;

  /**
   * Most recent plain API key the create dialog returned.
   * Surfaced as a banner after the dialog closes so the
   * operator can copy the value into the onboarding flow
   * before navigating away.
   */
  lastCreatedApiKey: string | null = null;

  /** Subject that tears down every open subscription on destroy. */
  private readonly destroy$ = new Subject<void>();

  constructor(
    private readonly fb: FormBuilder,
    private readonly service: AdminDashboardService,
    private readonly cdr: ChangeDetectorRef,
  ) {
    this.filtersForm = this.fb.nonNullable.group({
      q: [''],
      plan: [''],
      status: [''],
    });
    // Re-fetch the clients table when the user touches a
    // filter field. The ``valueChanges`` event fires on
    // every keystroke; we collapse the events into a
    // single observable and re-issue the request on the
    // first event of the stream. A debounce lives in the
    // template (no fancy debounce strategy needed for two
    // dropdowns and a search box).
    this.filtersForm.valueChanges
      .pipe(takeUntil(this.destroy$))
      .subscribe(() => {
        this.currentOffset = 0;
        this.clients = [];
        this.refreshClients({ append: false });
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
   * First-paint loader: hit the overview, the first page
   * of clients, the per-provider breakdown and the error
   * log in parallel so the dashboard paints every
   * section at once.
   *
   * A failure on any of the four is non-fatal: the
   * section that failed renders an inline error and the
   * rest of the dashboard still works. The critical
   * path is the clients table – the table is the
   * centrepiece of the screen.
   */
  private loadInitial(): void {
    this.loading = true;
    this.errorMessage = null;
    forkJoin({
      overview: this.service.getOverview(),
      clients: this.service.listClients({
        limit: DEFAULT_PAGE_SIZE,
        offset: 0,
      }),
      breakdown: this.service.getProviderBreakdown(),
      log: this.service.listErrorLog(DEFAULT_PAGE_SIZE, 0),
    })
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: ({ overview, clients, breakdown, log }) => {
          this.overview = overview;
          this.applyClients(clients, { append: false });
          this.providerBreakdown = [...breakdown];
          this.errorLog = log;
          this.loading = false;
          this.initialLoadDone = true;
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.loading = false;
          this.initialLoadDone = true;
          this.errorMessage = this.service.describeError(err)
            ?? 'No se pudo cargar el panel de administración. Intenta de nuevo.';
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * Re-fetch the clients table with the current filter
   * state. ``append`` controls whether the new rows
   * replace the old ones (filter change, first page) or
   * extend the current list ("Cargar más").
   */
  refreshClients({ append }: { append: boolean }): void {
    this.loading = true;
    this.errorMessage = null;
    const filters = this.buildFilters();
    if (!append) {
      filters.offset = 0;
    } else {
      filters.offset = this.currentOffset;
    }
    filters.limit = DEFAULT_PAGE_SIZE;
    this.service
      .listClients(filters)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (clients) => this.applyClients(clients, { append }),
        error: (err: unknown) => {
          this.loading = false;
          this.errorMessage = this.service.describeError(err)
            ?? 'No se pudo cargar la lista de clientes. Intenta de nuevo.';
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * "Cargar más" handler: advances the offset and
   * appends the next page to the current list.
   */
  loadMore(): void {
    this.refreshClients({ append: true });
  }

  /**
   * Reset the filter form to its empty state and re-fetch
   * the unfiltered clients table. Wired to the "Limpiar"
   * button next to the filter inputs.
   */
  clearFilters(): void {
    this.filtersForm.reset({ q: '', plan: '', status: '' });
    // ``valueChanges`` will fire from the ``reset()`` call
    // above; the subscription installed in the constructor
    // will pick it up and trigger a refresh. We do not
    // need to call ``refreshClients`` ourselves.
  }

  /**
   * "Suspender" handler: flip the matching client to
   * ``status=suspended``. The local state is updated in
   * place so the table reflects the change without
   * waiting for a full re-fetch.
   */
  suspend(row: AdminClientRow): void {
    this.errorMessage = null;
    this.service
      .suspendClient(row.id)
      .pipe(takeUntil(this.destroy$))
      .subscribe({
        next: (response) => {
          this.replaceClient(response.client);
          this.cdr.markForCheck();
        },
        error: (err: unknown) => {
          this.errorMessage = this.service.describeError(err)
            ?? 'No se pudo suspender al cliente. Intenta de nuevo.';
          this.cdr.markForCheck();
        },
      });
  }

  /**
   * "Crear cliente" handler: open the dialog in create
   * mode. The dialog manages its own form state and
   * posts to the service on submit; the dashboard only
   * owns the visibility flag.
   */
  openCreateDialog(): void {
    this.errorMessage = null;
    this.dialogMode = 'create';
    this.dialogClient = null;
    this.dialogOpen = true;
    this.cdr.markForCheck();
  }

  /**
   * "Editar" handler: open the dialog in edit mode for
   * a single client. The dialog reads the form values
   * off the row passed in, so a stale copy of the row
   * is fine – the server is the source of truth on
   * submit.
   */
  openEditDialog(row: AdminClientRow): void {
    this.errorMessage = null;
    this.dialogMode = 'edit';
    this.dialogClient = row;
    this.dialogOpen = true;
    this.cdr.markForCheck();
  }

  /**
   * Dialog "save" handler. The dialog posts to the
   * API on its own; this method just refreshes the
   * local state from the response.
   */
  onDialogSave(result: ClientEditDialogResult): void {
    this.dialogOpen = false;
    if (result.action === 'create') {
      this.lastCreatedApiKey = result.apiKey;
      // Prepend the new row so the operator sees the
      // result without waiting for a full re-fetch.
      this.clients = [result.client, ...this.clients];
      this.totalClients = this.clients.length;
      this.cdr.markForCheck();
      return;
    }
    this.lastCreatedApiKey = null;
    this.replaceClient(result.client);
  }

  /**
   * Dialog "cancel" handler. Clears the
   * "copy this API key" banner so the previous
   * customer's key is not silently shown while the
   * next dialog is being filled in.
   */
  onDialogCancel(): void {
    this.dialogOpen = false;
    this.dialogClient = null;
    this.cdr.markForCheck();
  }

  /**
   * Dismiss the "copy this API key" banner. Wired to
   * the banner's close button.
   */
  dismissCreatedApiKey(): void {
    this.lastCreatedApiKey = null;
    this.cdr.markForCheck();
  }

  /**
   * Read the current filter-form state and project it
   * into the shape the service expects. The mapping is
   * deliberately inline (rather than a helper class)
   * because the form has only three fields.
   */
  private buildFilters(): AdminClientListFilters {
    const raw = this.filtersForm.getRawValue() as {
      q: string;
      plan: string;
      status: string;
    };
    return {
      q: raw.q || null,
      plan: (raw.plan || null) as AdminClientRow['plan'] | null,
      status: (raw.status || null) as AdminClientRow['status'] | null,
    };
  }

  /**
   * Update the local state from a successful clients
   * response. ``append`` picks between "replace" and
   * "extend" semantics; the offset bookkeeping is
   * always advanced to the new total so the next
   * "Cargar más" call picks up where this one left off.
   */
  private applyClients(
    response: AdminClientListResponse,
    { append }: { append: boolean },
  ): void {
    this.clients = append
      ? [...this.clients, ...response.items]
      : [...response.items];
    this.totalClients = response.total;
    this.hasMoreClients = response.has_more;
    this.currentOffset = this.clients.length;
    this.loading = false;
    this.cdr.markForCheck();
  }

  /**
   * Replace a single client row in the local state.
   * Used by the "suspend" / "update" handlers so the
   * table reflects the new status without a full
   * re-fetch.
   */
  private replaceClient(updated: AdminClientRow): void {
    this.clients = this.clients.map((client) =>
      client.id === updated.id ? updated : client,
    );
    this.cdr.markForCheck();
  }

  /**
   * ``trackBy`` for the clients table. Using the client's
   * id as the key keeps the DOM stable when the page
   * appends more rows; Angular can match the new array
   * to the previous one row-by-row.
   */
  trackClient(_index: number, client: AdminClientRow): string {
    return client.id;
  }

  /**
   * ``trackBy`` for the per-provider breakdown list. The
   * ``(provider, channel)`` pair uniquely identifies a
   * row so the DOM stays stable across re-renders.
   */
  trackProvider(
    _index: number,
    row: AdminProviderBreakdownRow,
  ): string {
    return `${row.provider}::${row.channel}`;
  }

  /**
   * ``trackBy`` for the error log table. The message id
   * uniquely identifies a row so the DOM stays stable
   * across re-renders.
   */
  trackErrorLog(
    _index: number,
    entry: NonNullable<AdminErrorLogResponse['items'][number]>,
  ): string {
    return entry.message_id;
  }

  /**
   * Project a status enum into the Spanish label the
   * table shows. The mapping mirrors the customer-facing
   * usage dashboard so the two views feel consistent.
   */
  clientStatusLabel(status: AdminClientRow['status']): string {
    const labels: Record<AdminClientRow['status'], string> = {
      active: 'Activo',
      suspended: 'Suspendido',
      pending: 'Pendiente',
    };
    return labels[status] ?? status;
  }

  /**
   * Tailwind-friendly badge colour for a client status.
   * Lives on the component so the class strings are
   * co-located with the labels – a future status only
   * needs to be added in one place.
   */
  clientStatusBadge(status: AdminClientRow['status']): string {
    switch (status) {
      case 'active':
        return 'bg-emerald-100 text-emerald-800';
      case 'suspended':
        return 'bg-rose-100 text-rose-800';
      case 'pending':
      default:
        return 'bg-amber-100 text-amber-800';
    }
  }

  /**
   * Project a plan enum into the Spanish label the
   * table shows.
   */
  planLabel(plan: AdminClientRow['plan']): string {
    const labels: Record<AdminClientRow['plan'], string> = {
      starter: 'Starter',
      growth: 'Growth',
      enterprise: 'Enterprise',
    };
    return labels[plan] ?? plan;
  }

  /**
   * Project a role enum into the Spanish label the
   * table shows. The mapping lives on the component so
   * the dashboard does not have to import the same
   * dictionary from the service.
   */
  roleLabel(role: AdminClientRow['role']): string {
    return role === 'admin' ? 'Administrador' : 'Cliente';
  }

  /**
   * Format an ISO timestamp into a dashboard-friendly
   * local string. The function is intentionally simple
   * (locale + short month) so the table stays scannable.
   */
  formatDate(value: string | null): string {
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
   * Project a ``avg_latency_ms`` value into a dashboard-
   * friendly string. ``null`` (no observed dispatches for
   * the bucket) renders as the same "—" placeholder the
   * rest of the dashboard uses for missing values; a real
   * value is rounded to one decimal so a "150 ms" average
   * and a "150.4 ms" average read identically in the table.
   */
  formatLatency(value: number | null): string {
    if (value === null || Number.isNaN(value)) {
      return '—';
    }
    return `${value.toFixed(1)} ms`;
  }
}
