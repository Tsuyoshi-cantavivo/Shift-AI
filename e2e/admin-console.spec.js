/**
 * e2e/admin-console.spec.js — システム管理者コンソール（Phase 2）の受け入れテスト
 *
 * 【テスト対象】
 *   - ダッシュボード（KPI）の表示
 *   - ナビが4項目（ホーム / 店舗 / 監査ログ / システム）であること
 *   - 店舗詳細が4タブ（概要 / スタッフ / 設定 / 危険な操作）で切り替わること
 *   - 代理閲覧の開始（警告バー表示）と終了
 *   - 店舗の有効/無効トグルで店舗名が消えないこと（Phase 1 の回帰テスト）
 *   - 監査ログのフィルタUI・CSVボタンの表示
 *   - システム画面が4タブ（管理者アカウント / マイグレーション / DB診断 / お知らせ配信）
 *     であること
 *
 * 【方針】
 * 完全削除（店舗の完全削除・DELETE /api/admin/shops/:id）は、テストDBとはいえ実行しない
 * （危険な操作タブの表示確認までに留める）。
 *
 * ensureAdmin / ensureShop / loginAsAdmin は e2e/helpers.js の実装（request 引数を
 * 取る）に合わせて呼び出す。ナビゲーションは既存の e2e スペック（shop_create.spec.js /
 * admin_staff_management.spec.js）と同じ button[data-screen="..."] 方式に揃える
 * （サイドナビ・ボトムナビの2箇所に同じ data-screen 属性のボタンが存在するが、
 * page.click() は実測で単一要素にしか解決されず既存スペックでも問題なく動いている）。
 */
