/**
 * e2e/capture_mobile.spec.js — スマホ版 操作説明書用のスクリーンショット撮影。
 *
 * テストではなく撮影スクリプト。screenshots/mobile/ に出力する
 * （screenshots/ は .gitignore 済み）。
 *
 * 実行: npx playwright test --config playwright.capture.config.js
 *
 * 空の画面を撮っても説明にならないので、店舗・スタッフ・パターン・確定シフト・
 * 募集期間を作ってから撮る。日付は 2026年8月（システム日付の当月）に揃える。
 */
const fs = require('fs');
const path = require('path');
const { test, expect, devices } = require('@playwright/test');
const { ensureShop } = require('./helpers');

const RUN_ID = Date.now().toString(36);
const OUT = path.resolve(__dirname, '..', 'screenshots', 'mobile');

const SHOP = {
  shopCode: `MOB_${RUN_ID}`,
  shopName: 'カフェ・ひだまり',
  managerCode: `mobmgr_${RUN_ID}`,
  managerPassword: 'Mgr12345a',
  managerName: '店長',
};

const STAFF_PW = 'Stf1234a';
const STAFF = {
  code: `MOBS_${RUN_ID}`,
  name: '山本さくら',
};

// 撮影対象の月。ダッシュボードやマイシフトが空にならないよう、
// この月に確定シフトを入れておく。
const YM = '2026-08';

let shopHdr;
let staffIds = {};

/** iPhone 相当の縦画面。実機に近い比率で撮る。 */
test.use({ ...devices['iPhone 14 Pro'] });
test.describe.configure({ mode: 'serial' });

async function shot(page, name) {
  await page.waitForTimeout(700);
  await page.screenshot({ path: path.join(OUT, `${name}.png`) });
}

/** ログイン画面から入る（スマホの導線をそのまま撮るため API ログインは使わない）。 */
async function login(page, userCode, password) {
  await page.goto('/');
  await page.fill('#loginShopCode', SHOP.shopCode);
  await page.fill('#loginUserCode', userCode);
  await page.fill('#loginPassword', password);
  await page.click('#loginBtn');
  await page.waitForSelector('#appView:not(.d-none)', { timeout: 15000 });
}

/** ボトムナビ or サイドメニューから画面を開く。
 *  data-screen はサイドナビ（.side-item）とボトムナビ（.bn-item）の両方に付く。
 *  スマホではサイドナビが隠れているので、先にボトムナビを探す。 */
async function goScreen(page, key) {
  const bn = page.locator(`.bn-item[data-screen="${key}"]`);
  if (await bn.count() && await bn.isVisible().catch(() => false)) {
    await bn.click();
  } else {
    // ボトムナビに出ない画面（NAV_DEFS で mobile が無いもの）はハンバーガーから
    await page.click('#menuToggle');
    await page.waitForTimeout(500);
    await page.locator(`.side-item[data-screen="${key}"]`).click();
  }
  await page.waitForTimeout(900);
}

