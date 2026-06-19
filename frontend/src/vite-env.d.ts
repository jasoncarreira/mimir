/// <reference types="vite/client" />

// Build-time mimir version injected by vite.config (define). Used as the app
// shell's BUILD-label fallback when the runtime bootstrap doesn't carry one.
declare const __APP_VERSION__: string;
