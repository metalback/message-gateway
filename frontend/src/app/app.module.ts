import { NgModule } from '@angular/core';
import { BrowserModule } from '@angular/platform-browser';
import { HttpClientModule } from '@angular/common/http';

import { AppComponent } from './app.component';

/**
 * Root NgModule.
 *
 * The PRD calls for NgModules, so we expose `AppModule` even though
 * `bootstrapApplication` only needs the component. Feature modules
 * will import `HttpClientModule` / `BrowserModule` from here to keep
 * the dependency graph explicit.
 */
@NgModule({
  declarations: [AppComponent],
  imports: [BrowserModule, HttpClientModule],
  exports: [AppComponent],
})
export class AppModule {}
