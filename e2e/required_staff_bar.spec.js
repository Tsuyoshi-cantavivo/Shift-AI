/**
 * e2e/required_staff_bar.spec.js — 必要人数のバーUI。
 *
 * 実行: npx playwright test e2e/required_staff_bar.spec.js
 */
const { test, expect } = require('@playwright/test');
const { ensureShop, loginAsManager } = require('./helpers');

const SHOP = {
  shopCode: 'REQ1',
  shopName: '必要人数テスト店',
  managerCode: 'mgr1',
  managerPassword: 'mgr1pass',
  managerName: '必要人数店長',
};

/** 時間帯パターンを2件仕込む。バーが2本描かれる状態を作る。 */
async function seedPatterns(request, token) {
  for (const p of [
    { pattern_name: '早番', start_time: '09:00', end_time: '17:00', required_staff: 2 },
    { pattern_name: '夜番', start_time: '17:00', end_time: '22:00', required_staff: 3 },
  ]) {
    await request.post('/api/shop/patterns', {
      headers: { Authorization: `Bearer ${token}` },
      data: p,
    });
  }
}

/** SHOP は全テストで shopCode を共有しているため、既存パターンを消してから
 *  仕込まないと「バーが2本」の前提がテスト実行順に依存して崩れる
 *  （前のテストの beforeEach が作った早番/夜番が積み上がり、
 *  data-name="早番" が複数ヒットして strict mode violation になる）。 */
