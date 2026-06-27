import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
} from '@angular/core';
import { FormBuilder, FormGroup, Validators } from '@angular/forms';

import {
  AdminClientPlan,
  AdminClientRow,
  AdminClientStatus,
  AdminCreateClientRequest,
  AdminUpdateClientRequest,
} from '../../models/admin-dashboard.types';
import { AdminDashboardService } from '../../services/admin-dashboard.service';

/**
 * Result emitted by :class:`ClientEditDialogComponent` when the
 * operator submits the form successfully.
 *
 * The shape is a tagged union so the parent component can
 * branch on the action without an extra boolean flag:
 *
 * - ``create`` carries the full
 *   :class:`AdminCreateClientRequest` plus the plain API key
 *   the backend returned once.
 * - ``update`` carries the
 *   :class:`AdminUpdateClientRequest` payload alongside the
 *   id of the row that was changed.
 */
export type ClientEditDialogResult =
  | {
      readonly action: 'create';
      readonly payload: AdminCreateClientRequest;
      readonly apiKey: string;
      readonly client: AdminClientRow;
    }
  | {
      readonly action: 'update';
      readonly clientId: string;
      readonly payload: AdminUpdateClientRequest;
      readonly client: AdminClientRow;
    };

/**
 * "Crear / Editar cliente" dialog used by the
 * "Admin · Gestión de clientes y métricas" dashboard
 * (issue #10).
 *
 * The component backs the dashboard's "Configuración de
 * precios" and "crear / editar / cambiar plan" actions the
 * issue calls out. The HTTP surface is the existing
 * :class:`AdminDashboardService`; the dialog is the form
 * layer that maps the operator's input into the request
 * payload the backend expects.
 *
 * The component is a controlled dialog (no Angular Material
 * dependency) rendered in place by the dashboard parent;
 * :attr:`open` toggles visibility, the form is local to
 * this component and the parent receives a single
 * :class:`ClientEditDialogResult` on success.
 */
