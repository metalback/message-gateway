import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { AdminDashboardComponent } from './pages/admin-dashboard/admin-dashboard.component';

/**
 * Routes mounted under the "admin" feature.
 *
 * The PRD documents the dashboard as the "Gestión de
 * clientes y métricas" view: a single page that shows
 * the headline counters, the clients table, the
 * per-provider breakdown and the error log. Future
 * sub-views (a "client detail" drawer, a
 * "configuración de precios" page, …) land as siblings
 * of :class:`AdminDashboardComponent` so the page
 * header does not have to be re-implemented for every
 * drill-down.
 */
const routes: Routes = [
  {
    path: '',
    component: AdminDashboardComponent,
    title: 'Administración · Message Gateway',
  },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class AdminDashboardRoutingModule {}