async function clearPatterns(request, token) {
  const d = await (await request.get('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
  })).json();
  for (const p of d.patterns || []) {
    await request.delete(`/api/shop/patterns/${p.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }
}

test.beforeEach(async ({ page, request }) => {
  await ensureShop(request, SHOP);
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await clearPatterns(request, token);
  await seedPatterns(request, token);

  await loginAsManager(page, {
    shopCode: SHOP.shopCode,
    managerCode: SHOP.managerCode,
    password: SHOP.managerPassword,
  });
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');
});

test('時間帯がバーとして描画される', async ({ page }) => {
  await expect(page.locator('.rq-bar')).toHaveCount(2);
  await expect(page.locator('.rq-bar[data-name="早番"]')).toBeVisible();
});

test('バーの高さが必要人数に比例する', async ({ page }) => {
  const heights = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).getBoundingClientRect().height;
    return { asa: get('早番'), yoru: get('夜番') };
  });
  // 早番2人 / 夜番3人 → 夜番のほうが高い
  expect(heights.yoru).toBeGreaterThan(heights.asa);
});

test('曜日タブを切り替えられる', async ({ page }) => {
  await expect(page.locator('#reqBarTabs .rq-tab')).toHaveCount(8);  // 基本 + 日〜土
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  await expect(page.locator('#reqBarTabs .rq-tab[data-wd="6"]')).toHaveClass(/active/);
});

test('曜日別の人数が未設定なら基本値が表示される', async ({ page }) => {
  await page.click('#reqBarTabs .rq-tab[data-wd="6"]');
  const v = await page.inputValue('.rq-count[data-name="早番"]');
  expect(v).toBe('2');
  // 上書きではないことがクラスで分かる
  await expect(page.locator('.rq-bar[data-name="早番"]')).not.toHaveClass(/rq-override/);
});

test('時間帯が0件のとき案内が出る', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  const d = await (await request.get('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
  })).json();
  for (const p of d.patterns) {
    await request.delete(`/api/shop/patterns/${p.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await expect(page.locator('#reqBarEmpty')).toBeVisible();
});

// レビュー Critical C1: 数値欄の input で行DOMを再構築すると、入力中の
// input 要素自体が破棄されてフォーカスが飛び、2文字目以降が入らなくなる
// （10人以上を打てない）。page.fill() は値を一括設定するため、この
// フォーカス喪失を再現しない。1文字ずつキー入力する pressSequentially で検証する。
test('数値欄に複数桁を1文字ずつ入力できる（10人以上）', async ({ page }) => {
  const inp = page.locator('.rq-count[data-name="早番"]');
  await inp.click();
  await inp.fill('');
  await inp.pressSequentially('12', { delay: 30 });
  await expect(inp).toHaveValue('12');
  // 入力欄の値だけでなく、state とバー側にも反映されていることを見る
  // （レビュー指摘 Minor N3: 表示だけ直って中身が伴っていない可能性）。
  await expect(page.locator('.rq-bar[data-name="早番"] .rq-bar-label')).toContainText('12人');
  // blur しても入力した値のまま（正規化で消えたりしないこと）。
  await inp.blur();
  await expect(inp).toHaveValue('12');
});

// レビュー Minor N3: C1 のうち最も危険な経路。空欄のまま離れると
// parseInt('')=NaN → 0（＝Task1の契約で「募集しない」）に誤って
// 保存されかねない。直前の正常値に戻ることを検証する。
test('数値欄を空にしたまま離れると元の値に戻る（0に落ちない）', async ({ page }) => {
  const inp = page.locator('.rq-count[data-name="早番"]');
  await inp.click();
  await inp.fill('');
  await inp.blur();
  await expect(inp).toHaveValue('2');
  await expect(page.locator('.rq-bar[data-name="早番"]')).not.toHaveClass(/rq-zero/);
});

// レビュー Important N1（新規回帰）: blur で #reqBarBody 全体を再描画すると、
// mousedown → blur（再描画で対象ボタンが消える）→ mouseup の順になり、
// 行内ボタンへの1回目のクリックが空振りする。
test('数値欄にフォーカスしてから＋ボタンを押すと人数が増える', async ({ page }) => {
  const row = page.locator('.rq-row').filter({ has: page.locator('.rq-count[data-name="早番"]') });
  const inp = row.locator('.rq-count');
  await inp.click();   // フォーカスするだけで値は変えない
  await row.locator('.rq-step[data-step="1"]').click();
  // 早番の基本値は2人。空振りしていなければ1回のクリックで3人になる。
  await expect(page.locator('.rq-bar[data-name="早番"] .rq-bar-label')).toContainText('3人');
});

// レビュー Important N1: 時間帯Aの欄を打った直後に時間帯Bの欄へ移ってそのまま
// 打つと、blurの全体再描画でフォーカスがbodyに落ちて入力が飲まれる
// （利用者からはC1と同じ症状に見える）。
// 注意: locator.click()/pressSequentially は内部でアクショナビリティの
// 再試行・再フォーカスを行うため、レース1回分のフォーカス消失を
// 自己修復してしまい検知できないことがある（実測済み）。ここでは
// クリック直後に document.activeElement を直接確認し、そのあとは
// フォーカスを取り直さない page.keyboard.type で打鍵することで、
// レビューが実測した「フォーカスがbodyに落ちる」症状そのものを検証する。
test('時間帯Aの欄を打った直後に時間帯Bの欄へ移って打つとBに入る', async ({ page }) => {
  const inpA = page.locator('.rq-count[data-name="早番"]');
  const inpB = page.locator('.rq-count[data-name="夜番"]');
  await inpA.click();
  await inpA.fill('');
  await inpA.pressSequentially('5', { delay: 30 });
  await inpB.click();   // Aからフォーカスが外れ、Bへ移る
  await expect(inpB).toBeFocused();
  // fill() 等の Locator 操作は再フォーカスを含むため使わない。
  // page.keyboard だけで全選択→上書きする。
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.type('7', { delay: 30 });
  await expect(inpB).toHaveValue('7');
});

// レビュー Important I5: 時間帯が重なると不透明なバーが互いを隠して読めなくなる。
// 早番(09:00-17:00)と重なる「中番」(12:00-20:00)を追加し、縦に重ならない
// （別の段に描かれる）ことを実測で確認する。
test('重なる時間帯はバーが縦に重ならない', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await request.post('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
    data: { pattern_name: '中番', start_time: '12:00', end_time: '20:00', required_staff: 2 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');
  await expect(page.locator('.rq-bar')).toHaveCount(3);

  const rects = await page.evaluate(() => {
    const get = (n) => document.querySelector(`.rq-bar[data-name="${n}"]`).getBoundingClientRect();
    return { asa: get('早番'), naka: get('中番') };
  });
  const overlapsVertically = (a, b) => a.top < b.bottom && b.top < a.bottom;
  // 早番(09-17)と中番(12-20)は時間が重なる → 別の段に描かれ、縦にも重ならない
  expect(overlapsVertically(rects.asa, rects.naka)).toBe(false);
});

// レビュー Important I4: 0人は「募集しない」（Task1の契約）。斜線だけでは
// 判別できないため、ラベル自体が「募集しない」に切り替わることを検証する。
test('0人のバーは「募集しない」と表示される', async ({ page, request }) => {
  const res = await request.post('/api/login', {
    data: { shop_code: SHOP.shopCode, user_code: SHOP.managerCode, password: SHOP.managerPassword },
  });
  const token = (await res.json()).token;
  await request.post('/api/shop/patterns', {
    headers: { Authorization: `Bearer ${token}` },
    data: { pattern_name: '深夜', start_time: '22:00', end_time: '23:00', required_staff: 0 },
  });
  await page.reload();
  await page.waitForSelector('#appView:not(.d-none)');
  await page.click('.side-item[data-screen="settings"]');
  await page.waitForSelector('#reqBarTrack');

  const bar = page.locator('.rq-bar[data-name="深夜"]');
  await expect(bar).toHaveClass(/rq-zero/);
  await expect(bar.locator('.rq-bar-label')).toContainText('募集しない');
});
