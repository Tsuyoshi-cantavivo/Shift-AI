/**
 * e2e/helpers.js — Playwright テスト用 共通ヘルパ
 */

/**
 * API経由でシステム管理者を初期化（/api/init）。
 * 既に存在する場合は無害。
 */
async function ensureAdmin(request) {
  await request.post('/api/init');
}

/**
 * API経由で店舗+店舗責任者を作成。shop_code, manager_code を返す。
 * 既存の場合はスキップ。
 */
async function ensureShop(request, { shopCode, shopName, managerCode, managerPassword, managerName }) {
  // ログインして admin トークンを取得（admin/admin123）
  const loginRes = await request.post('/api/login', {
    data: {
      shop_code: 'admin',
      user_code: 'admin',
      password: 'admin123',
    },
  });
  let adminToken = null;
  if (loginRes.ok()) {
    adminToken = (await loginRes.json()).token;
  }
  if (!adminToken) {
    // /api/init を叩いてから再試行
    await ensureAdmin(request);
    const retry = await request.post('/api/login', {
      data: { shop_code: 'admin', user_code: 'admin', password: 'admin123' },
    });
    adminToken = (await retry.json()).token;
  }

  // 店舗作成（既存ならエラーになるので無視）
  await request.post('/api/admin/shops', {
    data: {
      shop_code: shopCode,
      shop_name: shopName,
      password: 'ShopPass1',
      manager_code: managerCode,
      manager_password: managerPassword,
      manager_name: managerName,
    },
    headers: { Authorization: `Bearer ${adminToken}` },
  });

  return { adminToken };
}

/**
 * システム管理者としてログイン。
 */
async function loginAsAdmin(page, request) {
  await ensureAdmin(request);
  await page.goto('/');
  await page.fill('#loginShopCode', 'admin');
  await page.fill('#loginUserCode', 'admin');
  await page.fill('#loginPassword', 'admin123');
  await page.click('#loginBtn');
  await page.waitForSelector('#appView:not(.d-none)', { timeout: 10000 });
}

/**
 * ブラウザで店舗管理者としてログイン。
 */
async function loginAsManager(page, { shopCode, managerCode, password }) {
  await page.goto('/');
  await page.fill('#loginShopCode', shopCode);
  await page.fill('#loginUserCode', managerCode);
  await page.fill('#loginPassword', password);
  await page.click('#loginBtn');
  // appView が表示されるまで待つ
  await page.waitForSelector('#appView:not(.d-none)', { timeout: 10000 });
}

/**
 * API経由でスタッフ（employee/part_time）としてログインし、トークンを返す。
 *
 * 希望シフトは /api/staff/requests（スタッフ本人）または /api/shop/my-requests
 * （店長自身）でのみ wish_history に永久保存される。/api/shop/shifts への直接
 * POST は「店長が実際にシフトとして配置する」操作であり、スタッフの希望提出とは
 * 別物なので、希望表管理（希望表）のテストデータ作成には使わないこと。
 */
async function loginAsStaffApi(request, { shopCode, staffCode, password }) {
  const res = await request.post('/api/login', {
    data: { shop_code: shopCode, user_code: staffCode, password },
  });
  const body = await res.json();
  return body.token;
}

/**
 * コンソールエラーを収集。
 * ページ遷移毎にリセットされるため、各テストで attach する。
 */
function attachConsoleCollector(page) {
  const errors = [];
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      const txt = msg.text();
      // ネットワークエラー等は除外（テスト環境の都合）
      if (txt.includes('Failed to load resource')) return;
      errors.push(txt);
    }
  });
  page.on('pageerror', (err) => {
    errors.push(`pageerror: ${err.message}`);
  });
  return errors;
}

/**
 * シフト画面の「その他の操作」（details.step-more）を開く。
 *
 * 工程バー化でコピー・印刷・用紙の向きはこの折りたたみの中へ移した。
 * 閉じている間 #printBtn 等は非表示なので、Playwright の click は
 * 「要素はあるが見えない」で失敗する。押す前に必ずこれを呼ぶこと。
 * 既に開いていれば何もしない（冪等）。
 */
async function openShiftMoreActions(page) {
  const details = page.locator('details.step-more');
  await details.waitFor({ state: 'attached', timeout: 10000 });
  if (!(await details.evaluate((d) => d.open))) {
    await details.locator('summary').click();
  }
  await page.locator('#printBtn').waitFor({ state: 'visible', timeout: 5000 });
}

module.exports = {
  ensureAdmin,
  ensureShop,
  loginAsAdmin,
  loginAsManager,
  loginAsStaffApi,
  attachConsoleCollector,
  openShiftMoreActions,
};
