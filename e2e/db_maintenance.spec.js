/**
 * e2e/db_maintenance.spec.js — DB メンテナンス機能の E2E テスト
 *
 * シナリオ:
 * 1. システム管理者でログイン
 * 2. 「システム」画面 →「DB診断」タブから
 * 3. DB状態表示（student ロール対応 / shop_holidays テーブル有無）
 * 4. 技術詳細（staffs テーブルのスキーマSQL）が確認できる
 *
 * 【Phase 2 での変更点】
 * 旧仕様ではホーム画面に「データベース状態確認・更新」ボタン（#dbMaintBtn）があり、
 * クリックするとモーダルで表示していたが、Phase 2 の管理画面再編（ナビ4項目化・
 * システム画面のタブ化）で「システム」→「DB診断」タブに統合された
 * （public/admin.js の renderDiagnosticTab）。モーダルではなくタブ本文に直接表示される。
 */
const { test, expect } = require('@playwright/test');
const { ensureAdmin, loginAsAdmin, attachConsoleCollector } = require('./helpers');

test.describe('DB メンテナンス機能', () => {
  test.beforeAll(async ({ request }) => {
    await ensureAdmin(request);
  });

  test('システム画面のDB診断タブにスキーマ状態が表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.click('button[data-screen="adminSystem"]');
    await page.click('[data-tab="diagnostic"]');
    await page.waitForSelector('#diagBody', { timeout: 10000 });
    await page.waitForSelector('text=student ロール', { timeout: 10000 });
    const bodyText = await page.locator('#diagBody').textContent();
    // テスト環境は毎回新規DBなので、student ロール対応・shop_holidays ありのはず
    expect(bodyText).toContain('student ロール対応済み');
    expect(bodyText).toContain('shop_holidays あり');
    expect(errors).toEqual([]);
  });

  test('技術詳細（スキーマSQL）が表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.click('button[data-screen="adminSystem"]');
    await page.click('[data-tab="diagnostic"]');
    await page.waitForSelector('text=student ロール', { timeout: 10000 });
    // 技術詳細を展開
    await page.click('#diagBody details summary');
    await page.waitForSelector('#diagBody details pre', { timeout: 5000 });
    const detailText = await page.locator('#diagBody details').textContent();
    expect(detailText).toContain('CREATE TABLE');
    expect(detailText).toContain('staffs');
    expect(errors).toEqual([]);
  });
});
