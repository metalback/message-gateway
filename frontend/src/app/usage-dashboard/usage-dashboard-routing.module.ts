import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

import { UsageDashboardComponent } from './pages/usage-dashboard/usage-dashboard.component';

/**
 * Routes mounted under the "usage" feature.
 *
 * The PRD documents the dashboard as the "Historial y
 * consumo" view: a single page that shows the current
 * period's balance and the customer's message history.
 * New sub-views (e.g. a CSV export) land as siblings of
 * :class:`UsageDashboardComponent` – not as children –
 * so the page header does not have to be re-implemented
 * for every drill-down.
 */
const routes: Routes = [
  {
    path: '',
    component: UsageDashboardComponent,
    title: 'Historial y consumo · Message Gateway',
  },
];

@NgModule({
  imports: [RouterModule.forChild(routes)],
  exports: [RouterModule],
})
export class UsageDashboardRoutingModule {}
