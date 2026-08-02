/**
 * playwright.capture.config.js — 操作説明書用スクリーンショットの撮影専用設定。
 *
 * 撮影スクリプト（e2e/capture_*.spec.js）は何も検証しないので、通常のテスト
 * スイート（playwright.config.js）からは testIgnore で外している。ただし
 * testIgnore は実行時にファイルを明示指定しても効くため、外しただけでは
 * 撮り直せなくなる。撮影はこの設定を使う。
 *
 * 実行: npx playwright test --config playwright.capture.config.js
 */
const base = require('./playwright.config.js');

module.exports = {
  ...base,
  testIgnore: undefined,
  testMatch: '**/capture_*.spec.js',
  // 撮影は「今の見た目を写す」作業なので、失敗時にリトライして
  // 中途半端な画像を残さない。
  retries: 0,
};
