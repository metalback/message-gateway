import { CommonModule } from '@angular/common';
import { NgModule } from '@angular/core';
import { ReactiveFormsModule } from '@angular/forms';
import { RouterModule } from '@angular/router';

import { AdminDashboardRoutingModule } from './admin-dashboard-routing.module';
import { AdminDashboardComponent } from './pages/admin-dashboard/admin-dashboard.component';
import { ClientEditDialogComponent } from './pages/client-edit-dialog/client-edit-dialog.component';

/**
 * NgModule that wires the "Admin" feature (issue #10).
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
  declarations: [AdminDashboardComponent, ClientEditDialogComponent],
  imports: [
    CommonModule,
    ReactiveFormsModule,
    RouterModule,
    AdminDashboardRoutingModule,
  ],
})
export class AdminDashboardModule {}
