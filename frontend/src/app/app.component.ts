import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

/**
 * Root component.
 *
 * The component owns the document chrome (header + the
 * "ir al panel" link) and delegates the page body to
 * :class:`<router-outlet>`. The "Historial y consumo"
 * dashboard mounts as the first routed view; future
 * screens (API Keys, factura, etc.) will land as
 * siblings of the same outlet without further changes
 * to the root template.
 */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterLink, RouterLinkActive, RouterOutlet],
  template: `
    <header class="border-b border-slate-200 bg-white">
      <div class="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
        <a routerLink="/usage" class="flex items-center gap-2 text-brand-700">
          <span class="text-lg font-semibold">Message Gateway</span>
          <span class="hidden text-xs text-slate-500 sm:inline">
            · Plataforma CPaaS chilena
          </span>
        </a>
        <nav>
          <a
            routerLink="/usage"
            routerLinkActive="text-brand-700 font-semibold"
            [routerLinkActiveOptions]="{ exact: false }"
            class="text-sm font-medium text-slate-600 hover:text-slate-900"
          >
            Historial y consumo
          </a>
        </nav>
      </div>
    </header>

    <router-outlet />
  `,
})
export class AppComponent {}
