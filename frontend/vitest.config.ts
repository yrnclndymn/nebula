import { defineConfig } from 'vitest/config'

// Vitest config kept separate from vite.config.ts (#162). The pure-logic unit
// tests need no plugins or JSX transform (esbuild handles the TS), so this stays
// plugin-free — which also sidesteps the Vite 8 / Vitest 3 nested-vite plugin
// type mismatch that surfaces when the two configs share one file. It is
// deliberately outside the `tsc -b` project (tsconfig.node.json includes only
// vite.config.ts) so the production typecheck never sees vitest's vite copy.
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
})
