/**
 * e2e/staff_attention.spec.js — ダッシュボードの「気にかけたい人」カード。
 *
 * 判定ロジックは tests/test_staff_attention.py の責務。ここで確かめるのは
 * 「該当者がいるときだけ出るか」「事実と声かけが出るか」「決めつけを渡して
 * いないか」の3点。API は page.route でスタブする（実データで検出条件を
 * 満たすには90日分のシフト投入が要り、テストが遅く壊れやすくなる）。
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);

const SHOP = {
  shopCode: `ATN_${RUN_ID}`,
  shopName: '気づきテスト店',
  managerCode: `atnmgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

const ITEM = {
  staff_id: 101,
  name: '田中太郎',
  reasons: [{ type: 'attendance_drop', recent: 4, base: 10.0 }],
  headline: '出勤が減っています',
  detail: '以前は30日あたり10.0日 → 直近30日は4日',
  message: '最近シフトが少なめですが、ご都合はいかがですか。',
};

/** /api/shop/staff-attention をスタブする。status を渡すと失敗を再現できる。 */
function stubAttention(page, items, status = 200) {
  return page.route(
    (url) => url.pathname.endsWith('/api/shop/staff-attention'),
    (route) => (status === 200
      ? route.fulfill({ json: { items, source: 'rule_based' } })
      : route.fulfill({ status, json: { error: 'failed' } })),
  );
}

async function openDashboard(page) {
  await loginAsManager(page, {
    shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword,
  });
  // ログイン直後がダッシュボード。右カラムの描画完了を「AIからの提案」で待つ。
  await page.waitForSelector('#dashRight', { timeout: 15000 });
  await expect(page.locator('#dashRight')).toContainText('AIからの提案', { timeout: 15000 });
}

test.describe('ダッシュボード: 気にかけたい人', () => {
  test.beforeAll(async ({ request }) => {
    await ensureShop(request, SHOP);
  });

  // ==========================================================
  // ケース1: 該当者がいなければカードごと出さない
  // ==========================================================
  test('該当者がいないときはカードごと出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, []);
    await openDashboard(page);
    // toBeHidden() は要素が無くても真になるため、件数で見る
    await expect(page.locator('#dashAttention')).toHaveCount(0);
    await expect(page.locator('#dashRight')).not.toContainText('気にかけたい人');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2: 該当者がいれば、名前・事実・声かけが出る
  // ==========================================================
  test('該当者がいるとカードが出て、名前・事実・声かけが表示される', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, [ITEM]);
    await openDashboard(page);

    const box = page.locator('#dashAttention');
    await expect(box).toHaveCount(1);
    await expect(box).toContainText('田中太郎');
    await expect(box).toContainText('出勤が減っています');
    // 店長が判断できる材料（事実）が出ていること
    await expect(box).toContainText('直近30日は4日');
    // 声かけの例が出ていること
    await expect(box).toContainText('ご都合はいかがですか');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース3: 決めつけを戒める但し書きが常に出る
  // ==========================================================
  test('決めつけずに伺うよう促す但し書きが出る', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, [ITEM]);
    await openDashboard(page);
    await expect(page.locator('#dashAttention')).toContainText('決めつけずに伺ってください');
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース4: 原因や状態を断定する語を画面に出さない
  // ==========================================================
  test('断定的な語がカードに出ない', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await stubAttention(page, [ITEM]);
    await openDashboard(page);
    const text = await page.locator('#dashAttention').textContent();
    for (const w of ['離職', '退職', 'メンタル', 'やる気']) {
      expect(text).not.toContain(w);
    }
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース5: 取得に失敗してもダッシュボードの他は出る
  // ==========================================================
  test('取得に失敗してもダッシュボードの他の部分は表示される', async ({ page }) => {
    await stubAttention(page, [], 500);
    await openDashboard(page);   // 「AIからの提案」が出ることを待っている
    await expect(page.locator('#dashAttention')).toHaveCount(0);
    await expect(page.locator('#dashRight')).toContainText('クイック操作');
  });
});
