/**
 * e2e/myshift_shop_role.spec.js — 店舗管理者のマイシフトが動作するか確認
 *
 * 以前のバグ: SCREENS.myshift が2重定義され、古い /staff/* を呼ぶ版が優先されていた
 * 修正後: shop ロールは /shop/* を呼ぶ新画面、staff ロールは staffMyshift 画面へ
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, loginAsAdmin, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `MSH_${RUN_ID}`,
  shopName: 'マイシフト確認店',
  managerCode: `mgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

test.describe('店舗管理者のマイシフト（権限エラー解消）', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('マイシフトを開いて /staff/* を呼ばないこと（403回避）', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    const api403Errors = [];
    // 403 を監視
    page.on('requestfailed', (req) => {
      const url = req.url();
      if (url.includes('/api/staff/')) {
        api403Errors.push(`Forbidden staff API call: ${url}`);
      }
    });
    page.on('response', async (resp) => {
      const url = resp.url();
      if (resp.status() === 403 && url.includes('/api/staff/')) {
        api403Errors.push(`403 on staff API: ${url}`);
      }
    });

    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });

    // サイドメニューの「マイシフト・希望」をクリック
    await page.click('button[data-screen="myshift"]');
    await page.waitForSelector('.page-head', { timeout: 10000 });
    // タイトルが「マイシフト・希望」であることを確認
    const title = await page.locator('.page-head h4').first().textContent();
    expect(title).toContain('マイシフト');
    // /api/staff/* を呼んでいないことを確認
    await page.waitForTimeout(2000); // API呼び出し完了を待つ
    expect(api403Errors, `403 errors: ${api403Errors.join('\n')}`).toEqual([]);
    expect(errors).toEqual([]);
  });

  test('マイシフト画面に確定シフト・希望セクションが表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="myshift"]');
    // 3つのセクションが表示される
    await page.waitForSelector('text=確定シフト', { timeout: 10000 });
    await page.waitForSelector('text=希望の提出');
    await page.waitForSelector('text=希望履歴');
    expect(errors).toEqual([]);
  });

  test('希望を追加 ボタンが機能する', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="myshift"]');
    await page.waitForSelector('#addMyReqBtn', { timeout: 10000 });
    await page.click('#addMyReqBtn');
    // モーダルが開く
    await page.waitForSelector('#myRqDate', { timeout: 5000 });
    // 項目が表示される
    expect(await page.locator('#myRqSt').isVisible()).toBeTruthy();
    expect(await page.locator('#myRqEt').isVisible()).toBeTruthy();
    expect(await page.locator('#myRqFlex').isVisible()).toBeTruthy();
    expect(errors).toEqual([]);
  });
});
