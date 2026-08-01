"""tests/test_shortage_no_patterns.py — 時間帯0件のときに「充足」と嘘をつかないこと。

実行: ./.venv/bin/python -m pytest tests/test_shortage_no_patterns.py -v

【背景】
新規店舗は shift_patterns も shift_request_periods もゼロ件で始まる
（src/admin_api.py の店舗作成が shops と manager しか作らない）。
時間帯（shift_patterns）が0件だと _computeHourlyGaps は常に空配列を返すため、
そのまま進むとシフト画面の #shortageBox が緑のチェックマークで
「不足なし — 全時間帯充足」と表示していた。店長が最初に見る画面が嘘をついていた。
ダッシュボードKPI（「今日の出勤」）も同じ理由で「充足」と表示されていた
（today_shortage はサーバ側で shift_patterns を元に計算するため、
パターン0件なら常に0になる）。

【方針】
public/app.js のロジックをテスト側に書き写すと、実装が変わっても
テストが緑のまま乖離する（Phase 1/2 で実際にその型の空振りテストが
見つかっている）。そのため helpers.extract_js_function で実装の関数定義
そのものを取り出し、Node で実際に実行して検証する。

- loadShortage: 非同期関数のため、DOM/api/ensureBusinessHours 等を
  最小限のスタブに差し替えて Node 上で実行する（このファイル内の
  _run_load_shortage が実行器。tests/helpers.run_js は同期式のみ対応の
  ため使わず、同じ思想で非同期版を自前で用意した）。
- attendanceShortageKpi: 依存の無い純関数として切り出したので
  helpers.run_js でそのまま呼べる。
"""
import json
import os
import subprocess

import pytest

from helpers import extract_js_function, run_js

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    with open(os.path.join(ROOT, "public/app.js"), encoding="utf-8") as f:
        return f.read()


def _extract_async(source, name):
    """helpers.extract_js_function は "function name(" にアンカーするため、
    直前の "async " 修飾子を取りこぼす（抜き出した断片が async 関数として
    実行できなくなる）。ここでソース上の "async function name(" の有無を見て
    必要なら補う。共有ヘルパ（他のテストも使う）は変えず、この呼び出し元だけで吸収する。"""
    fn_src = extract_js_function(source, name)
    if f"async function {name}" in source and not fn_src.startswith("async "):
        fn_src = "async " + fn_src
    return fn_src


