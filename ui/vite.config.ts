import { sveltekit } from '@sveltejs/kit/vite'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [sveltekit()],
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    // Vite сам ходит на API через nginx (same-origin), но если запускать UI
    // напрямую без nginx — раскомментировать прокси:
    // proxy: {
    //   '/admin/api': { target: 'http://app:8080', changeOrigin: true },
    //   '/api/v1':    { target: 'http://app:8080', changeOrigin: true },
    // },
  },
  test: {
    include: ['src/**/*.{test,spec}.{js,ts}'],
  },
})
