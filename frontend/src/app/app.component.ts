import { Component } from '@angular/core';

/**
 * Root component.
 *
 * Intentionally minimal: the dashboard layout, navigation, and
 * feature views will land in later tasks. This component is what
 * `bootstrapApplication` mounts into `<app-root>` (see `index.html`).
 */
@Component({
  selector: 'app-root',
  standalone: true,
  template: `
    <main class="mx-auto max-w-3xl px-6 py-12">
      <header class="mb-8">
        <h1 class="text-3xl font-semibold text-brand-700">Message Gateway</h1>
        <p class="mt-2 text-slate-600">
          Plataforma CPaaS chilena · API unificada de SMS y WhatsApp.
        </p>
      </header>

      <section class="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
        <h2 class="text-lg font-medium">Estado del scaffold</h2>
        <p class="mt-2 text-sm text-slate-600">
          El frontend Angular está conectado al backend FastAPI.
          Las pantallas de API Keys, mensajes y facturación se
          entregarán en tareas posteriores.
        </p>
      </section>
    </main>
  `,
})
export class AppComponent {}
