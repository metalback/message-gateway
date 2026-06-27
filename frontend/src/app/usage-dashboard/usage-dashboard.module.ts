import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { ReactiveFormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';

import { UsageDashboardRoutingModule } from './usage-dashboard-routing.module';
import { UsageDashboardComponent } from './pages/usage-dashboard/usage-dashboard.component';

/**
 * NgModule that wires the "Historial y consumo" feature.
 *
 * The module deliberately re-imports the small set of
 * Angular building blocks it needs
 * (:class:`CommonModule` for the structural directives
 * and pipes, :class:`ReactiveFormsModule` for the
 * filter form, :class:`RouterModule` for the
 * ``routerLink`` in the header) so the module is
 * self-contained: a future lazy load (see
 * ``loadChildren`` in :class:`AppRoutingModule`) does
 * not have to fight a hidden dependency on a parent
 * module.
 */
@NgModule({
  declarations: [UsageDashboardComponent],
  imports: [
    CommonModule,
    ReactiveFormsModule,
    RouterModule,
    UsageDashboardRoutingModule,
  ],
})
export class UsageDashboardModule {}
