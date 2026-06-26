import { NgModule } from '@angular/core';
import { RouterModule, Routes } from '@angular/router';

// Routes will be populated in later tasks. We keep the module here so
// the bootstrap wiring exists from day one and adding a feature route
// is a one-line change.
const routes: Routes = [];

@NgModule({
  imports: [RouterModule.forRoot(routes)],
  exports: [RouterModule],
})
export class AppRoutingModule {}
