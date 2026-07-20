// playwright.config.js - ShiftAI Playwright テスト設定
const { defineConfig } = require('@playwright/test');
const fs = require('fs');
const path = require('path');

// 各テスト実行前に e2e 用 DB を削除（スキーマを最新に保つため）
const E2E_DB = path.resolve(__dirname, 'shift_e2e.db');
try {
  if (fs.existsSync(E2E_DB)) fs.unlinkSync(E2E_DB);
} catch (e) { /* 無害 */ }

module.exports = defineConfig({
  testDir: './e2e',
  timeout: 30000,
  retries: 1,
  use: {
    baseURL: 'http://127.0.0.1:8000',
    headless: true,
    viewport: { width: 1280, height: 800 },
    actionTimeout: 8000,
    navigationTimeout: 10000,
    collectConsoleErrors: true,
    ignoreHTTPSErrors: true,
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'bash e2e/run_server.sh',
    port: 8000,
    timeout: 30000,
    reuseExistingServer: false,
    stderr: 'pipe',
    stdout: 'pipe',
  },
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
  reporter: [['list'], ['html', { open: 'never' }]],
});
