/**
 * e2e/manager_only_mode.spec.js — 店長のみ運用モードとシフト画面の工程バー
 *
 * 検証すること:
 * 1. 旧「AIシフト作成」画面がナビから消えていること（両モード共通）
 * 2. 店長のみ運用でナビが6項目になり、「マイシフト・希望」「通知」が消えること
 *    ただしヘッダーのベルは残り、システム管理者の一斉通知は読めること
 * 3. 設定の「募集期間」タブが店長のみ運用で消えること
 * 4. シフト画面の工程バーが4ステップ出て、現在地が状態に応じて動くこと
 * 5. 工程バー①から希望取り込みモーダルが開くこと
 * 6. ダッシュボードのAIアシスタントカードが出て、入力欄が使えること
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const SHOP = {
  shopCode: `MOM_${RUN_ID}`,
  shopName: '店長のみ運用テスト店',
  managerCode: `mgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

/** 設定→店舗情報から運用モードを切り替えて保存する。 */
async function setOperationMode(page, mode) {
  await page.click('button[data-screen="settings"]');
  await page.waitForSelector('.tab[data-tab="shop"]');
  await page.click('.tab[data-tab="shop"]');
  await page.waitForSelector(`input[name="opMode"][value="${mode}"]`);
  await page.check(`input[name="opMode"][value="${mode}"]`);
  await page.click('#saveSettings');
  // 保存直後にナビへ反映される（再ログイン不要）ことも併せて確認したいので、
  // トーストではなくナビの変化を待つ。
  await page.waitForTimeout(600);
}

const login = (page) => loginAsManager(page, {
  shopCode: SHOP.shopCode,
  managerCode: SHOP.managerCode,
  password: SHOP.managerPassword,
});

test.describe('店長のみ運用モード', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('AIシフト作成画面はどちらのモードでもナビに無い', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await login(page);
    await page.waitForSelector('button[data-screen="dashboard"]');
    await expect(page.locator('button[data-screen="aiGenerate"]')).toHaveCount(0);
    expect(errors).toEqual([]);
  });

  test('既定（スタッフ運用）ではナビが8項目', async ({ page }) => {
    await login(page);
    await page.waitForSelector('.side-item');
    await expect(page.locator('.side-item')).toHaveCount(8);
    await expect(page.locator('button.side-item[data-screen="myshift"]')).toHaveCount(1);
    await expect(page.locator('button.side-item[data-screen="notifications"]')).toHaveCount(1);
  });

  test('店長のみ運用に切り替えるとナビが6項目になり、保存直後に反映される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await login(page);
    await setOperationMode(page, 'manager_only');

    await expect(page.locator('.side-item')).toHaveCount(6);
    await expect(page.locator('button.side-item[data-screen="myshift"]')).toHaveCount(0);
    await expect(page.locator('button.side-item[data-screen="notifications"]')).toHaveCount(0);
    // 残るもの
    for (const key of ['dashboard', 'shifts', 'staffs', 'requests', 'analytics', 'settings']) {
      await expect(page.locator(`button.side-item[data-screen="${key}"]`)).toHaveCount(1);
    }
    // 通知ナビは消えるが、ヘッダーのベルは残る（システム管理者の一斉通知の受け口）
    await expect(page.locator('#notifBtn')).not.toHaveClass(/d-none/);
    expect(errors).toEqual([]);
  });

  test('店長のみ運用ではリロード後もナビが6項目のまま', async ({ page }) => {
    await login(page);
    await setOperationMode(page, 'manager_only');
    await page.reload();
    await page.waitForSelector('.side-item');
    await expect(page.locator('.side-item')).toHaveCount(6);
  });

  test('店長のみ運用では設定の「募集期間」タブが消える', async ({ page }) => {
    await login(page);
    await setOperationMode(page, 'manager_only');
    await page.click('button[data-screen="settings"]');
    await page.waitForSelector('.tab[data-tab="shop"]');
    await expect(page.locator('.tab[data-tab="periods"]')).toHaveCount(0);
    await expect(page.locator('.tab[data-tab="shifthours"]')).toHaveCount(1);
  });

  test('店長のみ運用では変更申請の導線が消える', async ({ page }) => {
    await login(page);
    await setOperationMode(page, 'manager_only');
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#stepGrid');
    await expect(page.locator('#openCreq2')).toHaveCount(0);
    await page.click('button[data-screen="dashboard"]');
    await page.waitForSelector('#kpiGrid');
    await page.waitForTimeout(1200);
    await expect(page.locator('#qCreq')).toHaveCount(0);
  });

  test('スタッフ運用に戻すとナビが8項目に戻る', async ({ page }) => {
    await login(page);
    await setOperationMode(page, 'manager_only');
    await expect(page.locator('.side-item')).toHaveCount(6);
    await setOperationMode(page, 'staff');
    await expect(page.locator('.side-item')).toHaveCount(8);
  });
});

