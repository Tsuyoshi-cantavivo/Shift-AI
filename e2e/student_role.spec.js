/**
 * e2e/student_role.spec.js — 学生アルバイト の Playwright テスト
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `SH3_${RUN_ID}`,
  shopName: 'E2E学生ロール店',
  managerCode: `mgr3_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: 'E2E店長3',
};

test.describe('学生アルバイト', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('スタッフ追加フォームに「学生アルバイト」選択肢が表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="staffs"]');
    await page.waitForSelector('#addStaffBtn', { timeout: 10000 });
    await page.click('#addStaffBtn');
    await page.waitForSelector('#f_role');
    // 学生アルバイト選択肢がある
    const opt = await page.locator('#f_role option[value="student"]').textContent();
    expect(opt).toContain('学生アルバイト');
    expect(opt).toContain('80');
    expect(errors).toEqual([]);
  });

  test('学生アルバイトを作成できる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="staffs"]');
    await page.waitForSelector('#addStaffBtn');
    await page.click('#addStaffBtn');
    await page.waitForSelector('#f_role');
    await page.fill('#f_code', 'STU001');
    await page.fill('#f_name', '学生花子');
    await page.selectOption('#f_role', 'student');
    await page.fill('#f_pw', 'Stu1234a');
    // 学生の上限が80に強制されること
    const maxVal = await page.inputValue('#f_max');
    expect(parseInt(maxVal, 10)).toBeLessThanOrEqual(80);
    await page.click('button[data-save]');
    // モーダルが閉じる or 成功Toast を待つ（どちらか先）
    await Promise.race([
      page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 8000 }),
      page.waitForSelector('.toast.success', { timeout: 8000 }),
    ]);
    // スタッフ一覧に表示
    await page.waitForSelector('text=学生花子', { timeout: 5000 });
    expect(errors).toEqual([]);
  });

  test('学生アルバイトは 80h 超に設定不可（クライアント側）', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="staffs"]');
    await page.waitForSelector('#addStaffBtn');
    await page.click('#addStaffBtn');
    await page.waitForSelector('#f_role');
    await page.fill('#f_code', 'STU002');
    await page.fill('#f_name', '学生次郎');
    await page.selectOption('#f_role', 'student');
    // 100h を入力しようとする → 自動的に80に修正される
    await page.fill('#f_max', '100');
    await page.fill('#f_pw', 'Stu1234a');
    const maxVal = await page.inputValue('#f_max');
    expect(parseInt(maxVal, 10)).toBeLessThanOrEqual(80);
    expect(errors).toEqual([]);
  });
});
