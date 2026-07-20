/**
 * e2e/fast_navigation.spec.js — 高速遷移でのコンソールエラー（特に innerHTML null）防止
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `FN_${RUN_ID}`,
  shopName: 'E2E高速遷移店',
  managerCode: `mgr5_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: 'E2E店長5',
};

test.describe('高速遷移テスト', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('タブを高速に切り替えても innerHTML null エラーが出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });

    // 10回 高速に画面を切り替え
    const screens = ['dashboard', 'shifts', 'staffs', 'settings', 'dashboard', 'staffs', 'shifts', 'settings'];
    for (const screen of screens) {
      const btn = page.locator(`button[data-screen="${screen}"]`).first();
      if (await btn.isVisible()) {
        await btn.click({ timeout: 3000 }).catch(() => {});
      }
      // 待たずに次へ
    }
    // 少し待ってコンソールエラーを確認
    await page.waitForTimeout(1500);
    // innerHTML null 系のエラーがないこと
    const nullErrors = errors.filter((e) => e.includes("Cannot set properties of null"));
    expect(nullErrors, `innerHTML null errors: ${nullErrors.join('\n')}`).toEqual([]);
  });

  test('メニューを高速にクリックしてもエラーが出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // サイドメニューの各項目を高速クリック
    const items = ['dashboard', 'shifts', 'aiGenerate', 'staffs', 'requests', 'analytics', 'notifications', 'settings'];
    for (const key of items) {
      await page.locator(`button[data-screen="${key}"]`).first().click({ timeout: 3000 }).catch(() => {});
    }
    await page.waitForTimeout(1500);
    const nullErrors = errors.filter((e) => e.includes("Cannot set properties of null"));
    expect(nullErrors).toEqual([]);
  });

  test('設定タブを高速切替してもエラーが出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shift"]');
    // 設定内のタブを高速切替
    const tabs = ['shift', 'shifthours', 'shop', 'periods', 'password', 'shift', 'shifthours', 'shop'];
    for (const tab of tabs) {
      await page.locator(`.tab[data-tab="${tab}"]`).click({ timeout: 3000 }).catch(() => {});
    }
    await page.waitForTimeout(1500);
    const nullErrors = errors.filter((e) => e.includes("Cannot set properties of null"));
    expect(nullErrors).toEqual([]);
  });
});
