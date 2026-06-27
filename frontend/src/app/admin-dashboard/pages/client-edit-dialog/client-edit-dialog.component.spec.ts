import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ReactiveFormsModule, Validators } from '@angular/forms';
import { SimpleChange } from '@angular/core';
import { of, throwError } from 'rxjs';

import { AdminDashboardService } from '../../services/admin-dashboard.service';
import {
  AdminClientRow,
  AdminClientUpdateResponse,
  AdminCreateClientResponse,
  AdminUpdateClientRequest,
} from '../../models/admin-dashboard.types';
import {
  ClientEditDialogComponent,
  ClientEditDialogResult,
} from './client-edit-dialog.component';

/**
 * Build a fully-populated :class:`AdminClientRow` so
 * the tests can override only the fields they care
 * about. Defaults are deliberately valid so a test
 * that does not care about a field gets a usable
 * fixture.
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

describe('ClientEditDialogComponent', () => {
  let component: ClientEditDialogComponent;
  let fixture: ComponentFixture<ClientEditDialogComponent>;
  let serviceStub: jasmine.SpyObj<AdminDashboardService>;

  beforeEach(async () => {
    serviceStub = jasmine.createSpyObj<AdminDashboardService>('AdminDashboardService', [
      'createClient',
      'updateClient',
      'describeError',
    ]);
    serviceStub.describeError.and.callFake((err: unknown) => {
      if (err instanceof Error) {
        return err.message;
      }
      return 'boom';
    });

    await TestBed.configureTestingModule({
      declarations: [ClientEditDialogComponent],
      imports: [ReactiveFormsModule],
      providers: [{ provide: AdminDashboardService, useValue: serviceStub }],
    }).compileComponents();

    fixture = TestBed.createComponent(ClientEditDialogComponent);
    component = fixture.componentInstance;
    // No ``detectChanges`` yet: the dialog is closed by
    // default and we drive the form state directly from
    // the component API to keep the assertions tight.
  });

  it('creates the component', () => {
    expect(component).toBeTruthy();
  });

  describe('create mode', () => {
    beforeEach(() => {
      component.mode = 'create';
      component.open = true;
      component.ngOnChanges({
        open: new SimpleChange(false, true, true),
        mode: new SimpleChange(undefined, 'create', true),
      });
      fixture.detectChanges();
    });

    it('requires name, email, RUT and password', () => {
      const required = Validators.required;
      expect(component.form.controls['name'].hasValidator(required)).toBe(true);
      expect(component.form.controls['email'].hasValidator(required)).toBe(true);
      expect(component.form.controls['rut'].hasValidator(required)).toBe(true);
      expect(component.form.controls['password'].hasValidator(required)).toBe(true);
      component.form.patchValue({
        name: '',
        email: '',
        rut: '',
        password: '',
      });
      expect(component.form.invalid).toBe(true);
    });

    it('rejects an invalid email without touching the service', () => {
      component.form.patchValue({
        name: 'Acme SpA',
        email: 'not-an-email',
        rut: '12345678-5',
        password: 'sup3r-secret',
      });
      expect(component.form.invalid).toBe(true);
      component.onSubmit();
      expect(serviceStub.createClient).not.toHaveBeenCalled();
    });

    it('POSTs to /v1/admin/clients and emits a "create" result', () => {
      const row = makeRow({ id: 'row-new' });
      const response: AdminCreateClientResponse = {
        client: row,
        api_key: 'mgw_live_abcdef1234567890',
        api_key_last4: '7890',
      };
      serviceStub.createClient.and.returnValue(of(response));

      component.form.patchValue({
        name: '  Acme SpA  ',
        email: 'ops@acme.cl',
        rut: '12345678-5',
        password: 'sup3r-secret',
        plan: 'growth',
        markup_percent: 0.1,
        markup_fixed_clp: 5,
      });
      const results: ClientEditDialogResult[] = [];
      component.submitted.subscribe((value) => results.push(value));
      component.onSubmit();

      expect(serviceStub.createClient).toHaveBeenCalledTimes(1);
      const payload = serviceStub.createClient.calls.mostRecent().args[0];
      expect(payload).toEqual({
        name: 'Acme SpA', // trimmed
        email: 'ops@acme.cl',
        rut: '12345678-5',
        password: 'sup3r-secret',
        plan: 'growth',
      });
      expect(results.length).toBe(1);
      expect(results[0].action).toBe('create');
      const createResult = results[0] as Extract<ClientEditDialogResult, { action: 'create' }>;
      expect(createResult.apiKey).toBe('mgw_live_abcdef1234567890');
      expect(createResult.client).toEqual(row);
      expect(component.createdApiKey).toBe('mgw_live_abcdef1234567890');
      expect(component.errorMessage).toBeNull();
    });

    it('surfaces the API error message on a failing create', () => {
      serviceStub.createClient.and.returnValue(
        throwError(() => new Error('duplicate identity')),
      );
      serviceStub.describeError.and.returnValue('duplicate_identity: email already used');
      component.form.patchValue({
        name: 'Acme SpA',
        email: 'ops@acme.cl',
        rut: '12345678-5',
        password: 'sup3r-secret',
      });
      component.onSubmit();
      expect(serviceStub.createClient).toHaveBeenCalledTimes(1);
      expect(component.errorMessage).toBe('duplicate_identity: email already used');
      expect(component.submitting).toBeFalse();
    });
  });

  describe('edit mode', () => {
    beforeEach(() => {
      component.mode = 'edit';
      component.client = makeRow({
        id: 'row-7',
        name: 'Acme SpA',
        plan: 'starter',
        status: 'active',
        markup_percent: 0.0,
        markup_fixed_clp: 0,
      });
      component.open = true;
      component.ngOnChanges({
        open: new SimpleChange(false, true, true),
        mode: new SimpleChange(undefined, 'edit', true),
        client: new SimpleChange(null, component.client, true),
      });
      fixture.detectChanges();
    });

    it('pre-fills the form with the current row values', () => {
      expect(component.form.value).toEqual(
        jasmine.objectContaining({
          name: 'Acme SpA',
          plan: 'starter',
          status: 'active',
          markup_percent: 0.0,
          markup_fixed_clp: 0,
        }),
      );
    });

    it('does not require email / RUT / password in edit mode', () => {
      component.form.patchValue({
        name: 'Acme SpA',
        email: '',
        rut: '',
        password: '',
      });
      expect(component.form.valid).toBe(true);
    });

    it('PATCHes the row and emits an "update" result', () => {
      const updated = makeRow({
        id: 'row-7',
        name: 'Acme SpA',
        plan: 'enterprise',
        status: 'active',
        markup_percent: 0.25,
        markup_fixed_clp: 10,
      });
      const response: AdminClientUpdateResponse = { client: updated };
      serviceStub.updateClient.and.returnValue(of(response));

      component.form.patchValue({
        name: 'Acme SpA',
        plan: 'enterprise',
        status: 'active',
        markup_percent: 0.25,
        markup_fixed_clp: 10,
      });
      const results: ClientEditDialogResult[] = [];
      component.submitted.subscribe((value) => results.push(value));
      component.onSubmit();

      expect(serviceStub.updateClient).toHaveBeenCalledTimes(1);
      const [clientId, payload] = serviceStub.updateClient.calls.mostRecent().args;
      expect(clientId).toBe('row-7');
      const expectedPayload: AdminUpdateClientRequest = {
        name: 'Acme SpA',
        plan: 'enterprise',
        status: 'active',
        markup_percent: 0.25,
        markup_fixed_clp: 10,
      };
      expect(payload).toEqual(expectedPayload);
      expect(results.length).toBe(1);
      expect(results[0].action).toBe('update');
      const updateResult = results[0] as Extract<ClientEditDialogResult, { action: 'update' }>;
      expect(updateResult.clientId).toBe('row-7');
      expect(updateResult.client).toEqual(updated);
    });

    it('surfaces the API error message on a failing update', () => {
      serviceStub.updateClient.and.returnValue(
        throwError(() => new Error('invalid_markup_percent')),
      );
      serviceStub.describeError.and.returnValue('invalid_markup_percent: out of range');
      component.form.patchValue({
        name: 'Acme SpA',
        plan: 'starter',
        status: 'active',
        markup_percent: 0.5,
        markup_fixed_clp: 0,
      });
      component.onSubmit();
      expect(component.errorMessage).toBe('invalid_markup_percent: out of range');
      expect(component.submitting).toBeFalse();
    });
  });

  describe('cancel', () => {
    it('emits "cancelled" when the user dismisses the dialog', () => {
      component.open = true;
      fixture.detectChanges();
      const calls: void[] = [];
      component.cancelled.subscribe(() => calls.push(undefined));
      component.onCancel();
      expect(calls.length).toBe(1);
    });

    it('does not emit "cancelled" while a request is in flight', () => {
      component.open = true;
      component.submitting = true;
      const calls: void[] = [];
      component.cancelled.subscribe(() => calls.push(undefined));
      component.onCancel();
      expect(calls.length).toBe(0);
    });
  });

  describe('labels', () => {
    it('maps plan enums to Spanish labels', () => {
      expect(component.planLabel('starter')).toBe('Starter');
      expect(component.planLabel('growth')).toBe('Growth');
      expect(component.planLabel('enterprise')).toBe('Enterprise');
    });

    it('maps status enums to Spanish labels', () => {
      expect(component.statusLabel('active')).toBe('Activo');
      expect(component.statusLabel('suspended')).toBe('Suspendido');
      expect(component.statusLabel('pending')).toBe('Pendiente');
    });
  });
});
