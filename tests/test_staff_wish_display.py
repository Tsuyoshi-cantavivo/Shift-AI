"""スタッフ希望入力画面（SCREENS.request）の表示に関する契約テスト。

【背景】
  カレンダーのセルが希望の「種別名」しか出しておらず、17:00-22:00 と
  入力しても固定文字「時間」と表示されていた。さらに日付を開き直しても
  time 入力が value="17:00"/"22:00" のハードコードだったため、何を入力
  したのか画面から確認する手段が無かった。データ（wishState）には正しい
  値が入っており提出も正常だったので、表示側だけの欠落。

  店長側の希望テキスト取り込みカレンダー（_wtiRenderCalendar）は
  `_wtiShortTime(start)-_wtiShortTime(end)` で実際の時刻を出しており、
  そちらが同一コードベース内の正解。スタッフ側をそれに揃える。

【方針】
  描画ロジックを純関数 wishMarkText / wishPickerBodyHtml に切り出し、
  public/app.js の定義そのものを Node で実行して検証する。テスト側に
  ロジックを書き写すと実装が変わっても緑のまま乖離するため。
"""
from pathlib import Path

from helpers import extract_js_function, run_js

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

# wishMarkText / wishPickerBodyHtml が依存する既存ヘルパ
_DEPS = ["hm", "esc", "_wtiShortTime", "badge"]


def _fragments(*names):
    return [extract_js_function(APP_JS, n) for n in (*_DEPS, *names)]


def _mark(w_json):
    return run_js(_fragments("wishMarkText"), f"wishMarkText({w_json})")


def _picker(cur_json):
    return run_js(_fragments("wishPickerBodyHtml"), f"wishPickerBodyHtml({cur_json})")


# ============================================================
# カレンダーのセル表示
# ============================================================
class TestWishMarkText:
    def test_time_type_shows_actual_hours_not_the_word_jikan(self):
        """時間指定は「時間」ではなく実際の時刻を出すこと（今回の不具合の本体）。"""
        out = _mark("{type:'time',start:'2026-07-15T17:00:00',end:'2026-07-15T22:00:00'}")
        assert out == "17-22", f"実際の時刻が出ていない: {out!r}"
        assert "時間" not in out

    def test_time_type_keeps_non_zero_minutes(self):
        """分が 00 でないときは切り捨てずに出すこと。"""
        out = _mark("{type:'time',start:'2026-07-15T17:30:00',end:'2026-07-15T22:45:00'}")
        assert out == "17:30-22:45", f"分が失われている: {out!r}"

    def test_overnight_shows_end_hour_of_next_day(self):
        """日またぎ（end が翌日）でも終了時刻を出すこと。"""
        out = _mark("{type:'time',start:'2026-07-15T17:00:00',end:'2026-07-16T02:00:00'}")
        assert out == "17-2", f"日またぎの表示が壊れている: {out!r}"

    def test_flexible_types_keep_their_labels(self):
        """柔軟希望は時刻が無いので種別名のままでよい。"""
        assert _mark("{type:'rest'}") == "休み"
        assert _mark("{type:'any'}") == "いつでも"
        assert _mark("{type:'morning'}") == "早番"
        assert _mark("{type:'evening'}") == "遅番"

    def test_unset_day_renders_nothing(self):
        assert _mark("undefined") == ""
        assert _mark("null") == ""

    def test_unknown_type_falls_back_to_the_raw_value_escaped(self):
        """将来 type が増えたときに空欄で消えないこと。HTML として解釈もされないこと。"""
        out = _mark("{type:'<img src=x>'}")
        assert "<img" not in out, f"エスケープされていない: {out!r}"
        assert "&lt;img" in out


# ============================================================
# 日付を開き直したときのピッカー復元
# ============================================================
class TestWishPickerBodyHtml:
    def test_time_inputs_are_prefilled_from_the_current_wish(self):
        """入力済みの時刻が復元されること（ハードコードの 17:00/22:00 に戻らない）。"""
        html = _picker("{type:'time',start:'2026-07-15T18:30:00',end:'2026-07-15T23:15:00'}")
        assert 'value="18:30"' in html, f"開始時刻が復元されていない:\n{html}"
        assert 'value="23:15"' in html, f"終了時刻が復元されていない:\n{html}"
        assert 'value="17:00"' not in html, f"既定値のまま表示されている:\n{html}"

    def test_defaults_are_used_when_nothing_is_set_yet(self):
        html = _picker("undefined")
        assert 'value="17:00"' in html
        assert 'value="22:00"' in html

    def test_selected_category_is_visually_marked(self):
        """今どれが選ばれているかが分かること。"""
        html = _picker("{type:'rest'}")
        import re
        rest_btn = re.search(r'<button[^>]*data-t="rest"[^>]*>', html)
        assert rest_btn, f"休みボタンが見つからない:\n{html}"
        assert "btn-primary" in rest_btn.group(0), f"選択中が強調されていない:\n{rest_btn.group(0)}"
        # 選ばれていないものは強調されない
        any_btn = re.search(r'<button[^>]*data-t="any"[^>]*>', html)
        assert "btn-primary" not in any_btn.group(0), f"未選択が強調されている:\n{any_btn.group(0)}"

    def test_time_section_is_marked_when_time_is_selected(self):
        html = _picker("{type:'time',start:'2026-07-15T17:00:00',end:'2026-07-15T22:00:00'}")
        assert "選択中" in html, f"時間指定が選択中であることが示されていない:\n{html}"

    def test_time_section_is_not_marked_when_a_category_is_selected(self):
        html = _picker("{type:'morning'}")
        assert "選択中" not in html, f"種別選択中なのに時間指定が選択中と出ている:\n{html}"

    def test_prefilled_times_are_escaped(self):
        """start/end は保存済みデータ由来。属性から脱出できないこと。"""
        html = _picker("{type:'time',start:'2026-07-15T\" autofocus x=\"',end:'2026-07-15T22:00:00'}")
        assert " autofocus" not in html, f"属性が注入されている:\n{html}"


# ============================================================
# 配線（純関数が実際に描画に使われているか）
# ============================================================
# 上のテストは純関数だけを見るので、drawWish / openWishPicker が呼ばなくなっても
# 緑のままになる。バンドラの無いグローバル JS なので、呼び出し側はソースを見て守る。
class TestCallSitesUseTheHelpers:
    def test_draw_wish_renders_via_wish_mark_text(self):
        body = extract_js_function(APP_JS, "drawWish")
        assert "wishMarkText(w)" in body, "drawWish がセル文字列を自前で組み立てている"
        assert "'時間'" not in body, "「時間」の固定ラベルが drawWish に残っている"

    def test_open_wish_picker_renders_via_wish_picker_body_html(self):
        body = extract_js_function(APP_JS, "openWishPicker")
        assert "wishPickerBodyHtml(wishState[day])" in body, \
            "openWishPicker が現在値を渡していない（開き直しで復元されない）"
        assert 'value="17:00"' not in body, "既定時刻がピッカー内にハードコードされている"
