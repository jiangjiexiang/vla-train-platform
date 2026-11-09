import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    // 提交页现在要等镜像列表 + 参数模板两次请求再渲染动态表单，
    // 默认 5s 在 CI 慢机上不够用（实测个别用例接近 5s）。
    testTimeout: 20000,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
