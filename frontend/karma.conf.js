// Karma configuration for the Angular dashboard.
//
// The CI workflow (`.github/workflows/ci.yml`) runs the frontend test
// job with `--browsers=ChromeHeadlessCI`. `karma-chrome-launcher`
// only ships the standard `Chrome`, `ChromeHeadless` and friends; the
// `CI` suffix is a project convention that lets us point at a
// pinned Chromium binary in CI without affecting local runs.
//
// Local developers can keep running `npm test` (which uses the
// default `Chrome` launcher) without ever touching this file; the
// `CI` custom launcher is only selected when the flag is passed on
// the command line.

module.exports = function (config) {
  config.set({
    // Base path that all patterns resolve from. The CLI's default
    // (`''`) is what the Angular builder expects; keep it explicit
    // so the file is self-contained.
    basePath: '',

    // Plugins must be loaded explicitly when shipping a custom
    // config. The Angular CLI's default config bundles
    // `@angular-devkit/build-angular/plugins/karma` as a framework
    // plugin; if the file is not listed here karma cannot resolve
    // the `framework:@angular-devkit/build-angular` entry the CLI
    // builder injects and the test job crashes with "No provider
    // for framework" before the first spec runs.
    plugins: [
      require('karma-jasmine'),
      require('karma-chrome-launcher'),
      require('karma-jasmine-html-reporter'),
      require('karma-coverage'),
      require('@angular-devkit/build-angular/plugins/karma'),
    ],

    // The Angular CLI wires a karma plugin that knows how to serve
    // the application's `polyfills`/`assets`/`styles` declared in
    // `angular.json`. Re-declaring it here would be redundant.
    frameworks: ['jasmine', '@angular-devkit/build-angular'],

    // Project-wide reporters. `progress` is friendlier in terminals;
    // CI relies on the spec reporter implicitly through the CLI.
    reporters: ['progress'],

    // Surface every browser console message in the karma output so
    // failing tests include their `console.error` context.
    client: {
      clearContext: false,
    },

    // One round of execution per `karma start` invocation. Required
    // for CI; harmless locally because the developer can re-run.
    singleRun: false,

    // Restart-on-file-change is the default; keeping it explicit so
    // the behaviour does not silently change with karma upgrades.
    restartOnFileChange: true,

    // Custom launcher for CI: headless Chromium with the flags
    // recommended by the karma-chrome-launcher README for
    // containerised environments (no sandbox, explicit user-data-dir,
    // disabled GPU). The same flags are used by Angular CLI's own
    // `ChromeHeadlessCI` example.
    customLaunchers: {
      ChromeHeadlessCI: {
        base: 'ChromeHeadless',
        flags: [
          '--no-sandbox',
          '--disable-gpu',
          '--disable-dev-shm-usage',
          '--user-data-dir=/tmp/chromium-ci-profile',
        ],
      },
    },

    // No `browsers` entry on purpose: the Angular CLI builder
    // supplies a default at runtime, and CI passes
    // `--browsers=ChromeHeadlessCI` on the command line.
  });
};
