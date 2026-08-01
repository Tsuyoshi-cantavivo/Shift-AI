/**
 * e2e/wish_image_import.spec.js — 希望表「画像」取り込み（貼り付け・ファイル選択/D&D・
 * 撮影）の e2e テスト。
 *
 * ブリーフ: .superpowers/sdd/2026-08-02-phase3-wish-image-import/task-5-brief.md
 * 対象実装: public/app.js の _wtiRenderStep1() 内の画像ゾーンと reqImageResize()、
 *           _wtiParse() の /wishes/parse-image 分岐。
 *
 * 既存の e2e/wish_text_import.spec.js と作法を揃える:
 *   - /wishes/parse・/wishes/parse-image は page.route でスタブし、実LLMには依存しない
 *     （実画像をLLMに投げない）。
 *   - ステップ2以降（確認画面・カレンダー・bulk確定）の挙動は wish_text_import 側で
 *     既にカバー済みのため、このファイルではステップ1（画像ゾーン）の挙動と
 *     parse-image への分岐だけを対象にする。
 */
const fs = require('fs');
const path = require('path');
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager, attachConsoleCollector } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const STAFF_PW = 'Stf1234a';

const SHOP = {
  shopCode: `WII_${RUN_ID}`,
  shopName: '希望画像取込テスト店',
  managerCode: `wiimgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

const FIXTURE_PNG = path.resolve(__dirname, 'fixtures', 'wti-test.png');

let shopHdr;

/** /api/shop/wishes/parse をスタブする（テキスト経路）。 */
function stubParse(page, response) {
  return page.route(
    (url) => url.pathname.endsWith('/api/shop/wishes/parse'),
    (route) => route.fulfill({ json: response }),
  );
}

/** /api/shop/wishes/parse-image をスタブする（画像経路）。実画像をLLMに投げない。 */
function stubParseImage(page, response) {
  return page.route(
    (url) => url.pathname.endsWith('/api/shop/wishes/parse-image'),
    (route) => route.fulfill({ json: response }),
  );
}

/** 希望表管理画面を開き、「テキストから取り込む」でモーダルを開く（ステップ1）。 */
async function openImportModal(page) {
  await page.click('button[data-screen="requests"]');
  await page.waitForSelector('#reqImportBtn', { timeout: 10000 });
  await page.click('#reqImportBtn');
  await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
}

test.describe('希望画像取り込み（wish image import）', () => {
  test.beforeAll(async ({ request }) => {
    fs.mkdirSync(path.resolve(__dirname, '..', 'screenshots'), { recursive: true });
    await ensureShop(request, SHOP);
    const loginRes = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    shopHdr = { Authorization: `Bearer ${(await loginRes.json()).token}` };
    await request.post('/api/shop/periods', {
      data: { start_date: '2026-01-01', end_date: '2026-12-31', deadline: '2099-12-31' },
      headers: shopHdr,
    });
  });

  // ==========================================================
  // ケース1: 画像ゾーンが表示され、ファイルを選ぶとサムネイルが出る
  // ==========================================================
  test('画像ゾーンが表示され、ファイルを選ぶとサムネイルが出る', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);

    await expect(page.locator('#wtiImageDrop')).toBeVisible();
    await expect(page.locator('.wti-image-thumb')).toHaveCount(0);

    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await expect(page.locator('.wti-image-thumb[data-idx="0"]')).toBeVisible();
    await expect(page.locator('.wti-image-thumb')).toHaveCount(1);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース2: 画像を選んだ状態で「解析する」を押すと /wishes/parse-image が
  //          呼ばれる（テキストのみなら /wishes/parse）
  // ==========================================================
  test('画像選択時は /wishes/parse-image が呼ばれ、テキストのみなら /wishes/parse が呼ばれる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });

    let parseImageCalls = 0;
    let parseCalls = 0;
    // '/api/shop/wishes/parse-image' は '/api/shop/wishes/parse' で終わらないため、
    // endsWith だけで両者は排他的に判定できる（例: wish_text_import.spec.js の stubParse と同じ判定式）。
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse'), (route) => {
      parseCalls++;
      return route.fulfill({ json: { entries: [], unparsed: [], source: 'llm' } });
    });
    await page.route((url) => url.pathname.endsWith('/api/shop/wishes/parse-image'), (route) => {
      parseImageCalls++;
      return route.fulfill({ json: { entries: [], unparsed: [], source: 'llm', ocr_text: '' } });
    });

    // まずテキストのみ → /wishes/parse
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.fill('#wtiText', 'テキストのみのテスト');
    await page.click('#wtiParseBtn');
    await expect.poll(() => parseCalls, { timeout: 5000 }).toBeGreaterThan(0);
    expect(parseImageCalls).toBe(0);

    // 画像を追加 → /wishes/parse-image（テキストが残っていても画像経路優先）
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await expect(page.locator('.wti-image-thumb')).toHaveCount(1);
    await page.click('#wtiParseBtn');
    await expect.poll(() => parseImageCalls, { timeout: 5000 }).toBeGreaterThan(0);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース3: スタブしたレスポンスでステップ2（確認画面）へ進む
  // ==========================================================
  test('画像経路でもスタブしたレスポンスでステップ2（確認画面）へ進む', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);
    await page.selectOption('#wtiMonth', '2026-08');
    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);

    await stubParseImage(page, {
      entries: [{ staff_id: null, staff_hint: null, dates: ['2026-08-10'], availability: 'rest', start: null, end: null, raw: '8/10は休みたいです', raw_verified: true }],
      unparsed: [], source: 'llm', ocr_text: '8/10は休みたいです', name_candidates: {},
    });
    await page.click('#wtiParseBtn');
    await page.waitForSelector('#wtiSubmitBtn', { timeout: 10000 });
    // 画像経路の目印: OCR全文の折りたたみが出ている（テキスト経路には無い要素）
    await expect(page.locator('.wti-ocr-details')).toBeVisible();
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース4: サムネイルの削除ボタンで取り消せる
  // ==========================================================
  test('サムネイルの削除ボタンで取り消せる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);

    await page.setInputFiles('#wtiImageInput', [FIXTURE_PNG, FIXTURE_PNG]);
    await expect(page.locator('.wti-image-thumb')).toHaveCount(2);

    await page.locator('.wti-image-thumb[data-idx="0"] [data-del-img]').click();
    await expect(page.locator('.wti-image-thumb')).toHaveCount(1);
    expect(errors).toEqual([]);
  });

  // ==========================================================
  // ケース5: 4枚目を追加しようとすると弾かれる
  // ==========================================================
  test('4枚目を追加しようとすると弾かれる', async ({ page }) => {
    const errors = attachConsoleCollector(page);
    await loginAsManager(page, { shopCode: SHOP.shopCode, managerCode: SHOP.managerCode, password: SHOP.managerPassword });
    await openImportModal(page);

    await page.setInputFiles('#wtiImageInput', [FIXTURE_PNG, FIXTURE_PNG, FIXTURE_PNG]);
    await expect(page.locator('.wti-image-thumb')).toHaveCount(3);

    await page.setInputFiles('#wtiImageInput', FIXTURE_PNG);
    await expect(page.locator('.toast')).toContainText('3枚', { timeout: 3000 });
    await expect(page.locator('.wti-image-thumb')).toHaveCount(3);
    expect(errors).toEqual([]);
  });
});
