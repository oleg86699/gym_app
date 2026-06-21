import adapter from '@sveltejs/adapter-static'
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte'

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),

  kit: {
    // SPA — fallback на index.html для клиентского роутинга.
    // Адекватно для админ-панели за авторизацией: SSR нам тут не нужен.
    adapter: adapter({
      fallback: 'index.html',
      strict: false,
    }),
    alias: {
      $lib: 'src/lib',
    },
  },
}

export default config
