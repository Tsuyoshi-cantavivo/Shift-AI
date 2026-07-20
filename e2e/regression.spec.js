/**
 * e2e/regression.spec.js — デグレードテスト（既存機能が壊れていないか確認）
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `REG_${RUN_ID}`,
  shopName: 'デグレ防止店',
  managerCode: `regmgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: 'デグレ店長',
};

test.describe('デグレードテスト（既存機能の動作確認）', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('ログインが成功する', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // アプリが表示される
    await expect(page.locator('#appView')).toBeVisible();
    // サイドナビが表示される
    await expect(page.locator('.side-nav')).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('間違ったパスワードでログイン拒否', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await page.goto('/');
    await page.fill('#loginShopCode', SHOP.shopCode);
    await page.fill('#loginUserCode', SHOP.managerCode);
    await page.fill('#loginPassword', 'wrongpassword1');
    await page.click('#loginBtn');
    // エラー表示
    await page.waitForSelector('#loginError:not(:empty)');
    const err = await page.textContent('#loginError');
    expect(err.length).toBeGreaterThan(0);
    // アプリには入らない
    await expect(page.locator('#loginView')).not.toHaveClass(/d-none/);
    expect(errors).toEqual([]);
  });

  test('ダッシュボードが表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.waitForSelector('.page-head');
    const title = await page.textContent('.page-head h4');
    expect(title).toContain('ダッシュボード');
    expect(errors).toEqual([]);
  });

  test('権限: システム管理者メニューは店舗ロールに表示されない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // 店舗のサイドナビに「店舗」(管理者メニュー) がない
    const adminShopItem = await page.locator('button[data-screen="adminShops"]').count();
    expect(adminShopItem).toBe(0);
    expect(errors).toEqual([]);
  });

  test('スタッフ管理画面の表示', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="staffs"]');
    await page.waitForSelector('.page-head');
    const title = await page.textContent('.page-head h4');
    expect(title).toContain('スタッフ管理');
    // スタッフ追加ボタンが表示
    await expect(page.locator('#addStaffBtn')).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('設定画面（全タブ表示）', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    // 全タブが表示
    for (const tab of ['shift', 'shifthours', 'shop', 'periods', 'password']) {
      await expect(page.locator(`.tab[data-tab="${tab}"]`)).toBeVisible();
    }
    // 各タブを切り替えてエラー無し
    for (const tab of ['shift', 'shifthours', 'shop', 'periods', 'password']) {
      await page.click(`.tab[data-tab="${tab}"]`);
      await page.waitForTimeout(200);
    }
    expect(errors).toEqual([]);
  });

  test('通知画面が表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="notifications"]');
    await page.waitForSelector('.page-head');
    const title = await page.textContent('.page-head h4');
    expect(title).toContain('通知');
    expect(errors).toEqual([]);
  });

  test('テーマ切替が動作する', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // ログイン後のテーマ切替ボタンが表示されるまで待つ
    await page.waitForSelector('#themeToggleBtn', { state: 'visible', timeout: 10000 });
    const beforeTheme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    await page.click('#themeToggleBtn');
    await page.waitForTimeout(300);
    const afterTheme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
    expect(beforeTheme).not.toEqual(afterTheme);
    expect(errors).toEqual([]);
  });
});
