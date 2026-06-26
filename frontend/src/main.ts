import { bootstrapApplication } from '@angular/platform-browser';

import { AppComponent } from './app/app.component';
import { appConfig } from './app/app.config';

// NgModule-style wiring is provided by `AppConfig` (see app.config.ts) so
// feature modules can still register providers via the `imports` array.
bootstrapApplication(AppComponent, appConfig).catch((err) => {
  // eslint-disable-next-line no-console
  console.error(err);
});