@Component({
  selector: 'app-client-edit-dialog',
  standalone: false,
  templateUrl: './client-edit-dialog.component.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ClientEditDialogComponent implements OnChanges {
  /**
   * Whether the dialog is visible. The parent owns the
   * ``open`` state so the dialog can be dismissed from
   * the outside (e.g. via a "Cancelar" link in the
   * breadcrumb).
   */
  @Input() open = false;

  /**
   * Dialog mode. ``'create'`` reveals the email / RUT /
   * password fields and posts to
   * ``POST /v1/admin/clients``; ``'edit'`` hides them and
   * patches the row in place via
   * ``PATCH /v1/admin/clients/{id}``.
   */
  @Input() mode: 'create' | 'edit' = 'create';

  /**
   * Row being edited. ``null`` (the default) means
   * "create" – the parent should set this input only when
   * :attr:`mode` is ``'edit'``.
   */
  @Input() client: AdminClientRow | null = null;

  /** Emitted when the operator dismisses the dialog without saving. */
  @Output() cancelled = new EventEmitter<void>();

  /** Emitted when the operator successfully saves the form. */
  @Output() submitted = new EventEmitter<ClientEditDialogResult>();

  /**
   * Reactive form backing the dialog. The fields
   * toggled by the ``mode`` input are validated on
   * the fly so a missing required field in "create"
   * mode (e.g. password) blocks the submit.
   */
  readonly form: FormGroup;

  /**
   * Human-readable error from the most recent failed
   * request, or ``null`` when the last call succeeded.
   */
  errorMessage: string | null = null;

  /** ``True`` while a create / update request is in flight. */
  submitting = false;

  /** The plain API key returned by a successful create. */
  private lastCreatedApiKey: string | null = null;

  /** Available plan options for the ``plan`` select. */
  readonly plans: ReadonlyArray<AdminClientPlan> = [
    'starter',
    'growth',
    'enterprise',
  ];

  /** Available status options for the ``status`` select (edit only). */
  readonly statuses: ReadonlyArray<AdminClientStatus> = [
    'active',
    'suspended',
    'pending',
  ];

  constructor(
    private readonly fb: FormBuilder,
    private readonly service: AdminDashboardService,
    private readonly cdr: ChangeDetectorRef,
  ) {
    // The form is created once and re-initialised on
    // every ``ngOnChanges`` so the dialog can be reused
    // for several clients without leaking state between
    // them. The validators mirror the Pydantic
    // constraints in :mod:`app.routes.admin` so the user
    // gets the same 422 on the wire as the inline
    // ``describeError`` renders.
    this.form = this.fb.nonNullable.group({
      name: ['', [Validators.required, Validators.maxLength(200)]],
      email: ['', [Validators.required, Validators.email]],
      rut: ['', [Validators.required, Validators.maxLength(20)]],
      password: ['', [Validators.required, Validators.maxLength(255)]],
      plan: ['starter' as AdminClientPlan, Validators.required],
      status: ['active' as AdminClientStatus, Validators.required],
      markup_percent: [
        0,
        [Validators.min(0), Validators.max(10)],
      ],
      markup_fixed_clp: [0, [Validators.min(0), Validators.max(1_000_000)]],
    });
  }

  ngOnChanges(changes: SimpleChanges): void {
    // Re-initialise the form every time the parent
    // opens the dialog so a stale value from a previous
    // edit does not leak into the next one.
    if (changes['open'] || changes['mode'] || changes['client']) {
      this.errorMessage = null;
      this.lastCreatedApiKey = null;
      this.submitting = false;
      if (this.open) {
        this.resetForm();
      }
    }
  }

  /**
   * Re-seed the form from the dialog inputs. ``create``
   * mode opens a blank form with the default plan;
   * ``edit`` mode pre-fills every mutable field.
   */
  private resetForm(): void {
    if (this.mode === 'edit' && this.client) {
      this.form.reset({
        name: this.client.name,
        email: this.client.email,
        rut: this.client.rut,
        password: '',
        plan: this.client.plan,
        status: this.client.status,
        markup_percent: this.client.markup_percent,
        markup_fixed_clp: this.client.markup_fixed_clp,
      });
    } else {
      this.form.reset({
        name: '',
        email: '',
        rut: '',
        password: '',
        plan: 'starter',
        status: 'active',
        markup_percent: 0,
        markup_fixed_clp: 0,
      });
    }
    // ``Validators.required`` is toggled on the create-only
    // fields based on the mode so a blank email / RUT
    // / password does not block an "edit" submit.
    const emailControl = this.form.controls['email'];
    const rutControl = this.form.controls['rut'];
    const passwordControl = this.form.controls['password'];
    if (this.mode === 'create') {
      emailControl.setValidators([Validators.required, Validators.email]);
      rutControl.setValidators([Validators.required, Validators.maxLength(20)]);
      passwordControl.setValidators([
        Validators.required,
        Validators.maxLength(255),
      ]);
    } else {
      // In "edit" mode the email / RUT / password are
      // ignored server-side (the backend does not let
      // the operator rotate credentials through this
      // surface), so the form does not require them.
      emailControl.clearValidators();
      rutControl.clearValidators();
      passwordControl.clearValidators();
    }
    emailControl.updateValueAndValidity({ emitEvent: false });
    rutControl.updateValueAndValidity({ emitEvent: false });
    passwordControl.updateValueAndValidity({ emitEvent: false });
  }

  /**
   * Submit handler. Branches on :attr:`mode` and posts
   * to the matching service method. On success the
   * parent receives a :class:`ClientEditDialogResult`
   * and the dialog stays open until the parent flips
   * :attr:`open` to ``false``.
   */
  onSubmit(): void {
    if (this.submitting) {
      return;
    }
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    this.submitting = true;
    this.errorMessage = null;
    if (this.mode === 'create') {
      this.submitCreate();
    } else {
      this.submitUpdate();
    }
  }

  /** Fire ``POST /v1/admin/clients`` and emit the create result. */
  private submitCreate(): void {
    const raw = this.form.getRawValue() as {
      name: string;
      email: string;
      rut: string;
      password: string;
      plan: AdminClientPlan;
      markup_percent: number;
      markup_fixed_clp: number;
    };
    const payload: AdminCreateClientRequest = {
      name: raw.name.trim(),
      email: raw.email.trim(),
      rut: raw.rut.trim(),
      password: raw.password,
      plan: raw.plan,
    };
    this.service.createClient(payload).subscribe({
      next: (response) => {
        this.submitting = false;
        this.lastCreatedApiKey = response.api_key;
        this.cdr.markForCheck();
        this.submitted.emit({
          action: 'create',
          payload,
          apiKey: response.api_key,
          client: response.client,
        });
      },
      error: (err: unknown) => {
        this.submitting = false;
        this.errorMessage = this.service.describeError(err)
          ?? 'No se pudo crear el cliente. Intenta de nuevo.';
        this.cdr.markForCheck();
      },
    });
  }

  /**
   * Fire ``PATCH /v1/admin/clients/{id}`` and emit the
   * update result. The PATCH body only carries the
   * fields the operator actually edited; a field that
   * was not touched is sent as ``null`` so the
   * backend's ``is not None`` check treats it as a
   * no-op.
   */
  private submitUpdate(): void {
    if (!this.client) {
      this.errorMessage = 'No se encontró el cliente a editar.';
      this.submitting = false;
      this.cdr.markForCheck();
      return;
    }
    const raw = this.form.getRawValue() as {
      name: string;
      plan: AdminClientPlan;
      status: AdminClientStatus;
      markup_percent: number;
      markup_fixed_clp: number;
    };
    const payload: AdminUpdateClientRequest = {
      name: raw.name.trim() || null,
      plan: raw.plan,
      status: raw.status,
      markup_percent: raw.markup_percent,
      markup_fixed_clp: raw.markup_fixed_clp,
    };
    this.service.updateClient(this.client.id, payload).subscribe({
      next: (response) => {
        this.submitting = false;
        this.cdr.markForCheck();
        this.submitted.emit({
          action: 'update',
          clientId: this.client!.id,
          payload,
          client: response.client,
        });
      },
      error: (err: unknown) => {
        this.submitting = false;
        this.errorMessage = this.service.describeError(err)
          ?? 'No se pudo actualizar el cliente. Intenta de nuevo.';
        this.cdr.markForCheck();
      },
    });
  }

  /**
   * "Cancelar" handler. The parent owns the
   * visibility state so we just emit the event and
   * let it decide what to do.
   */
  onCancel(): void {
    if (this.submitting) {
      return;
    }
    this.cancelled.emit();
  }

  /**
   * Convenience predicate for the template – the
   * "create" form requires an email / RUT / password
   * to be valid before submit. The PATCH path does
   * not.
   */
  get isCreateMode(): boolean {
    return this.mode === 'create';
  }

  /**
   * Expose the most recent create API key so the
   * template can render a copy-to-clipboard prompt.
   * Returns ``null`` in edit mode (or before a
   * successful create) so the template can hide the
   * block.
   */
  get createdApiKey(): string | null {
    return this.lastCreatedApiKey;
  }

  /**
   * Format the plan enum into a Spanish label
   * mirroring the dashboard's table column.
   */
  planLabel(plan: AdminClientPlan): string {
    const labels: Record<AdminClientPlan, string> = {
      starter: 'Starter',
      growth: 'Growth',
      enterprise: 'Enterprise',
    };
    return labels[plan] ?? plan;
  }

  /** Spanish label for a status option. */
  statusLabel(status: AdminClientStatus): string {
    const labels: Record<AdminClientStatus, string> = {
      active: 'Activo',
      suspended: 'Suspendido',
      pending: 'Pendiente',
    };
    return labels[status] ?? status;
  }
}
