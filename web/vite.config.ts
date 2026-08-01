import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// Kindred 只读可视化前端构建配置。
//
// dev（server）与 preview 都把 /api 代理到后端 kindred serve：规避跨端口 CORS，
// 且生产同源部署时前端 fetch('/api/...') 同样成立，前端代码零改动。
//
// 端口（N-1）：后端 `kindred serve` 默认 8787（见 src/kindred/cli.py），不是写死的
// 8000。代理目标用环境变量 VITE_KINDRED_API_TARGET 覆盖，默认对齐 8787，避免默认
// 联调路径（kindred serve + pnpm dev）打到错误端口进网络错误态。
const DEFAULT_API_TARGET = 'http://127.0.0.1:8787'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.VITE_KINDRED_API_TARGET || DEFAULT_API_TARGET

  const apiProxy = {
    '/api': {
      target: apiTarget,
      changeOrigin: true,
      rewrite: (p: string) => p.replace(/^\/api/, ''),
    },
  }

  // host / allowedHosts（N-5）：默认不放行任意 Host。web 层读的是个人 life state，
  // 即使只读也不该默认把 host 校验全开。仅当显式设置 VITE_PREVIEW_HOST（如 CloudIDE
  // 场景）时才监听 0.0.0.0 并放行对应网关域名；否则只听 localhost、用默认 host 校验。
  const previewHost = env.VITE_PREVIEW_HOST // 例：kindred-preview.example.net
  const exposed = Boolean(previewHost)

  return {
    plugins: [vue()],
    publicDir: false,
    server: {
      host: exposed ? '0.0.0.0' : 'localhost',
      port: 5173,
      proxy: apiProxy,
      ...(exposed ? { allowedHosts: [previewHost as string] } : {}),
    },
    preview: {
      host: exposed ? '0.0.0.0' : 'localhost',
      port: 5173,
      proxy: apiProxy,
      ...(exposed ? { allowedHosts: [previewHost as string] } : {}),
    },
    build: {
      outDir: '../src/kindred/web/static',
      emptyOutDir: true,
      sourcemap: false,
    },
  }
})