const { test, expect } = require('@playwright/test');
const { ensureAdmin, ensureShop, loginAsAdmin, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `ACON${RUN_ID}`,
  shopName: '管理者コンソールE2Eテスト店',
  managerCode: `aconmgr${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: 'コンソールE2E店長',
};

test.describe('システム管理者コンソール（Phase 2）', () => {
  test.beforeAll(async ({ request }) => {
    await ensureAdmin(request);
    await ensureShop(request, SHOP);
  });

  test('管理者でログインするとダッシュボードにKPIが表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);

    const kpi = page.locator('#dashKpi');
    await expect(kpi).toBeVisible();
    await expect(kpi).toContainText('稼働店舗');
    await expect(kpi).toContainText('在籍スタッフ');
    await expect(kpi).toContainText('今月の確定シフト');
    await expect(kpi).toContainText('要対応');
    expect(errors).toEqual([]);
  });

  test('ナビが4項目（ホーム / 店舗 / 監査ログ / システム）である', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);

    // サイドナビ（PC表示）の項目を数える。ボトムナビは別要素なのでここでは対象外。
    const navItems = page.locator('#sideNav .side-item');
    await expect(navItems).toHaveCount(4);
    await expect(navItems.nth(0)).toContainText('ホーム');
    await expect(navItems.nth(1)).toContainText('店舗');
    await expect(navItems.nth(2)).toContainText('監査ログ');
    await expect(navItems.nth(3)).toContainText('システム');
    expect(errors).toEqual([]);
  });

  test('店舗詳細が4タブ（概要・スタッフ・設定・危険な操作）で切り替わる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.click('button[data-screen="adminShops"]');
    await page.waitForSelector(`text=${SHOP.shopCode}`);
    await page.click(`text=${SHOP.shopCode}`);

    const tabs = page.locator('.tabs .tab');
    await expect(tabs).toHaveCount(4);
    await expect(page.locator('.tab[data-tab="overview"]')).toContainText('概要');
    await expect(page.locator('.tab[data-tab="staffs"]')).toContainText('スタッフ');
    await expect(page.locator('.tab[data-tab="settings"]')).toContainText('設定');
    await expect(page.locator('.tab[data-tab="danger"]')).toContainText('危険な操作');

    // 概要タブ（既定表示）
    await expect(page.locator('#impersonateBtn')).toBeVisible();

    // スタッフタブ
    await page.click('.tab[data-tab="staffs"]');
    await expect(page.locator('#addStaffBtn')).toBeVisible();

    // 設定タブ
    await page.click('.tab[data-tab="settings"]');
    await expect(page.locator('#stName')).toBeVisible();

    // 危険な操作タブ
    await page.click('.tab[data-tab="danger"]');
    await expect(page.locator('#archBtn')).toBeVisible();
    // 完全削除ボタンは表示だけ確認し、クリックはしない（テストDBでも実行禁止）
    await expect(page.locator('#delBtn')).toBeVisible();

    expect(errors).toEqual([]);
  });

  test('代理閲覧を開始すると警告バーが出て、「管理者に戻る」で解除できる', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.click('button[data-screen="adminShops"]');
    await page.waitForSelector(`text=${SHOP.shopCode}`);
    await page.click(`text=${SHOP.shopCode}`);

    // 概要タブの「この店舗を代理閲覧」ボタン
    await page.waitForSelector('#impersonateBtn');
    await page.click('#impersonateBtn');
    await page.waitForSelector('button[data-save]');
    await page.click('button[data-save]'); // 「代理閲覧を開始」

    const bar = page.locator('#impersonationBar');
    await expect(bar).toBeVisible();
    await expect(bar).toContainText('閲覧のみ');
    await expect(bar).toContainText(SHOP.shopName);

    await page.click('#stopImpersonateBtn');
    await expect(page.locator('#impersonationBar')).toHaveCount(0);

    expect(errors).toEqual([]);
  });

  test('店舗の有効/無効トグルで店舗名が消えない（Phase 1 回帰）', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.click('button[data-screen="adminShops"]');

    const row = page.locator('[data-detail]', { hasText: SHOP.shopCode });
    await expect(row).toBeVisible();
    const nameBefore = ((await row.locator('strong').first().textContent()) || '').trim();
    expect(nameBefore).not.toBe('');

    // 有効 → 無効化
    await row.locator('[data-toggle]').click();
    const rowAfterOff = page.locator('[data-detail]', { hasText: SHOP.shopCode });
    await expect(rowAfterOff).toContainText('無効'); // 再描画完了を待つ
    const nameAfterOff = ((await rowAfterOff.locator('strong').first().textContent()) || '').trim();
    expect(nameAfterOff).toBe(nameBefore);
    expect(nameAfterOff).not.toBe('');

    // 後続テスト・実データへの影響を避けるため、無効化 → 有効化に戻す
    await rowAfterOff.locator('[data-toggle]').click();
    const rowAfterOn = page.locator('[data-detail]', { hasText: SHOP.shopCode });
    await expect(rowAfterOn).toContainText('有効');
    const nameAfterOn = ((await rowAfterOn.locator('strong').first().textContent()) || '').trim();
    expect(nameAfterOn).toBe(nameBefore);

    expect(errors).toEqual([]);
  });

  test('監査ログのフィルタUIとCSVボタンが表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.click('button[data-screen="adminAudit"]');

    await expect(page.locator('#auStart')).toBeVisible();
    await expect(page.locator('#auEnd')).toBeVisible();
    await expect(page.locator('#auShop')).toBeVisible();
    await expect(page.locator('#auActor')).toBeVisible();
    await expect(page.locator('#auAction')).toBeVisible();
    await expect(page.locator('#auCsv')).toBeVisible();
    await expect(page.locator('#auCsv')).toContainText('CSV');

    expect(errors).toEqual([]);
  });

  test('システム画面が4タブ（管理者アカウント・マイグレーション・DB診断・お知らせ配信）である', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.click('button[data-screen="adminSystem"]');

    const tabs = page.locator('.tabs .tab');
    await expect(tabs).toHaveCount(4);
    await expect(page.locator('.tab[data-tab="admins"]')).toContainText('管理者アカウント');
    await expect(page.locator('.tab[data-tab="migrations"]')).toContainText('マイグレーション');
    await expect(page.locator('.tab[data-tab="diagnostic"]')).toContainText('DB診断');
    await expect(page.locator('.tab[data-tab="announce"]')).toContainText('お知らせ配信');

    // 既定タブ（管理者アカウント）が表示されている
    await expect(page.locator('#adminList')).toBeVisible();

    expect(errors).toEqual([]);
  });
});