def _run_load_shortage(patterns_json):
    """loadShortage(box, start, end) を Node 上で実行し、結果を dict で返す。

    appState.patterns / businessHours は ensureBusinessHours のスタブが埋める
    （実物は /shop/patterns を叩くネットワーク呼び出しのため差し替える）。
    api はエラーにせず常に「シフト0件」を返す。ガードが外れて先へ進んでも
    例外で誤魔化さず、パターン0件のまま本来のロジックに到達させることで
    「全時間帯充足」の嘘が本当に再現するかまで確認できるようにする。
    box は innerHTML と querySelector だけを持つ最小限のスタブDOM。
    戻り値: {html, apiCalled, hasSettingsButton, navigatedTo}
    """
    src = _src()
    fn_src = _extract_async(src, "loadShortage")
    # loadShortage はパターンが存在する場合に _computeHourlyGaps / _mergeHourlyGaps /
    # _fmtExtHour / _localDateStr / esc を直接呼ぶ。テスト側に計算式を書き写すと
    # 実装の変更に追従できなくなるため、依存関数も実ソースから抜き出して使う。
    dep_names = ["_computeHourlyGaps", "_mergeHourlyGaps", "_fmtExtHour", "_localDateStr",
                 "esc", "_parseTimeParts", "_extMinFromIso", "_dateDiffDays"]
    deps_src = "\n".join(extract_js_function(src, n) for n in dep_names)
    script = f"""
{deps_src}
{fn_src}

const appState = {{ patterns: null, businessHours: null }};
async function ensureBusinessHours() {{
  appState.patterns = {patterns_json};
  appState.businessHours = {{ start: 9, end: 22 }};
  return appState.businessHours;
}}
function navToken() {{ return 1; }}
function isAlive(t) {{ return true; }}
let navigatedTo = null;
function navigateTo(screen) {{ navigatedTo = screen; }}
let apiCalled = false;
async function api(path) {{
  apiCalled = true;
  return {{ shifts: [] }};
}}

class FakeBox {{
  constructor() {{ this._html = ''; this.isConnected = true; this._handlers = {{}}; }}
  set innerHTML(v) {{ this._html = v; }}
  get innerHTML() {{ return this._html; }}
  querySelector(sel) {{
    const id = sel.replace('#', '');
    if (this._html.includes(id)) {{
      return {{ addEventListener: (evt, cb) => {{ this._handlers[evt] = cb; }} }};
    }}
    return null;
  }}
}}

(async () => {{
  const box = new FakeBox();
  await loadShortage(box, '2026-08-01', '2026-08-07');
  const hasSettingsButton = box._html.includes('shortageGoSettings');
  if (box._handlers.click) box._handlers.click();
  process.stdout.write(JSON.stringify({{
    html: box._html,
    apiCalled,
    hasSettingsButton,
    navigatedTo,
  }}));
}})();
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                             encoding="utf-8", timeout=10)
    assert result.returncode == 0, f"Node実行に失敗: {result.stderr}\n--- script ---\n{script}"
    return json.loads(result.stdout)


# ============================================================
# パターン0件 — 「充足」と嘘をつかないこと
# ============================================================
class TestLoadShortageWithoutPatterns:
    def test_shows_setup_guidance_message(self):
        """パターン0件のときは「時間帯が未設定」の案内を出すこと。"""
        out = _run_load_shortage("[]")
        assert "時間帯が未設定" in out["html"], f"案内が出ていない: {out['html']!r}"

    def test_does_not_claim_fulfilled(self):
        """パターン0件のときに「全時間帯充足」（嘘）を出さないこと。"""
        out = _run_load_shortage("[]")
        assert "全時間帯充足" not in out["html"], f"嘘の充足表示が出ている: {out['html']!r}"

    def test_does_not_fetch_shifts(self):
        """パターン0件のときは /shop/shifts を叩かず即座に案内を出して抜けること。"""
        out = _run_load_shortage("[]")
        assert out["apiCalled"] is False, "パターン0件なのに /shop/shifts を叩いている"

    def test_settings_button_navigates_to_settings(self):
        """案内の「設定を開く」ボタンが設定画面へ遷移させること。"""
        out = _run_load_shortage("[]")
        assert out["hasSettingsButton"], "設定への導線ボタンが無い"
        assert out["navigatedTo"] == "settings", \
            f"ボタンを押しても設定画面に遷移しない: {out['navigatedTo']!r}"


# ============================================================
# パターンがある場合 — 従来の判定が生きていること（回帰防止）
# ============================================================
class TestLoadShortageWithPatterns:
    def test_still_reports_fulfilled_when_patterns_exist_and_no_gap(self):
        """パターンがあり不足が無い場合は、従来どおり「全時間帯充足」を出すこと。

        required_staff=0 のパターンなら _computeHourlyGaps は常に空配列を
        返すため、シフトが無くても不足0件になる。
        """
        patterns = json.dumps([
            {"start_time": "09:00", "end_time": "18:00", "required_staff": 0},
        ])
        out = _run_load_shortage(patterns)
        assert "全時間帯充足" in out["html"], f"通常時の充足表示が消えている: {out['html']!r}"
        assert "時間帯が未設定" not in out["html"]

    def test_fetches_shifts_when_patterns_exist(self):
        """パターンがあるときは通常どおり /shop/shifts を取得すること。"""
        patterns = json.dumps([
            {"start_time": "09:00", "end_time": "18:00", "required_staff": 0},
        ])
        out = _run_load_shortage(patterns)
        assert out["apiCalled"] is True


# ============================================================
# ダッシュボードKPI「今日の出勤」— 同じ嘘をつかないこと
# ============================================================
class TestAttendanceShortageKpi:
    """attendanceShortageKpi(hasPatterns, todayShortage) の純関数テスト。"""

    def _call(self, has_patterns, today_shortage):
        frag = extract_js_function(_src(), "attendanceShortageKpi")
        js_bool = "true" if has_patterns else "false"
        out = run_js([frag], f"JSON.stringify(attendanceShortageKpi({js_bool}, {today_shortage}))")
        return json.loads(out)

    def test_no_patterns_shows_unset_label_not_fulfilled(self):
        """パターン0件のときは today_shortage が0でも「充足」ではなく専用文言を出すこと。"""
        r = self._call(False, 0)
        assert r["text"] == "時間帯未設定", f"「充足」と嘘をついている: {r}"
        assert r["variant"] != "green", f"未設定なのに緑（安全表示）になっている: {r}"

    def test_patterns_present_and_no_shortage_is_fulfilled(self):
        r = self._call(True, 0)
        assert r["text"] == "充足"
        assert r["variant"] == "green"

    def test_patterns_present_and_shortage_reports_count(self):
        r = self._call(True, 3)
        assert r["text"] == "3枠不足"
        assert r["variant"] == "amber"


# ============================================================
# 配線（純関数が実際にダッシュボード描画から呼ばれているか）
# ============================================================
class TestDashboardWiresAttendanceKpi:
    """SCREENS.dashboard 本体は DOM 依存が重く直接実行しないため、
    ソース上で純関数が正しく配線されていることをテキストで確認する。
    """

    def test_dashboard_uses_attendance_shortage_kpi(self):
        js = _src()
        idx = js.find("SCREENS.dashboard = async function")
        assert idx > 0, "SCREENS.dashboard の定義が見つからない（構造が変わった？）"
        # 次の同レベル定義（次のSCREENS.*代入 or ファイル末尾）までを大まかに切り出す
        next_idx = js.find("\nSCREENS.", idx + 10)
        body = js[idx: next_idx if next_idx > 0 else idx + 6000]
        assert "attendanceShortageKpi(hasPatterns" in body, \
            "ダッシュボードKPIが attendanceShortageKpi を使っていない"
        assert "appState.patterns" in body, \
            "ダッシュボードKPIが appState.patterns を参照していない"
