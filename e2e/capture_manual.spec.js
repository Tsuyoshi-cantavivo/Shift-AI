/**
 * e2e/capture_manual.spec.js — 操作説明書用のスクリーンショット撮影。
 *
 * テストではなく撮影スクリプト。screenshots/manual/ に出力する
 * （screenshots/ は .gitignore 済み）。
 *
 * 実行: npx playwright test e2e/capture_manual.spec.js
 */
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const OUT = path.resolve(__dirname, '..', 'screenshots', 'manual');
const FIXTURE_PNG = path.resolve(__dirname, 'fixtures', 'wti-test.png');

const SHOP = {
  shopCode: `CAP_${RUN_ID}`,
  shopName: 'キャプチャ用店舗',
  managerCode: `capmgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

let shopHdr;
let staffIds = {};

/** モーダルのフェードイン完了を待ってから撮る。待たないと中身が半透明で写る。 */
async function shot(page, name, selector) {
  await page.waitForTimeout(700);
  const target = selector ? page.locator(selector).first() : page;
  await target.screenshot({ path: path.join(OUT, `${name}.png`) });
}
const MODAL = '.modal-overlay';

test.describe.configure({ mode: 'serial' });

test.describe('操作説明書用キャプチャ', () => {
  test.beforeAll(async ({ request }) => {
    fs.mkdirSync(OUT, { recursive: true });
    await ensureShop(request, SHOP);
    const res = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    shopHdr = { Authorization: `Bearer ${(await res.json()).token}` };
    for (const [key, [code, name, role]] of Object.entries({
      a: [`CAPA_${RUN_ID}`, '田中太郎', 'part_time'],
      b: [`CAPB_${RUN_ID}`, '田中花子', 'part_time'],
      c: [`CAPC_${RUN_ID}`, '佐藤次郎', 'employee'],
    })) {
      const r = await request.post('/api/shop/staffs', {
        data: { staff_code: code, name, password: 'Stf1234a', role },
        headers: shopHdr,
      });
      staffIds[key] = (await r.json()).id;
    }
  });

  test('01 画像取込のステップ1（画像ゾーン）', async ({ page }) => {
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
    await page.click('#reqImportBtn');
    await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
    await shot(page, '01-import-step1', MODAL);

    // 画像を選んだ状態（サムネイル表示）
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await page.waitForSelector('.wti-image-thumb', { timeout: 10000 });
    await shot(page, '02-import-thumbnail', MODAL);
  });

  test('03 確認画面のOCR全文と警告文言', async ({ page }) => {
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
    await page.click('#reqImportBtn');
    await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);

    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse-image'), (route) =>
      route.fulfill({
        json: {
          entries: [
            { staff_id: staffIds.c, staff_hint: '佐藤', dates: ['2026-08-10'], availability: 'rest', start: null, end: null, raw: '8/10 佐藤 休み', raw_verified: true },
            { staff_id: null, staff_hint: '田中', dates: ['2026-08-12'], availability: 'rest', start: null, end: null, raw: '8/12 田中 休み', raw_verified: false },
          ],
          unparsed: [], source: 'llm',
          ocr_text: '8/10 佐藤 休み\n8/12 田中 休み',
          name_candidates: {
            1: [
              { staff_id: staffIds.a, name: '田中太郎', score: 0.85, reason: '姓が一致' },
              { staff_id: staffIds.b, name: '田中花子', score: 0.85, reason: '姓が一致' },
            ],
          },
        },
      }));
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    await shot(page, '03-import-step2', MODAL);

    // OCR全文の折りたたみを開く
    const det = page.locator('.wti-ocr-details');
    if (await det.count()) {
      await det.locator('summary').first().click();
      await page.waitForTimeout(300);
      await shot(page, '04-ocr-text', MODAL);
    }

    // 名前候補の確認UI（未割り当て欄）
    const cand = page.locator('.wti-cand-list').first();
    if (await cand.count()) {
      await cand.scrollIntoViewIfNeeded();
      await page.waitForTimeout(200);
      await shot(page, '05-name-candidates', MODAL);
      // 候補を選ぶと確定ボタンに名前が出る
      await page.locator('.wti-cand-option input[type="radio"]').first().click();
      await page.waitForTimeout(200);
      await shot(page, '06-name-candidates-selected', MODAL);
    }
  });

  test('08 確認画面カレンダー: 他のスタッフの希望がある日', async ({ page }) => {
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await page.click('button[data-screen="requests"]');
    await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
    await page.click('#reqImportBtn');
    await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);

    // 希望表の写真は複数人が1枚に写る。カレンダーは1人分ずつ表示するので、
    // 他の人の希望がある日に印が出ることを見せる。
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse-image'), (route) =>
      route.fulfill({
        json: {
          entries: [
            { staff_id: staffIds.a, staff_hint: '田中', dates: ['2026-08-10'], availability: 'rest', start: null, end: null, raw: '8/10 田中 休み', raw_verified: true },
            { staff_id: staffIds.b, staff_hint: '花子', dates: ['2026-08-12'], availability: 'rest', start: null, end: null, raw: '8/12 花子 休み', raw_verified: true },
            { staff_id: null, staff_hint: 'ヤマダ', dates: ['2026-08-14'], availability: 'rest', start: null, end: null, raw: '8/14 ヤマダ 休み', raw_verified: true },
          ],
          unparsed: [], source: 'llm',
          ocr_text: '8/10 田中 休み\n8/12 花子 休み\n8/14 ヤマダ 休み',
          name_candidates: {},
        },
      }));
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    // カレンダーが見えるところまでスクロールしてから撮る
    await page.locator('.wish-cell[data-day="2026-08-12"]').scrollIntoViewIfNeeded();
    await shot(page, '08-calendar-other-staff', MODAL);
  });

  test('09 ダッシュボード: 気にかけたい人', async ({ page, request }) => {
    // 実データで検出させる（スタブしない）。基準期間（30〜89日前）に多く出勤し、
    // 直近30日はわずか、という状態を作る。
    const today = new Date();
    const iso = (daysAgo) => {
      const d = new Date(today);
      d.setDate(d.getDate() - daysAgo);
      return d.toISOString().slice(0, 10);
    };
    const days = [];
    for (let i = 31; i <= 70; i += 2) days.push(i);   // 基準期間に20日
    days.push(5);                                      // 直近30日は1日だけ
    for (const d of days) {
      await request.post('/api/shop/shifts', {
        data: {
          staff_id: staffIds.c,
          start_datetime: `${iso(d)}T09:00:00`,
          end_datetime: `${iso(d)}T17:00:00`,
          status: 'confirmed',
        },
        headers: shopHdr,
      });
    }
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await page.waitForSelector('#dashAttention', { timeout: 20000 });
    await shot(page, '09-dashboard-attention', '#dashRight');
  });

  test('07 スタッフ管理のロール選択（外国籍アルバイト）', async ({ page }) => {
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await page.click('button[data-screen="staffs"]');
    await page.waitForSelector('#addStaffBtn', { timeout: 10000 });
    await page.click('#addStaffBtn');
    await page.waitForSelector('#f_role', { timeout: 10000 });
    await page.selectOption('#f_role', 'foreign_worker');
    await page.waitForTimeout(300);
    await shot(page, '07-role-foreign-worker', MODAL);
  });
});