test.describe('シフト画面の工程バー', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('4ステップが出て、希望0件なら現在地はSTEP1', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await login(page);
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#stepGrid');

    await expect(page.locator('.step-cell')).toHaveCount(4);
    await expect(page.locator('.step-cell[data-step="1"] .step-name')).toHaveText('希望を集める');
    await expect(page.locator('.step-cell[data-step="2"] .step-name')).toHaveText('AIで組む');
    await expect(page.locator('.step-cell[data-step="3"] .step-name')).toHaveText('調整する');
    await expect(page.locator('.step-cell[data-step="4"] .step-name')).toHaveText('確定する');

    // 希望も確定シフトも無い新規店舗なので、現在地はSTEP1。
    await expect(page.locator('.step-cell.now')).toHaveCount(1);
    await expect(page.locator('.step-cell[data-step="1"]')).toHaveClass(/now/);
    await expect(page.locator('#stepStat1')).toContainText('まだありません');
    expect(errors).toEqual([]);
  });

  test('工程バー①から希望取り込みモーダルが開く', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await login(page);
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#stepImportBtn');
    await page.click('#stepImportBtn');
    // openWishImportModal のステップ1が出る（希望表管理から開くのと同じモーダル）
    await page.waitForSelector('.modal-overlay', { timeout: 8000 });
    await expect(page.locator('.modal-overlay')).toContainText('取り込');
    expect(errors).toEqual([]);
  });

  test('コピー・印刷は「その他の操作」に畳まれている', async ({ page }) => {
    await login(page);
    await page.click('button[data-screen="shifts"]');
    await page.waitForSelector('#stepGrid');
    const details = page.locator('details.step-more');
    await expect(details).toHaveCount(1);
    // 既定では閉じている
    expect(await details.evaluate((d) => d.open)).toBe(false);
    await details.locator('summary').click();
    await expect(page.locator('#copyBtn')).toBeVisible();
    await expect(page.locator('#printBtn')).toBeVisible();
  });
});

test.describe('ダッシュボードのAIアシスタント', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  test('AIアシスタントカードが出て入力欄が使える', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await login(page);
    await page.waitForSelector('#kpiGrid');
    // 右カラムは /shop/ai/review などを待ってから描かれる
    await page.waitForSelector('#dashChat .chat-card', { timeout: 15000 });

    await expect(page.locator('#shopChatInput')).toBeVisible();
    await expect(page.locator('#shopChatSend')).toBeVisible();
    // 高さを抑えたバリアントが当たっていること（右カラムを占領しない）
    await expect(page.locator('#dashChat .chat-card')).toHaveClass(/chat-card-compact/);
    // 1通目が出ている（LLM未接続でも renderShopChat 側の入口メッセージが出る）
    await expect(page.locator('#shopChatMsgs .chat-bubble')).not.toHaveCount(0);
    // 旧「AIからの提案」の読み物カードは無い
    await expect(page.locator('#dashRight')).not.toContainText('AIからの提案');
    expect(errors).toEqual([]);
  });

  test('クイック操作の「シフトを作る」でシフト画面へ行く', async ({ page }) => {
    await login(page);
    await page.waitForSelector('#qShifts', { timeout: 15000 });
    await page.click('#qShifts');
    await page.waitForSelector('#stepGrid');
    await expect(page.locator('.step-cell')).toHaveCount(4);
  });
});
