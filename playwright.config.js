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
  // capture_*.spec.js はテストではなく操作説明書用のスクリーンショット撮影で、
  // 何も検証しないため通常のスイートからは外す。撮り直すときは
  // playwright.capture.config.js を使うこと（testIgnore は実行時にファイルを
  // 明示しても効くので、この設定のままでは撮影できない）。
  testIgnore: '**/capture_*.spec.js',
  timeout: 30000,
  retries: 1,
  // 直列実行。全テストが単一の SQLite（shift_e2e.db）と単一の Flask サーバを
  // 共有しているため、並列だと書き込みが競合してテストが不安定になる。
  // 実測: 並列だと 54 passed + 2 flaky、悪いときは 8 failed。
  //       直列なら 56 passed で完全に安定（複数回で確認）。
  // 代償は実行時間（約30秒 → 約1.5分）だが、偽の失敗を追う時間の方が高くつく。
  // 並列に戻すなら、テストごとに DB を分離する仕組みが先に必要。
  workers: 1,
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
