/**
 * e2e/shift_hours_sync.spec.js — シフト時間設定とパターンの同期 E2E
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `SYNC_${RUN_ID}`,
  shopName: '同期テスト店',
  managerCode: `mgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

test.describe('シフト時間設定 ⇔ パターン同期', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('シフト時間設定を保存すると、シフト設定タブにも反映される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });

    // 1. シフト時間設定タブを開く
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shSave', { timeout: 10000 });

    // 2. 一括設定で 04:00-02:00 に設定
    const bulkToggle = page.locator('#shBulkMode');
    if (!await bulkToggle.isChecked()) await bulkToggle.check();
    await page.fill('#shBulkWrap .sh-start', '04:00');
    await page.fill('#shBulkWrap .sh-end', '02:00');
    // 同期チェックボックスがデフォルトでONであることを確認
    expect(await page.locator('#shSyncPatterns').isChecked()).toBeTruthy();
    // 保存
    await page.click('#shSave');
    // 成功 or 同期ログを待つ
    await page.waitForSelector('#shMsg .text-success, .toast', { timeout: 8000 });

    // 3. シフト設定タブに切り替え
    await page.click('.tab[data-tab="shift"]');
    await page.waitForSelector('#matrixWrap, .empty-state', { timeout: 10000 });

    // 4. パターンが 04:00-02:00 になっているか確認
    // ※ 新規店舗なら「通し」パターンが自動作成されているはず
    const matrixText = await page.locator('#matrixWrap').textContent().catch(() => '');
    // 04:00 と 02:00 が含まれることを確認
    expect(matrixText).toContain('04:00');
    expect(matrixText).toContain('02:00');
    expect(errors).toEqual([]);
  });

  test('同期OFFで保存すればパターンは変わらない', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // 事前にパターンを固定値で作るため、シフト設定タブで追加
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shift"]');
    await page.click('.tab[data-tab="shift"]');
    await page.waitForSelector('#addPat, .empty-state', { timeout: 10000 });
    // 既にパターンがあるはず（前のテストで作成）なので、その時間を記憶
    const beforeMatrix = await page.locator('#matrixWrap').textContent().catch(() => '');

    // シフト時間設定タブで時間を変えて、同期OFFで保存
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shSave', { timeout: 10000 });
    const bulkToggle = page.locator('#shBulkMode');
    if (!await bulkToggle.isChecked()) await bulkToggle.check();
    await page.fill('#shBulkWrap .sh-start', '10:00');
    await page.fill('#shBulkWrap .sh-end', '20:00');
    // 同期OFF
    await page.uncheck('#shSyncPatterns');
    await page.click('#shSave');
    await page.waitForSelector('#shMsg .text-success, .toast', { timeout: 8000 });

    // シフト設定タブに戻って確認
    await page.click('.tab[data-tab="shift"]');
    await page.waitForSelector('#matrixWrap, .empty-state', { timeout: 10000 });
    const afterMatrix = await page.locator('#matrixWrap').textContent().catch(() => '');
    // 04:00-02:00 のまま（同期していないので）
    expect(afterMatrix).toContain('04:00');
    expect(afterMatrix).toContain('02:00');
    expect(errors).toEqual([]);
  });
});
