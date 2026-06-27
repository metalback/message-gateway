import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpClientTestingModule } from '@angular/common/http/testing';
import { ReactiveFormsModule } from '@angular/forms';
import { RouterTestingModule } from '@angular/router/testing';
import { of, throwError } from 'rxjs';

import { AdminDashboardComponent } from './admin-dashboard.component';
import { AdminDashboardService } from '../../services/admin-dashboard.service';
import {
  AdminClientListResponse,
  AdminErrorLogResponse,
  AdminOverview,
  AdminProviderBreakdownRow,
} from '../../models/admin-dashboard.types';

/**
 * HTTP-level tests for the admin dashboard component.
 *
 * The tests stub :class:`AdminDashboardService` so the
 * component is exercised in isolation; the service-level
 * contract is covered by
 * :mod:`services/admin-dashboard.service.spec.ts`.
 */
describe('AdminDashboardComponent', () => {
  let component: AdminDashboardComponent;
  let fixture: ComponentFixture<AdminDashboardComponent>;
  let serviceStub: jasmine.SpyObj<AdminDashboardService>;

  const SAMPLE_OVERVIEW: AdminOverview = {
    period_start: '2026-06-01',
    period_end: '2026-06-30',
    total_clients: 1,
    active_clients: 1,
    suspended_clients: 0,
    pending_clients: 0,
    admin_users: 0,
    total_messages: 0,
    billable_messages: 0,
    delivered_messages: 0,
    failed_messages: 0,
    pending_messages: 0,
    total_revenue_clp: 0,
  };

  const SAMPLE_CLIENTS: AdminClientListResponse = {
    items: [
      {
        id: 'row-1',
        name: 'Acme SpA',
        email: 'ops@acme.cl',
        rut: '12345678-5',
        plan: 'starter',
        status: 'active',
        role: 'client',
        api_key_last4: '7a3f',
        markup_percent: 0.0,
        markup_fixed_clp: 0,
        created_at: '2026-06-15T10:00:00+00:00',
        updated_at: null,
      },
    ],
    total: 1,
    limit: 50,
    offset: 0,
    has_more: false,
  };

  const SAMPLE_BREAKDOWN: ReadonlyArray<AdminProviderBreakdownRow> = [
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

  const SAMPLE_LOG: AdminErrorLogResponse = {
    items: [],
    total: 0,
    limit: 50,
    offset: 0,
    has_more: false,
  };

  beforeEach(async () => {
    serviceStub = jasmine.createSpyObj<AdminDashboardService>('AdminDashboardService', [
      'getOverview',
      'listClients',
      'getProviderBreakdown',
      'listErrorLog',
      'suspendClient',
      'describeError',
    ]);
    serviceStub.getOverview.and.returnValue(of(SAMPLE_OVERVIEW));
    serviceStub.listClients.and.returnValue(of(SAMPLE_CLIENTS));
    serviceStub.getProviderBreakdown.and.returnValue(of(SAMPLE_BREAKDOWN));
    serviceStub.listErrorLog.and.returnValue(of(SAMPLE_LOG));
    serviceStub.describeError.and.returnValue('boom');

    await TestBed.configureTestingModule({
      declarations: [AdminDashboardComponent],
      imports: [HttpClientTestingModule, ReactiveFormsModule, RouterTestingModule],
      providers: [{ provide: AdminDashboardService, useValue: serviceStub }],
    }).compileComponents();

    fixture = TestBed.createComponent(AdminDashboardComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
  });

  it('renders the overview tiles after the first paint', () => {
    expect(component.overview).toEqual(SAMPLE_OVERVIEW);
    expect(component.clients.length).toBe(1);
    expect(component.providerBreakdown).toEqual(SAMPLE_BREAKDOWN);
    expect(component.errorLog).toEqual(SAMPLE_LOG);
    expect(component.initialLoadDone).toBeTrue();
    expect(component.errorMessage).toBeNull();
  });

  it('surfaces a stable error message when the initial load fails', () => {
    // Recreate the component so the failing stubs are in
    // place *before* ``ngOnInit`` fires the initial forkJoin.
    // Reaching into ``component.loadInitial`` directly would
    // require exposing a private hook purely for tests; the
    // public init lifecycle is enough to exercise the error
    // path.
    TestBed.resetTestingModule();
    const failingStub = jasmine.createSpyObj<AdminDashboardService>(
      'AdminDashboardService',
      [
        'getOverview',
        'listClients',
        'getProviderBreakdown',
        'listErrorLog',
        'suspendClient',
        'describeError',
      ],
    );
    failingStub.getOverview.and.returnValue(throwError(() => new Error('boom')));
    failingStub.listClients.and.returnValue(throwError(() => new Error('boom')));
    failingStub.getProviderBreakdown.and.returnValue(
      throwError(() => new Error('boom')),
    );
    failingStub.listErrorLog.and.returnValue(throwError(() => new Error('boom')));
    failingStub.describeError.and.returnValue('boom');

    TestBed.configureTestingModule({
      declarations: [AdminDashboardComponent],
      imports: [HttpClientTestingModule, ReactiveFormsModule, RouterTestingModule],
      providers: [{ provide: AdminDashboardService, useValue: failingStub }],
    }).compileComponents();

    const failingFixture = TestBed.createComponent(AdminDashboardComponent);
    const failingComponent = failingFixture.componentInstance;
    failingFixture.detectChanges();

    expect(failingComponent.errorMessage).toBe('boom');
    expect(failingComponent.initialLoadDone).toBeTrue();
  });

  it('suspends a client and replaces the row in place', () => {
    const updated = {
      ...SAMPLE_CLIENTS.items[0],
      status: 'suspended' as const,
    };
    serviceStub.suspendClient.and.returnValue(
      of({ client: updated, suspended_at: '2026-06-15T12:00:00+00:00' }),
    );
    component.suspend(SAMPLE_CLIENTS.items[0]);
    expect(serviceStub.suspendClient).toHaveBeenCalledWith('row-1');
    expect(component.clients[0].status).toBe('suspended');
  });

  it('projects a status enum into a Spanish label', () => {
    expect(component.clientStatusLabel('active')).toBe('Activo');
    expect(component.clientStatusLabel('suspended')).toBe('Suspendido');
    expect(component.clientStatusLabel('pending')).toBe('Pendiente');
  });

  it('returns a Tailwind-friendly badge colour for every status', () => {
    expect(component.clientStatusBadge('active')).toContain('emerald');
    expect(component.clientStatusBadge('suspended')).toContain('rose');
    expect(component.clientStatusBadge('pending')).toContain('amber');
  });

  it('projects a plan enum into a Spanish label', () => {
    expect(component.planLabel('starter')).toBe('Starter');
    expect(component.planLabel('growth')).toBe('Growth');
    expect(component.planLabel('enterprise')).toBe('Enterprise');
  });

  it('projects a role enum into a Spanish label', () => {
    expect(component.roleLabel('admin')).toBe('Administrador');
    expect(component.roleLabel('client')).toBe('Cliente');
  });

  it('formats an ISO timestamp into a dashboard-friendly string', () => {
    const formatted = component.formatDate('2026-06-15T10:00:00+00:00');
    expect(formatted).not.toBe('—');
    expect(formatted).not.toBe('2026-06-15T10:00:00+00:00');
  });

  it('returns a dash for an unparseable timestamp', () => {
    expect(component.formatDate(null)).toBe('—');
    expect(component.formatDate('')).toBe('—');
  });
});
