// 构建产物放进 Python 包，由 litgleaner serve 提供。
// 开发时：先运行 litgleaner serve --no-open，把它打印的地址设为 LITGLEANER_API，
// 再运行 pnpm dev，用 http://localhost:5173/launch?token=... 打开。
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const api = process.env.LITGLEANER_API ?? "http://127.0.0.1:8765";

const proxy = {
  target: api,
  changeOrigin: true,
  // 服务只接受本机 Origin，开发服务器转发时改成服务自己的地址
  headers: { origin: api },
};

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/litgleaner/server/static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },
  server: {
    proxy: { "/api": proxy, "/launch": proxy },
  },
});
