import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

/**
 * Top-level routes.
 *
 * The "Historial y consumo" feature is the first
 * dashboard screen to ship; the path is mounted under
 * ``/usage`` so a future rebrand ("Mensajes", "Reportes",
 * …) does not have to touch every internal link. The
 * landing page (``/``) is reserved for the project
 * overview; individual feature screens live one level
 * down so the navigation stays flat.
 *
 * The feature module is referenced exclusively through
 * :func:`loadChildren` so the dashboard's code ships
 * as its own lazy chunk. Importing the module eagerly
 * here (e.g. to declare it in ``imports``) would defeat
 * the code-splitting and inflate the initial bundle.
 */
const routes: Routes = [
  {
    path: 'usage',
    loadChildren: () =>
      import('./usage-dashboard/usage-dashboard.module').then(
        (m) => m.UsageDashboardModule,
      ),
  },
  {
    path: '',
    pathMatch: 'full',
    redirectTo: 'usage',
  },
];

@NgModule({
  imports: [RouterModule.forRoot(routes)],
  exports: [RouterModule],
})
export class AppRoutingModule {}
