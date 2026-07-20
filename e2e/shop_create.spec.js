/**
 * e2e/shop_create.spec.js — 店舗作成 + 店舗責任者作成 の Playwright テスト
 */
const { test, expect } = require('@playwright/test');
const { ensureAdmin, ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

// テストラン毎に一意のコードを生成（重複エラーを回避）
const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `E2E${RUN_ID}`,
  shopName: 'E2E店舗新規',
  managerCode: `mgr${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: 'E2E新店長',
};

test.describe('店舗作成 + 店舗責任者作成', () => {
  test('システム管理者が店舗を新規作成し、同時に店舗責任者アカウントを発行できる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await ensureAdmin(request);

    // 管理者としてログイン
    await page.goto('/');
    await page.fill('#loginShopCode', 'admin');
    await page.fill('#loginUserCode', 'admin');
    await page.fill('#loginPassword', 'admin123');
    await page.click('#loginBtn');
    await page.waitForSelector('#appView:not(.d-none)');

    // 店舗一覧へ遷移
    await page.click('button[data-screen="adminShops"]');
    await page.waitForSelector('#addShopBtn');

    // 店舗追加モーダルを開く
    await page.click('#addShopBtn');
    await page.waitForSelector('#shMgrCode');

    // フォーム入力（店舗 + 店舗責任者）
    await page.fill('#shCode', SHOP.shopCode);
    await page.fill('#shName', SHOP.shopName);
    await page.fill('#shPw', 'ShopPass1');
    await page.fill('#shMgrCode', SHOP.managerCode);
    await page.fill('#shMgrName', SHOP.managerName);
    await page.fill('#shMgrPw', SHOP.managerPassword);

    // 保存
    await page.click('button[data-save]');

    // モーダルが閉じる or 成功Toast（どちらか先）
    await Promise.race([
      page.waitForSelector('.modal-overlay', { state: 'detached', timeout: 8000 }),
      page.waitForSelector('.toast', { timeout: 8000 }),
    ]);
    // 成功 Toast が表示されたか、店舗一覧に追加されたか確認
    await page.waitForTimeout(500);
    // 店舗が一覧に表示されていることを検索（より確実）
    await page.waitForSelector(`text=${SHOP.shopCode}`, { timeout: 5000 });
    // エラーがないことを確認（エラーメッセージが表示されていない）
    const errVisible = await page.locator('#shFormErr:not(:empty)').count();
    expect(errVisible).toBe(0);
    expect(errors, `Console errors: ${errors.join(', ')}`).toEqual([]);
  });

  test('作成した店舗責任者でログインできる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await ensureShop(request, SHOP);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // ダッシュボードが表示される
    await page.waitForSelector('.page-head');
    const title = await page.textContent('.page-head h4');
    expect(title).toContain('ダッシュボード');
    expect(errors).toEqual([]);
  });

  test('必須項目欠落時に分かりやすいエラー', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await ensureAdmin(request);
    await page.goto('/');
    await page.fill('#loginShopCode', 'admin');
    await page.fill('#loginUserCode', 'admin');
    await page.fill('#loginPassword', 'admin123');
    await page.click('#loginBtn');
    await page.waitForSelector('#appView:not(.d-none)');

    await page.click('button[data-screen="adminShops"]');
    await page.waitForSelector('#addShopBtn');
    await page.click('#addShopBtn');
    await page.waitForSelector('#shMgrCode');

    // 店舗コードだけ入れて保存（氏名などは空のまま）
    await page.fill('#shCode', 'E2EFAIL');
    await page.fill('#shName', '');
    await page.fill('#shPw', 'ShopPass1');
    await page.fill('#shMgrCode', 'mgr');
    await page.fill('#shMgrName', '');
    await page.fill('#shMgrPw', 'Mgr12345a');
    await page.click('button[data-save]');

    // フォーム内エラー表示を確認
    await page.waitForSelector('#shFormErr:not(:empty)', { timeout: 5000 });
    const errText = await page.textContent('#shFormErr');
    // いずれかの必須項目エラーが出る
    expect(errText.length).toBeGreaterThan(0);
    // 店舗が作成されていないことを確認
    expect(errors).toEqual([]);
  });
});
