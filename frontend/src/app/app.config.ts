import { ApplicationConfig, importProvidersFrom } from '@angular/core';
import { provideHttpClient, withFetch } from '@angular/common/http';
import { provideRouter, withComponentInputBinding } from '@angular/router';

import { AppRoutingModule } from './app-routing.module';

/**
 * Root providers for the standalone bootstrap.
 *
 * We deliberately keep feature wiring in NgModules (per the PRD) but
 * expose them to the standalone `AppComponent` via `importProvidersFrom`.
 */
export const appConfig: ApplicationConfig = {
  providers: [
    provideHttpClient(withFetch()),
    provideRouter([], withComponentInputBinding()),
    importProvidersFrom(AppRoutingModule),
  ],
};