test.describe('スマホ版 操作説明書キャプチャ', () => {
  test.beforeAll(async ({ request }) => {
    fs.mkdirSync(OUT, { recursive: true });
    await ensureShop(request, SHOP);
    const res = await request.post('/api/login', {
      data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
    });
    shopHdr = { Authorization: `Bearer ${(await res.json()).token}` };

    // --- スタッフ（説明書に名前が出るので、それらしい名前にする）---
    const people = [
      [STAFF.code, STAFF.name, 'part_time'],
      [`MOBE_${RUN_ID}`, '佐藤健一', 'employee'],
      [`MOBP_${RUN_ID}`, '鈴木みなみ', 'part_time'],
      [`MOBT_${RUN_ID}`, '李ミンス', 'foreign_worker'],
    ];
    for (const [code, name, role] of people) {
      const r = await request.post('/api/shop/staffs', {
        data: { staff_code: code, name, password: STAFF_PW, role, hourly_wage: 1150 },
        headers: shopHdr,
      });
      staffIds[name] = (await r.json()).id;
    }

    // --- シフトパターン ---
    for (const [name, st, en, req] of [['早番', '09:00', '15:00', 1], ['遅番', '15:00', '21:00', 1]]) {
      await request.post('/api/shop/patterns', {
        data: { pattern_name: name, start_time: st, end_time: en, required_staff: req },
        headers: shopHdr,
      });
    }

    // --- 募集期間（希望提出画面に出る）---
    await request.post('/api/shop/periods', {
      data: { start_date: `${YM}-01`, end_date: `${YM}-31`, deadline: `${YM}-05`, is_active: true },
      headers: shopHdr,
    });

    // --- 確定シフト（カレンダーと集計が空にならないように）---
    const plan = [
      ['山本さくら', '03', '09:00', '15:00'], ['佐藤健一', '03', '15:00', '21:00'],
      ['山本さくら', '05', '15:00', '21:00'], ['鈴木みなみ', '05', '09:00', '15:00'],
      ['山本さくら', '07', '09:00', '15:00'], ['李ミンス', '07', '15:00', '21:00'],
      ['佐藤健一', '10', '09:00', '15:00'], ['山本さくら', '12', '15:00', '21:00'],
      ['鈴木みなみ', '12', '09:00', '15:00'], ['山本さくら', '14', '09:00', '15:00'],
    ];
    for (const [name, d, st, en] of plan) {
      await request.post('/api/shop/shifts', {
        data: {
          staff_id: staffIds[name],
          start_datetime: `${YM}-${d}T${st}:00`,
          end_datetime: `${YM}-${d}T${en}:00`,
          status: 'confirmed',
        },
        headers: shopHdr,
      });
    }
  });

  // ==========================================================
  // スタッフの画面
  // ==========================================================
  test('スタッフ画面', async ({ page }) => {
    // ログイン画面（アプリの入口）
    await page.goto('/');
    await page.waitForSelector('#loginBtn', { timeout: 15000 });
    await shot(page, 's01-login');

    await login(page, STAFF.code, STAFF_PW);
    await shot(page, 's02-home');

    await goScreen(page, 'staffMyshift');
    await shot(page, 's03-myshift');

    await goScreen(page, 'request');
    await shot(page, 's04-request');

    await goScreen(page, 'staffSettings');
    await shot(page, 's05-settings');
  });

  // ==========================================================
  // 店長の画面
  // ==========================================================
  test('店長画面', async ({ page }) => {
    await login(page, SHOP.managerCode, SHOP.managerPassword);
    await shot(page, 'm01-dashboard');

    await goScreen(page, 'shifts');
    await shot(page, 'm02-shifts');

    await goScreen(page, 'aiGenerate');
    await shot(page, 'm03-ai-generate');

    await goScreen(page, 'staffs');
    await shot(page, 'm04-staffs');

    await goScreen(page, 'requests');
    await shot(page, 'm05-requests');

    await goScreen(page, 'analytics');
    await shot(page, 'm06-analytics');

    await goScreen(page, 'settings');
    await shot(page, 'm07-settings');
  });

  // ==========================================================
  // 気にかけたい人（働き方の変化）
  // ==========================================================
  test('気にかけたい人カード', async ({ page, request }) => {
    // 実データで検出させる。基準期間（30〜89日前）に多く出勤し、
    // 直近30日はわずか、という状態を作る。
    const today = new Date();
    const iso = (daysAgo) => {
      const d = new Date(today);
      d.setDate(d.getDate() - daysAgo);
      return d.toISOString().slice(0, 10);
    };
    const days = [];
    for (let i = 31; i <= 70; i += 2) days.push(i);   // 基準期間に20日
    days.push(4);                                      // 直近30日は1日だけ
    for (const d of days) {
      await request.post('/api/shop/shifts', {
        data: {
          staff_id: staffIds['鈴木みなみ'],
          start_datetime: `${iso(d)}T09:00:00`,
          end_datetime: `${iso(d)}T17:00:00`,
          status: 'confirmed',
        },
        headers: shopHdr,
      });
    }
    await login(page, SHOP.managerCode, SHOP.managerPassword);
    await page.waitForSelector('#dashAttention', { timeout: 20000 });
    await page.locator('#dashAttention').scrollIntoViewIfNeeded();
    await shot(page, 'm10-attention');
  });

  // ==========================================================
  // スマホ特有の導線（メニュー・希望の入力）
  // ==========================================================
  test('メニューと入力の導線', async ({ page }) => {
    await login(page, SHOP.managerCode, SHOP.managerPassword);
    // ハンバーガーメニューを開いた状態
    await page.click('#menuToggle');
    await shot(page, 'm08-menu-open');
    // 同じボタンでトグルして閉じる。開いたままだとオーバーレイが
    // ボトムナビのクリックを遮る（Escape でもオーバーレイ経由でも閉じない）。
    await page.click('#menuToggle');
    await page.waitForTimeout(500);

    // 希望表の取り込みモーダル（スマホでは「写真を撮る」が出る）
    await goScreen(page, 'requests');
    const importBtn = page.locator('#reqImportBtn');
    if (await importBtn.count()) {
      await importBtn.click();
      await page.waitForSelector('#wtiImageDrop', { timeout: 10000 });
      await shot(page, 'm09-import-mobile');
    }
  });
});
