/**
 * e2e/db_maintenance.spec.js — DB メンテナンス機能の E2E テスト
 *
 * シナリオ:
 * 1. システム管理者でログイン
 * 2. ホーム画面の「データベース状態確認・更新」ボタンから
 * 3. DB状態表示（student ロール対応 / shop_holidays テーブル有無）
 * 4. （必要なら）マイグレーション実行
 * 5. 全スタッフの manager 化確認
 */
const { test, expect } = require('@playwright/test');
const { ensureAdmin, loginAsAdmin, attachConsoleCollector } = require('./helpers');

test.describe('DB メンテナンス機能', () => {
  test.beforeAll(async ({ request }) => {
    await ensureAdmin(request);
  });

  test('ホーム画面に「データベース状態確認・更新」ボタンが表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.waitForSelector('#dbMaintBtn', { timeout: 10000 });
    expect(await page.locator('#dbMaintBtn').isVisible()).toBeTruthy();
    expect(await page.locator('#dbMaintBtn').textContent()).toContain('データベース');
    expect(errors).toEqual([]);
  });

  test('DB状態モーダルが開き、状態が表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.waitForSelector('#dbMaintBtn');
    await page.click('#dbMaintBtn');
    // モーダル内に「student ロール対応」「shop_holidays」表示を待つ
    await page.waitForSelector('text=student ロール対応', { timeout: 10000 });
    await page.waitForSelector('text=shop_holidays テーブル');
    const modalText = await page.locator('.modal-overlay').textContent();
    // いずれか（対応済み or 未対応）が表示される
    expect(modalText).toContain('student ロール対応');
    expect(errors).toEqual([]);
  });

  test('DBスキーマが新仕様であることを確認（最新環境）', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.waitForSelector('#dbMaintBtn');
    await page.click('#dbMaintBtn');
    await page.waitForSelector('text=student ロール対応', { timeout: 10000 });
    // ローカルの新規DBなら「対応済み」なはず
    const modalText = await page.locator('.modal-overlay').textContent();
    // テスト環境は毎回新規DBなので対応済みなはず
    expect(modalText).toContain('対応済み');
    expect(errors).toEqual([]);
  });

  test('技術詳細（スキーマSQL）が表示される', async ({ page, request }) => {
    const errors = attachConsoleCollector(page);
    await loginAsAdmin(page, request);
    await page.waitForSelector('#dbMaintBtn');
    await page.click('#dbMaintBtn');
    await page.waitForSelector('text=student ロール対応');
    // 技術詳細を展開
    await page.click('details summary');
    await page.waitForSelector('details pre', { timeout: 5000 });
    const detailText = await page.locator('details').textContent();
    expect(detailText).toContain('CREATE TABLE');
    expect(detailText).toContain('staffs');
    expect(errors).toEqual([]);
  });
});
