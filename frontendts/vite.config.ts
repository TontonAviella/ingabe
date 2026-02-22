import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import tailwindcss from '@tailwindcss/vite'
import type { Plugin } from 'vite'

/**
 * Stub optional deps that maplibre-gl-components dynamically imports
 * but which are not installed (they belong to excluded ControlGrid tools).
 * This prevents Vite's import-analysis from erroring on these dynamic imports
 * even when the source uses `/* @vite-ignore *\/`.
 */
const OPTIONAL_DEP_STUBS = ['shpjs', '@duckdb/duckdb-wasm', 'geotiff', 'geotiff.js', 'jspdf']

function stubOptionalDeps(): Plugin {
  const STUB_PREFIX = '\0stub:'
  return {
    name: 'stub-optional-maplibre-deps',
    resolveId(id) {
      if (OPTIONAL_DEP_STUBS.includes(id)) return STUB_PREFIX + id
    },
    load(id) {
      if (id.startsWith(STUB_PREFIX)) return 'export default {};\nexport const __esModule = true;\n'
    },
  }
}

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss(), stubOptionalDeps()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@mundi/ee': path.resolve(__dirname, './src/lib/ee-stub.tsx'),
    },
    dedupe: [
      'react',
      'react-dom',
      // Prevent duplicate luma.gl / deck.gl instances when maplibre-gl-components
      // bundles its own copy alongside the app's dynamic @deck.gl imports.
      // Two copies cause luma.gl to throw "already loaded", which then surfaces
      // as a spurious "__publicField is not defined" MapLibre error event.
      '@luma.gl/core',
      '@luma.gl/engine',
      '@luma.gl/webgl',
      '@luma.gl/shadertools',
      '@deck.gl/core',
      '@deck.gl/layers',
      '@deck.gl/mapbox',
    ],
  },
  base: '/',
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', ws: true, changeOrigin: true },
    },
  },
  build: {
    sourcemap: mode === 'development',
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      // Optional deps of maplibre-gl-components that we don't use.
      // Externalized so Vite doesn't fail when bundling unused dynamic chunks.
      external: ['shpjs', '@duckdb/duckdb-wasm', 'geotiff', 'geotiff.js', 'jspdf'],
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
          ui: ['@radix-ui/react-dialog', '@radix-ui/react-dropdown-menu'],
        },
      },
    },
  },
  optimizeDeps: {
    include: ['react-router-dom'],
  },
}))
