/**
 * e2e/shift_create.spec.js — シフト作成 の Playwright テスト
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `SH4_${RUN_ID}`,
  shopName: 'E2Eシフト作成店',
  managerCode: `mgr4_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: 'E2E店長4',
};

test.describe('シフト作成', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('社員を作成してシフトを配置できる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });

    // 社員を作成
    await page.click('button[data-screen="staffs"]');
    await page.waitForSelector('#addStaffBtn');
    await page.click('#addStaffBtn');
    await page.waitForSelector('#f_role');
    await page.fill('#f_code', 'EMP001');
    await page.fill('#f_name', '社員一郎');
    await page.selectOption('#f_role', 'employee');
    await page.fill('#f_pw', 'Emp1234a');
    await page.click('button[data-save]');
    // モーダル閉じる or toast
    await Promise.race([
      page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 8000 }),
      page.waitForSelector('text=社員一郎', { timeout: 8000 }),
    ]);

    // パターンを作成
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shift"]');
    await page.click('.tab[data-tab="shift"]');
    await page.waitForSelector('#addPat');
    await page.click('#addPat');
    await page.waitForSelector('#pName');
    await page.fill('#pName', '通し');
    await page.fill('#pSt', '09:00');
    await page.fill('#pEt', '18:00');
    await page.fill('#pReq', '2');
    await page.click('button[data-save]');
    await Promise.race([
      page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 8000 }),
      page.waitForSelector('.toast.success, .toast.info', { timeout: 8000 }),
    ]);

    // シフト画面へ
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('.page-head', { timeout: 10000 });
    expect(errors).toEqual([]);
  });

  test('バリデーション: 学生のみのシフトは拒否される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // 学生アルバイト作成
    await page.click('button[data-screen="staffs"]');
    await page.waitForSelector('#addStaffBtn');
    await page.click('#addStaffBtn');
    await page.waitForSelector('#f_role');
    await page.fill('#f_code', 'STU100');
    await page.fill('#f_name', '学生テスト');
    await page.selectOption('#f_role', 'student');
    await page.fill('#f_pw', 'Stu1234a');
    await page.click('button[data-save]');
    await Promise.race([
      page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 8000 }),
      page.waitForSelector('text=学生テスト', { timeout: 8000 }),
    ]);
    expect(errors).toEqual([]);
  });
});
