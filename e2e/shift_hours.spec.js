/**
 * e2e/shift_hours.spec.js — シフト時間設定タブ の Playwright テスト
 */
const { test, expect } = require('@playwright/test');
const { ensureAdmin, ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `SH2_${RUN_ID}`,
  shopName: 'E2E時間設定店',
  managerCode: `mgr2_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: 'E2E店長2',
};

test.describe('シフト時間設定タブ', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('設定画面に「シフト時間設定」タブが表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    // 設定画面へ
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    const tabText = await page.textContent('.tab[data-tab="shifthours"]');
    expect(tabText).toContain('シフト時間設定');
    expect(errors).toEqual([]);
  });

  test('一括設定 ON で曜日別フォーム非表示', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    // 読み込み完了待ち
    await page.waitForSelector('#shBulkMode', { timeout: 10000 });

    // 一括設定ON
    const bulkToggle = await page.locator('#shBulkMode');
    if (!await bulkToggle.isChecked()) await bulkToggle.check();
    // 曜日別フォームが非表示
    await expect(page.locator('#shDaysWrap')).toBeHidden();
    // 一括フォームは表示
    await expect(page.locator('#shBulkWrap')).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('一括設定 OFF で曜日別フォーム表示（月-日・祝日の8枠）', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shBulkMode', { timeout: 10000 });

    const bulkToggle = await page.locator('#shBulkMode');
    if (await bulkToggle.isChecked()) await bulkToggle.uncheck();
    // 曜日別フォーム表示
    await expect(page.locator('#shDaysWrap')).toBeVisible();
    await expect(page.locator('#shBulkWrap')).toBeHidden();
    // 8行（月火水木金土日祝）存在
    const dayRows = await page.locator('#shDaysWrap .sh-row').count();
    expect(dayRows).toBe(8);
    expect(errors).toEqual([]);
  });

  test('定休日にチェックすると時間入力が disabled になる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shBulkMode', { timeout: 10000 });

    const bulkToggle = await page.locator('#shBulkMode');
    if (await bulkToggle.isChecked()) await bulkToggle.uncheck();
    await expect(page.locator('#shDaysWrap')).toBeVisible();

    // 最初の曜日行（月曜）の定休日チェック
    const firstClosedCheck = page.locator('#shDaysWrap .sh-row').first().locator('.sh-closed');
    await firstClosedCheck.check();
    // 開始・終了時刻が disabled になる
    const startInput = page.locator('#shDaysWrap .sh-row').first().locator('.sh-start');
    const endInput = page.locator('#shDaysWrap .sh-row').first().locator('.sh-end');
    await expect(startInput).toBeDisabled();
    await expect(endInput).toBeDisabled();
    // チェックを外すと再有効化
    await firstClosedCheck.uncheck();
    await expect(startInput).toBeEnabled();
    await expect(endInput).toBeEnabled();
    expect(errors).toEqual([]);
  });

  test('一括設定を保存できる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shSave', { timeout: 10000 });

    // 一括モードにして保存
    const bulkToggle = await page.locator('#shBulkMode');
    if (!await bulkToggle.isChecked()) await bulkToggle.check();
    await page.fill('#shBulkWrap .sh-start', '10:00');
    await page.fill('#shBulkWrap .sh-end', '21:00');
    await page.click('#shSave');
    // 成功メッセージ または toast を待つ（どちらか先）
    await Promise.race([
      page.waitForSelector('#shMsg .text-success', { timeout: 8000 }),
      page.waitForSelector('.toast.success', { timeout: 8000 }),
    ]);
    expect(errors).toEqual([]);
  });

  test('祝日を追加・削除できる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shHolidayDate', { timeout: 10000 });

    // 祝日追加
    await page.fill('#shHolidayDate', '2026-01-01');
    await page.click('#shAddHoliday');
    await page.waitForSelector('.holiday-row[data-date="2026-01-01"]', { timeout: 5000 });

    // 削除
    await page.click('.holiday-row[data-date="2026-01-01"] [data-del="2026-01-01"]');
    await page.waitForSelector('.holiday-row[data-date="2026-01-01"]', { state: 'detached', timeout: 5000 });
    expect(errors).toEqual([]);
  });

  test('日本の祝日をプレビューできる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shPreviewJapanese', { timeout: 10000 });

    // プレビューボタン
    await page.click('#shPreviewJapanese');
    await page.waitForSelector('#shJapanesePreview .holiday-chip', { timeout: 8000 });
    // 元日が含まれる
    const chips = await page.locator('#shJapanesePreview .holiday-chip').count();
    expect(chips).toBeGreaterThan(10);
    expect(errors).toEqual([]);
  });

  test('日本の祝日を一括取り込みできる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, {
      shopCode: SHOP.shopCode,
      managerCode: SHOP.managerCode,
      password: SHOP.managerPassword,
    });
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shifthours"]');
    await page.click('.tab[data-tab="shifthours"]');
    await page.waitForSelector('#shImportJapanese', { timeout: 10000 });

    // confirm ダイアログを自動承認
    page.on('dialog', (d) => d.accept().catch(() => {}));
    // 一括取り込み
    await page.click('#shImportJapanese');
    // 成功トースト or 祝日リストに追加されるのを待つ
    await Promise.race([
      page.waitForSelector('.toast', { timeout: 8000 }),
      page.waitForSelector('.holiday-row', { timeout: 8000 }),
    ]);
    // 少なくとも1件の祝日（元日など）が含まれる
    const holidayCount = await page.locator('.holiday-row').count();
    expect(holidayCount).toBeGreaterThan(5);  // 数年分で数十件
    expect(errors).toEqual([]);
  });
});
