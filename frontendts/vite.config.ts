import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => ({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@mundi/ee': path.resolve(__dirname, './src/lib/ee-stub.tsx'),
    },
    dedupe: [
      'react',
      'react-dom',
      // Keep a single luma.gl / deck.gl instance for the app's dynamic @deck.gl
      // imports. Two copies cause luma.gl to throw "already loaded", which then
      // surfaces as a spurious "__publicField is not defined" MapLibre error event.
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
    watch: {
      ignored: ['**/opensrc/**'],
    },
    proxy: {
      '/api': { target: 'http://localhost:8000', ws: true, changeOrigin: true },
      '/auth': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    // ES2022 keeps class fields native. Lower targets compile them into an
    // esbuild `__publicField` helper that MapLibre's worker (built from its
    // own source text) does not have, so maps using that worker failed with
    // "__publicField is not defined" (minified "yr is not defined") and drew
    // nothing (the /rwanda dashboard map, 2026-10-04).
    target: 'es2022',
    sourcemap: mode === 'development',
    chunkSizeWarningLimit: 1000,
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
          ui: ['@radix-ui/react-dialog', '@radix-ui/react-dropdown-menu'],
        },
      },
    },
  },
  optimizeDeps: {
    esbuildOptions: { target: 'es2022' }, // same reason as build.target
    entries: ['index.html'],
    include: ['react-router-dom'],
  },
}))
