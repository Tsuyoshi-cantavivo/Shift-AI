"""tests/test_settings_xss.py - shops.settings 経由の保存型XSSに対する回帰テスト。

レビュー指摘（Critical）: 管理コンソールの店舗詳細「設定」タブ
(public/admin.js の renderShopSettingsTab) が shop.settings を JSON.parse() した値を
無エスケープで <input value="..."> に描画していた。数値キー
（default_hourly_wage 等）に文字列（HTMLタグを含む値）を保存させることができれば、
管理者が設定タブを開くだけで任意のJSが実行される（クリック不要の保存型XSS、
かつ「店舗ユーザー」→「運営管理者」という権限境界を跨ぐ権限昇格）。

再レビューで、代理閲覧（admin セッションが店舗スコープの GET に対して
role="shop" に化ける）経由で同じ問題が public/app.js 側の2箇所
（AIシフト作成の条件表示・一般店舗ユーザー向け設定画面）にも実在することが
実証された。管理者が対象店舗を代理閲覧するだけで発火するため、これも同じ
権限境界越え。両ファイルとも修正・回帰テストの対象にしている。

このファイルは2層の防御を検証する。
  層1（描画側）: public/admin.js の num()/esc()、public/app.js の
                 num()/esc()（renderShopTab）と「AIに考慮させる条件」表示の
                 esc() が実際にエスケープすること
                 （Node で実ファイルから関数を抜き出して実行し、生成HTMLを確認）
  層2（入口側）: PUT /api/shop/settings（店舗ユーザー）・
                 PUT /api/admin/shops/<id>/settings（運営管理者）・
                 POST /api/admin/shops（店舗作成時、運営管理者）の3経路すべてが、
                 既知キー（src/utils.py の SETTINGS_KEYS）の値の型を検証し、
                 不正なら 400 で弾き、DBにも保存されないこと
"""
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import db as dbmod
from helpers import (auth, insert_admin, insert_pattern, insert_shop, insert_staff,
                     make_session)

ROOT = Path(__file__).resolve().parents[1]
ADMIN_JS = (ROOT / "public" / "admin.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

XSS_PAYLOAD = '1000"><img src=x id="poc" onerror="window.__xss_fired=1">'


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


# ============================================================
# 層1: 描画側のエスケープ（public/admin.js を実際に Node で実行して確認）
# ============================================================
def _extract_function(source, name):
    """`function name(...) { ... }` を波括弧の対応を数えて丸ごと抜き出す。
    正規表現の手書き複製ではなく実ファイルの関数定義そのものを使うことで、
    admin.js/app.js が変わったときにテストが追随する（乖離した二重実装を避ける）。"""
    m = re.search(rf"function {re.escape(name)}\s*\([^)]*\)\s*{{", source)
    assert m, f"{name} の定義が見つかりません（app.js の構造が変わった？）"
    depth = 0
    i = m.end() - 1  # '{' の位置
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[m.start():i + 1]
        i += 1
    raise AssertionError(f"{name} の終端 '}}' が見つかりません")


def _extract_num_definition():
    m = re.search(r"const num = \(v\) => .*?;", ADMIN_JS)
    assert m, "renderShopSettingsTab の num() 定義が見つかりません（admin.js の構造が変わった？）"
    return m.group(0)


def _extract_cf_wage_template():
    """cfWage の <input> テンプレート断片を admin.js から実際に抜き出す
    （テスト側でテンプレートを手書きすると、本体が変わってもテストが気づけない）。"""
    m = re.search(r'<input type="number" id="cfWage"[^>]*>', ADMIN_JS)
    assert m, "cfWage の <input> テンプレートが見つかりません（admin.js の構造が変わった？）"
    tpl = m.group(0)
    assert "${num(s.default_hourly_wage)}" in tpl, "テンプレートの形が想定と異なります"
    return tpl


def _render_cf_wage_html(payload_value):
    """esc()/num() の実装 + 実際の cfWage テンプレートを Node で評価し、
    生成される <input> のHTML文字列を返す。"""
    esc_src = _extract_function(APP_JS, "esc")
    num_src = _extract_num_definition()
    template = _extract_cf_wage_template()
    script = f"""
{esc_src}
{num_src}
const s = {json.dumps({"default_hourly_wage": payload_value})};
const html = `{template}`;
process.stdout.write(html);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"Node実行に失敗: {result.stderr}"
    return result.stdout


class TestSettingsTabRenderEscaping:
    def test_malicious_string_value_is_escaped_in_rendered_html(self):
        """レビュアーが実測したペイロードそのもの。<img> がタグとして注入されないこと。"""
        html = _render_cf_wage_html(XSS_PAYLOAD)
        assert "<img" not in html, f"エスケープされずタグが注入されている:\n{html}"
        assert "&lt;img" in html, f"esc() が効いていない:\n{html}"
        # payload内の " もエスケープされ、属性から脱出できないこと
        assert html.count('value="') == 1, f"属性の境界が壊れている:\n{html}"

    def test_normal_numeric_value_renders_unchanged(self):
        """既存の正常系（数値）が引き続き素直に描画されること。"""
        html = _render_cf_wage_html(1000)
        assert 'value="1000"' in html

    def test_empty_value_renders_as_empty_attribute(self):
        html = _render_cf_wage_html(None)
        assert 'value=""' in html


# ---- 再レビュー指摘: public/app.js 側の2箇所（代理閲覧経由で権限境界を跨ぐ） ----
# app.js:1939 相当（AIシフト作成画面の「AIに考慮させる条件」表示・テキストコンテキスト）
# app.js:4165 相当（renderShopTab、一般店舗ユーザー向け設定画面・属性値コンテキスト）
# いずれも代理閲覧中に管理者のブラウザで別テナントの settings を描画するため、
# 「新規保存はサーバ側で型を締めた」だけでは防げない（既存の汚染データが残っている限り）。
def _extract_gen_condition_night_rate_template():
    m = re.search(
        r'<div class="gen-condition"><span class="gen-condition-label">深夜割増率</span>.*?</div>',
        APP_JS)
    assert m, "AIシフト作成の深夜割増率テンプレートが見つかりません（app.js の構造が変わった？）"
    tpl = m.group(0)
    assert "s.night_premium_rate" in tpl, "テンプレートの形が想定と異なります"
    return tpl


def _render_gen_condition_night_rate(payload_value):
    esc_src = _extract_function(APP_JS, "esc")
    template = _extract_gen_condition_night_rate_template()
    script = f"""
{esc_src}
const s = {json.dumps({"night_premium_rate": payload_value})};
const html = `{template}`;
process.stdout.write(html);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"Node実行に失敗: {result.stderr}"
    return result.stdout


def _extract_shop_tab_num_definition():
    m = re.search(r"const num = \(v, dflt\) => .*?;", APP_JS)
    assert m, "renderShopTab の num() 定義が見つかりません（app.js の構造が変わった？）"
    return m.group(0)


def _extract_setnightrate_template():
    m = re.search(r'<input id="setNightRate"[^>]*>', APP_JS)
    assert m, "setNightRate の <input> テンプレートが見つかりません（app.js の構造が変わった？）"
    tpl = m.group(0)
    assert "${num(s.night_premium_rate, 1.25)}" in tpl, "テンプレートの形が想定と異なります"
    return tpl


def _render_setnightrate_html(payload_value):
    esc_src = _extract_function(APP_JS, "esc")
    num_src = _extract_shop_tab_num_definition()
    template = _extract_setnightrate_template()
    script = f"""
{esc_src}
{num_src}
const s = {json.dumps({"night_premium_rate": payload_value})};
const html = `{template}`;
process.stdout.write(html);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"Node実行に失敗: {result.stderr}"
    return result.stdout


class TestGenConditionsRenderEscaping:
    """public/app.js の AIシフト作成画面「AIに考慮させる条件」表示（テキストコンテキスト）。
    代理閲覧中に管理者のブラウザで顧客settingsを描画する箇所の1つ。"""

    def test_malicious_string_value_is_escaped_in_rendered_html(self):
        html = _render_gen_condition_night_rate(XSS_PAYLOAD)
        assert "<img" not in html, f"エスケープされずタグが注入されている:\n{html}"
        assert "&lt;img" in html, f"esc() が効いていない:\n{html}"

    def test_normal_numeric_value_renders_unchanged(self):
        html = _render_gen_condition_night_rate(1.25)
        assert "1.25倍" in html


class TestShopSettingsScreenRenderEscaping:
    """public/app.js の renderShopTab（一般店舗ユーザー向け設定画面、属性値コンテキスト）。
    代理閲覧中に管理者のブラウザで顧客settingsを描画するもう1つの箇所。"""

    def test_malicious_string_value_is_escaped_in_rendered_html(self):
        html = _render_setnightrate_html(XSS_PAYLOAD)
        assert "<img" not in html, f"エスケープされずタグが注入されている:\n{html}"
        assert "&lt;img" in html, f"esc() が効いていない:\n{html}"
        assert html.count('value="') == 1, f"属性の境界が壊れている:\n{html}"

    def test_normal_numeric_value_renders_unchanged(self):
        html = _render_setnightrate_html(1.25)
        assert 'value="1.25"' in html


# ============================================================
# 層2: 入口側の型検証（管理者向け PUT /api/admin/shops/<id>/settings）
# ============================================================
class TestAdminSettingsValueValidation:
    def test_xss_payload_in_numeric_key_is_rejected(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP1", name="店1")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"default_hourly_wage": XSS_PAYLOAD})
        assert r.status_code == 400
        row = dbmod.query_one("SELECT settings FROM shops WHERE id=?", (sid,))
        s = json.loads(row["settings"] or "{}")
        assert "default_hourly_wage" not in s, "拒否されたはずの値がDBに残っている"

    def test_all_numeric_keys_reject_string_values(self, client):
        """6つの数値キーすべてが同じ穴を持つ、というレビュー指摘を1本ずつ固定する。"""
        numeric_keys = [
            "default_hourly_wage", "max_daily_hours", "min_daily_hours",
            "max_employee_daily_hours", "max_consecutive_days",
            "transport_per_day", "night_premium_rate",
        ]
        t = _admin_token(client)
        for key in numeric_keys:
            sid = insert_shop(f"SHOP_{key}", name="店")
            r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                            json={key: "1<img src=x>"})
            assert r.status_code == 400, f"{key} が文字列を受理してしまう"

    def test_bool_is_rejected_for_numeric_key(self, client):
        """bool は Python では int のサブクラスなので、明示的に弾く必要がある。"""
        t = _admin_token(client)
        sid = insert_shop("SHOP_BOOL", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"default_hourly_wage": True})
        assert r.status_code == 400

    def test_invalid_period_mode_is_rejected(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP_PM", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"period_mode": "<script>alert(1)</script>"})
        assert r.status_code == 400

    def test_valid_period_mode_is_accepted(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP_PM2", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"period_mode": "month"})
        assert r.status_code == 200

    def test_invalid_business_hours_format_is_rejected(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP_BH", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"business_hours": '09:00-22:00"><img src=x>'})
        assert r.status_code == 400

    def test_valid_business_hours_is_accepted(self, client):
        """ローカルDBの実店舗（MS_LakeTown）に実在する形式であることを確認済み。"""
        t = _admin_token(client)
        sid = insert_shop("SHOP_BH2", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"business_hours": "06:00-05:00"})
        assert r.status_code == 200

    def test_non_dict_shift_hours_is_rejected(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP_SH", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"shift_hours": "<img src=x onerror=alert(1)>"})
        assert r.status_code == 400

    def test_dict_shift_hours_is_accepted(self, client):
        t = _admin_token(client)
        sid = insert_shop("SHOP_SH2", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"shift_hours": {"bulk_mode": True}})
        assert r.status_code == 200

    def test_normal_numeric_save_still_works(self, client):
        """既存の正常系（tests/test_admin_shop_lifecycle.py と重複気味だが、
        本ファイル単体でも回帰を検出できるように明示的に確認する）。"""
        t = _admin_token(client)
        sid = insert_shop("SHOP_OK", name="店")
        r = client.put(f"/api/admin/shops/{sid}/settings", headers=auth(t),
                        json={"default_hourly_wage": 1200, "max_daily_hours": 8})
        assert r.status_code == 200
        s = json.loads(dbmod.query_one("SELECT settings FROM shops WHERE id=?", (sid,))["settings"])
        assert s["default_hourly_wage"] == 1200
        assert s["max_daily_hours"] == 8


# ============================================================
# 再レビュー指摘 Minor1: POST /api/admin/shops が層2の入口から漏れていた
# （src/admin_api.py の admin_create_shop が settings をキーも値も検証せず保存していた）
# ============================================================
class TestAdminCreateShopSettingsValidation:
    def _payload(self, shop_code, mgr_code, settings):
        return {
            "shop_code": shop_code, "shop_name": "テスト店舗",
            "password": "ShopPass1",
            "manager_code": mgr_code, "manager_password": "Mgr12345",
            "manager_name": "テスト管理者",
            "settings": settings,
        }

    def test_xss_payload_in_settings_at_creation_is_rejected(self, client):
        t = _admin_token(client)
        r = client.post("/api/admin/shops", headers=auth(t),
                        json=self._payload("XSSSHOP", "mgrxss", {"default_hourly_wage": XSS_PAYLOAD}))
        assert r.status_code == 400
        # ロールバックではなく、店舗作成の前段で弾かれ、行自体が作られていないこと
        assert dbmod.query_one("SELECT id FROM shops WHERE shop_code=?", ("XSSSHOP",)) is None

    def test_bool_in_settings_at_creation_is_rejected(self, client):
        t = _admin_token(client)
        r = client.post("/api/admin/shops", headers=auth(t),
                        json=self._payload("BOOLSHOP", "mgrbool", {"default_hourly_wage": True}))
        assert r.status_code == 400

    def test_valid_settings_at_creation_still_works(self, client):
        t = _admin_token(client)
        r = client.post("/api/admin/shops", headers=auth(t),
                        json=self._payload("VALIDSHOP", "mgrvalid",
                                            {"default_hourly_wage": 1500, "period_mode": "month"}))
        assert r.status_code == 200
        sid = r.get_json()["id"]
        s = json.loads(dbmod.query_one("SELECT settings FROM shops WHERE id=?", (sid,))["settings"])
        assert s["default_hourly_wage"] == 1500
        assert s["period_mode"] == "month"

    def test_unknown_key_at_creation_still_accepted_backward_compat(self, client):
        """既存契約: tests/test_admin_staff_apis.py::test_admin_create_and_update_shop が
        settings={"x": 1}（未知キー）での作成を200で期待している
        （admin_shop_update_settings とは異なり、作成時は未知キーを拒否しない設計）。
        この既存契約を壊していないことを本ファイル内でも明示的に固定する。"""
        t = _admin_token(client)
        r = client.post("/api/admin/shops", headers=auth(t),
                        json=self._payload("OKSHOP", "mgrok", {"x": 1}))
        assert r.status_code == 200

    def test_no_settings_key_still_works(self, client):
        """settings 自体を送らないケース（多くの実利用）が壊れていないこと。"""
        t = _admin_token(client)
        payload = self._payload("NOSETSHOP", "mgrnoset", {})
        del payload["settings"]
        r = client.post("/api/admin/shops", headers=auth(t), json=payload)
        assert r.status_code == 200


# ============================================================
# 層2: 入口側の型検証（店舗ユーザー向け PUT /api/shop/settings）
# ============================================================
class TestShopSettingsValueValidation:
    def test_xss_payload_in_numeric_key_is_rejected(self, client):
        shop_id = insert_shop(code="S1")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/settings", json={"settings": {"default_hourly_wage": XSS_PAYLOAD}},
                        headers=auth(tok))
        assert r.status_code == 400
        row = dbmod.query_one("SELECT settings FROM shops WHERE id=?", (shop_id,))
        s = json.loads(row["settings"] or "{}")
        assert "default_hourly_wage" not in s, "拒否されたはずの値がDBに残っている"

    def test_non_dict_settings_patch_is_rejected_not_500(self, client):
        """settings に dict 以外（配列等）が来ても、生のPython例外が漏れる500では
        なく400として拒否されること（admin側 admin_shop_update_settings と同じ慣習）。

        【teeth注意】status_code==400 だけでは弱い: dict.update(list) 自体が
        Python組み込みの ValueError（"dictionary update sequence element #0
        has length 3; 2 is required" 等）を出し、@app.errorhandler(ValueError) が
        それを400に変換するため、validate_known_settings_values() の
        isinstance(patch, dict) チェックを外しても本テストは緑のままだった
        （レビュー指摘）。エラーメッセージまで検証し、実際に
        validate_known_settings_values() 経由であることを固定する。
        """
        shop_id = insert_shop(code="S1")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/settings", json={"settings": ["not", "a", "dict"]},
                        headers=auth(tok))
        assert r.status_code == 400
        assert r.get_json()["error"] == "settings はオブジェクトで指定してください", (
            "validate_known_settings_values() の isinstance チェックではなく、"
            "dict.update(list) の組み込みエラーで400になっている可能性がある: "
            + str(r.get_json()))

    def test_night_premium_rate_string_is_rejected(self, client):
        """public/app.js の renderShopTab（一般店舗ユーザー設定画面）が
        `${s.night_premium_rate ?? 1.25}` を無エスケープで描画しており、
        こちらも同じ入口で防ぐ必要がある。"""
        shop_id = insert_shop(code="S1")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/settings",
                        json={"settings": {"night_premium_rate": '1.25"><img src=x>'}},
                        headers=auth(tok))
        assert r.status_code == 400

    # ---- 既存の正常系（後方互換）が壊れていないことの確認 ----
    def test_normal_ui_payload_still_works(self, client):
        """public/app.js renderShopTab の #saveSettings が実際に送る形をそのまま送る。"""
        shop_id = insert_shop(code="S1", name="旧店舗名")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/settings", json={
            "shop_name": "新店舗名",
            "settings": {
                "default_hourly_wage": 1100, "min_daily_hours": 4, "max_daily_hours": 9,
                "max_consecutive_days": 6, "night_premium_rate": 1.25,
                "transport_per_day": 300, "period_mode": "half",
            },
        }, headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT shop_name, settings FROM shops WHERE id=?", (shop_id,))
        assert row["shop_name"] == "新店舗名"
        s = json.loads(row["settings"])
        assert s["default_hourly_wage"] == 1100
        assert s["period_mode"] == "half"

    def test_unknown_key_still_accepted_backward_compat(self, client):
        """PUT /api/shop/settings は既知キー以外の任意キーを許容する既存契約がある
        （tests/test_admin_staff_apis.py::test_update_shop_name と同じ前提）。
        本タスクの型検証は既知キーだけを対象にしており、この契約を壊していないことを
        本ファイル内でも明示的に固定する。"""
        shop_id = insert_shop(code="S1")
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/settings", json={"settings": {"custom_future_key": "plain text"}},
                        headers=auth(tok))
        assert r.status_code == 200
        row = dbmod.query_one("SELECT settings FROM shops WHERE id=?", (shop_id,))
        s = json.loads(row["settings"])
        assert s["custom_future_key"] == "plain text"

    def test_role_mass_assignment_still_returns_200_and_has_no_effect(self, client):
        """tests/test_security.py::test_shop_cannot_self_promote_to_admin と同じ前提
        （role は未知キーなので拒否対象ではなく、素通しされても権限には影響しない）。
        層2の変更でこの既存契約を壊していないことをここでも固定する。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.put("/api/shop/settings", json={"settings": {"role": "admin"}}, headers=auth(tok))
        assert r.status_code == 200
        r2 = client.get("/api/me", headers=auth(tok))
        assert r2.get_json()["role"] == "shop"


# ============================================================
# C-1 層1: 描画側のエスケープ（実ファイルのテンプレートを Node で評価する）
# ============================================================
def _render(template, context, extra_src=""):
    """public/*.js から抜き出した実テンプレートを Node で評価し、生成HTMLを返す。

    テンプレートをテスト側で手書きすると本体が変わっても気づけないため、
    必ず実ファイルから正規表現で抜き出したものを渡す。
    """
    esc_src = _extract_function(APP_JS, "esc")
    decls = "\n".join(f"const {k} = {json.dumps(v, ensure_ascii=False)};"
                       for k, v in context.items())
    script = f"""
{esc_src}
{extra_src}
{decls}
const html = `{template}`;
process.stdout.write(html);
"""
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"Node実行に失敗: {result.stderr}"
    return result.stdout


def _grab(source, pattern, what):
    m = re.search(pattern, source)
    assert m, f"{what} のテンプレートが見つかりません（JSの構造が変わった？）"
    return m.group(0)


class _TagAttrCollector(HTMLParser):
    """生成HTMLを実際にHTMLとしてパースし、タグごとの属性名を集める。

    レビュアーはヘッドレスChromiumで「value="" を抜けて onfocus / autofocus が
    独立属性としてパースされ、ハンドラが実行された」ことを実測している。
    単純な文字列検索では、エスケープ済みの `&quot; onfocus=&quot;` と
    属性として生えた onfocus= を区別できない。パーサに判定させる。
    """

    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, [a[0] for a in attrs]))

    handle_startendtag = handle_starttag


def _assert_attribute_is_not_escapable(html):
    """ATTR_ESCAPE_PAYLOAD が value 属性の外へ出ていないこと。

    ペイロード `" onfocus="alert(1)" autofocus x="` を流し込んだ結果をHTMLとして
    パースし、onfocus / autofocus / x が属性として生えていないことを確認する。
    """
    p = _TagAttrCollector()
    p.feed(html)
    names = set()
    for _tag, attr_names in p.tags:
        names.update(attr_names)
    injected = names & {"onfocus", "autofocus", "x"}
    assert not injected, f"属性として注入されている {sorted(injected)}:\n{html}\n{p.tags}"
    assert len(p.tags) == 1, f"タグが増えている（値がタグを閉じている）:\n{html}\n{p.tags}"


class TestStaffNumericRenderEscaping:
    """レビュアーが実測した経路: 管理者が店舗詳細→スタッフ→編集を押すだけで発火。
    代理閲覧すら不要な、店舗ユーザー→運営管理者の権限昇格。"""

    def test_admin_staff_edit_wage_input_is_escaped(self):
        tpl = _grab(ADMIN_JS, r'<input id="aeWage"[^>]*>', "admin.js スタッフ編集の時給")
        html = _render(tpl, {"s": {"hourly_wage": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)

    def test_admin_staff_edit_wage_normal_value_unchanged(self):
        tpl = _grab(ADMIN_JS, r'<input id="aeWage"[^>]*>', "admin.js スタッフ編集の時給")
        assert 'value="1200"' in _render(tpl, {"s": {"hourly_wage": 1200}})

    def test_shop_staff_form_wage_input_is_escaped(self):
        tpl = _grab(APP_JS, r'<input id="f_wage"[^>]*>', "app.js スタッフフォームの時給")
        html = _render(tpl, {"s": {"hourly_wage": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)

    def test_shop_staff_form_min_hours_input_is_escaped(self):
        tpl = _grab(APP_JS, r'<input id="f_min"[^>]*>', "app.js スタッフフォームの最低h")
        html = _render(tpl, {"s": {"min_hours_per_month": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)

    def test_shop_staff_list_text_context_is_escaped(self):
        """一覧のテキストコンテキスト（時給◯円 ・ 月◯-◯h）。"""
        tpl = _grab(APP_JS, r'<div class="small text-secondary">\$\{roleLabel\(s\.role\)\}[^<]*</div>',
                    "app.js スタッフ一覧の時給表示")
        html = _render(tpl, {"s": {"role": "part_time",
                                   "hourly_wage": '1000<img src=x onerror=alert(1)>',
                                   "min_hours_per_month": 0, "max_hours_per_month": 160}},
                       extra_src=_extract_function(APP_JS, "roleLabel"))
        assert "<img" not in html, f"タグが注入されている:\n{html}"
        assert "&lt;img" in html


class TestPatternRequiredStaffRenderEscaping:
    def test_matrix_default_input_is_escaped(self):
        tpl = _grab(APP_JS, r'<input type="number" class="matrix-input matrix-default"[^>]*>',
                    "app.js 必要人数マトリクスの基本人数")
        html = _render(tpl, {"p": {"id": 1, "required_staff": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)

    def test_matrix_default_normal_value_unchanged(self):
        tpl = _grab(APP_JS, r'<input type="number" class="matrix-input matrix-default"[^>]*>',
                    "app.js 必要人数マトリクスの基本人数")
        assert 'value="3"' in _render(tpl, {"p": {"id": 1, "required_staff": 3}})

    def test_pattern_edit_modal_inputs_are_escaped(self):
        """dataset 経由で pattern_name / start_time が生のまま戻ってくる経路。"""
        tpl = _grab(APP_JS, r'<input id="pName"[^>]*>', "app.js 時間帯編集の名称")
        html = _render(tpl, {"data": {"n": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)

    def test_pattern_edit_modal_req_is_escaped(self):
        tpl = _grab(APP_JS, r'<input id="pReq"[^>]*>', "app.js 時間帯編集の必要人数")
        html = _render(tpl, {"data": {"req": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)


class TestPeriodDateRenderEscaping:
    def test_generate_tab_start_date_is_escaped(self):
        tpl = _grab(APP_JS, r'<input type="date"  id="genStart"[^>]*>', "app.js AI作成の開始日")
        html = _render(tpl, {"p": {"start_date": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)

    def test_shifts_tab_start_date_is_escaped(self):
        tpl = _grab(APP_JS, r'<input type="date" id="sStart"[^>]*>', "app.js シフト管理の開始日")
        html = _render(tpl, {"p": {"start_date": ATTR_ESCAPE_PAYLOAD}})
        _assert_attribute_is_not_escapable(html)

    def test_shifts_tab_start_date_normal_value_unchanged(self):
        tpl = _grab(APP_JS, r'<input type="date" id="sStart"[^>]*>', "app.js シフト管理の開始日")
        assert 'value="2026-08-01"' in _render(tpl, {"p": {"start_date": "2026-08-01"}})


class TestFixedShiftRenderEscaping:
    def test_fixed_shift_edit_start_time_is_escaped(self):
        tpl = _grab(APP_JS, r'<input id="eSt"[^>]*>', "app.js 固定シフト編集の開始時刻")
        html = _render(tpl, {"b": {"dataset": {"st": ATTR_ESCAPE_PAYLOAD}}})
        _assert_attribute_is_not_escapable(html)


# ============================================================
# C-1: shops.settings と同型の穴が、別フィールドに残っていた
# ============================================================
# Task 11 は shops.settings について「入口で型検証＋描画で esc()」の2層で塞いだが、
# 同じクラスの穴が staffs.hourly_wage / min_hours_per_month / max_hours_per_month、
# shift_patterns.required_staff、shift_request_periods の日付に残っていた。
# SQLite の INTEGER affinity は非数値 TEXT をそのまま保持するため、
# `hourly_wage='" onfocus="alert(1)" autofocus x="'` が 200 で保存でき、
# 管理者が店舗詳細→スタッフ→編集を押すだけで value="" を抜けて
# onfocus / autofocus が独立属性としてパースされ、ハンドラが実行された
# （代理閲覧すら不要な、店舗ユーザー→運営管理者の権限昇格）。
ATTR_ESCAPE_PAYLOAD = '" onfocus="alert(1)" autofocus x="'


def _shop_token(shop_id):
    return make_session("shop", shop_id, shop_id)


class TestStaffNumericFieldValidation:
    """POST/PUT /api/shop/staffs（店舗管理者が日常的に使う経路）。"""

    NUMERIC_KEYS = ("hourly_wage", "min_hours_per_month", "max_hours_per_month")

    def _create(self, client, tok, **extra):
        payload = {"staff_code": "X1", "name": "太郎", "password": "Password1"}
        payload.update(extra)
        return client.post("/api/shop/staffs", json=payload, headers=auth(tok))

    def test_attr_escape_payload_in_hourly_wage_is_rejected_on_create(self, client):
        """レビュアーが実測したペイロードそのもの。"""
        shop_id = insert_shop(code="S1")
        r = self._create(client, _shop_token(shop_id), hourly_wage=ATTR_ESCAPE_PAYLOAD)
        assert r.status_code == 400, r.get_data(as_text=True)
        assert dbmod.query_one("SELECT id FROM staffs WHERE staff_code='X1'") is None, \
            "拒否されたはずの行が作られている"

    def test_all_numeric_keys_reject_strings_on_create(self, client):
        shop_id = insert_shop(code="S1")
        tok = _shop_token(shop_id)
        for i, key in enumerate(self.NUMERIC_KEYS):
            r = self._create(client, tok, staff_code=f"C{i}",
                             **{key: '1<img src=x onerror=alert(1)>'})
            assert r.status_code == 400, f"{key} が文字列を受理してしまう"

    def test_all_numeric_keys_reject_strings_on_update(self, client):
        shop_id = insert_shop(code="S1")
        tok = _shop_token(shop_id)
        sid = insert_staff(shop_id, "U1", "太郎")
        for key in self.NUMERIC_KEYS:
            body = {"name": "太郎", "hourly_wage": 1000, "min_hours_per_month": 0,
                    "max_hours_per_month": 160}
            body[key] = ATTR_ESCAPE_PAYLOAD
            r = client.put(f"/api/shop/staffs/{sid}", json=body, headers=auth(tok))
            assert r.status_code == 400, f"{key} が文字列を受理してしまう（更新）"
            row = dbmod.query_one("SELECT * FROM staffs WHERE id=?", (sid,))
            assert row[key] != ATTR_ESCAPE_PAYLOAD, f"{key} に文字列が保存されている"

    def test_bool_is_rejected(self, client):
        """bool は Python では int のサブクラス。settings 側と同じく明示的に弾く。"""
        shop_id = insert_shop(code="S1")
        r = self._create(client, _shop_token(shop_id), hourly_wage=True)
        assert r.status_code == 400

    def test_admin_staff_create_also_validates(self, client):
        """運営管理者の POST /api/admin/shops/<id>/staffs も同じ入口で塞ぐこと。"""
        t = _admin_token(client)
        sid = insert_shop("ADM1", name="店")
        r = client.post(f"/api/admin/shops/{sid}/staffs", headers=auth(t),
                        json={"staff_code": "a1", "name": "太郎", "password": "Password1",
                              "hourly_wage": ATTR_ESCAPE_PAYLOAD})
        assert r.status_code == 400
        assert dbmod.query_one("SELECT id FROM staffs WHERE staff_code='a1'") is None

    # ---- 既存の正常系（public/app.js が実際に送る形）が壊れていないこと ----
    def test_normal_ui_payload_still_works_on_create(self, client):
        """public/app.js showStaffForm が送る形（+g('#f_wage') は JS の number）。"""
        shop_id = insert_shop(code="S1")
        r = self._create(client, _shop_token(shop_id), role="part_time",
                         hourly_wage=1100, min_hours_per_month=0, max_hours_per_month=160)
        assert r.status_code == 200, r.get_data(as_text=True)
        row = dbmod.query_one("SELECT * FROM staffs WHERE staff_code='X1'")
        assert row["hourly_wage"] == 1100 and row["max_hours_per_month"] == 160

    def test_normal_ui_payload_still_works_on_update(self, client):
        shop_id = insert_shop(code="S1")
        tok = _shop_token(shop_id)
        sid = insert_staff(shop_id, "U1", "太郎")
        r = client.put(f"/api/shop/staffs/{sid}", headers=auth(tok), json={
            "name": "太郎", "hourly_wage": 1250, "min_hours_per_month": 20,
            "max_hours_per_month": 120, "is_resigned": False})
        assert r.status_code == 200, r.get_data(as_text=True)
        row = dbmod.query_one("SELECT * FROM staffs WHERE id=?", (sid,))
        assert row["hourly_wage"] == 1250 and row["min_hours_per_month"] == 20

    def test_omitted_numeric_fields_still_fall_back_to_defaults(self, client):
        """未指定のときの既存の既定値フォールバックを壊していないこと。"""
        shop_id = insert_shop(code="S1", settings={"default_hourly_wage": 1300})
        r = self._create(client, _shop_token(shop_id))
        assert r.status_code == 200, r.get_data(as_text=True)
        row = dbmod.query_one("SELECT * FROM staffs WHERE staff_code='X1'")
        assert row["hourly_wage"] == 1300
        assert row["min_hours_per_month"] == 0
        assert row["max_hours_per_month"] == 160

    def test_numeric_string_from_a_form_is_accepted(self, client):
        """<input type="number"> の値を文字列のまま送るクライアントも壊さない。
        「数値として読める文字列」は数値に変換して受け入れる（描画の安全は保たれる）。"""
        shop_id = insert_shop(code="S1")
        r = self._create(client, _shop_token(shop_id), hourly_wage="1150")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert dbmod.query_one("SELECT hourly_wage FROM staffs WHERE staff_code='X1'"
                               )["hourly_wage"] == 1150


class TestPatternRequiredStaffValidation:
    def test_required_staff_string_is_rejected_on_create(self, client):
        shop_id = insert_shop(code="S1")
        r = client.post("/api/shop/patterns", headers=auth(_shop_token(shop_id)),
                        json={"pattern_name": "早番", "start_time": "09:00",
                              "end_time": "17:00", "required_staff": ATTR_ESCAPE_PAYLOAD})
        assert r.status_code == 400
        assert dbmod.query_one("SELECT id FROM shift_patterns WHERE shop_id=?",
                               (shop_id,)) is None

    def test_required_staff_string_is_rejected_on_update(self, client):
        shop_id = insert_shop(code="S1")
        pid = insert_pattern(shop_id, "早番", "09:00", "17:00", 2)
        r = client.put(f"/api/shop/patterns/{pid}", headers=auth(_shop_token(shop_id)),
                       json={"pattern_name": "早番", "start_time": "09:00",
                             "end_time": "17:00", "required_staff": ATTR_ESCAPE_PAYLOAD})
        assert r.status_code == 400
        assert dbmod.query_one("SELECT required_staff FROM shift_patterns WHERE id=?",
                               (pid,))["required_staff"] == 2

    def test_normal_pattern_payload_still_works(self, client):
        """public/app.js openPatternModal が送る形（required_staff は number）。"""
        shop_id = insert_shop(code="S1")
        r = client.post("/api/shop/patterns", headers=auth(_shop_token(shop_id)),
                        json={"pattern_name": "夜", "start_time": "17:00",
                              "end_time": "22:00", "required_staff": 2})
        assert r.status_code == 200, r.get_data(as_text=True)
        assert dbmod.query_one("SELECT required_staff FROM shift_patterns WHERE shop_id=?",
                               (shop_id,))["required_staff"] == 2

    def test_omitted_required_staff_defaults_to_one(self, client):
        shop_id = insert_shop(code="S1")
        r = client.post("/api/shop/patterns", headers=auth(_shop_token(shop_id)),
                        json={"pattern_name": "夜", "start_time": "17:00", "end_time": "22:00"})
        assert r.status_code == 200
        assert dbmod.query_one("SELECT required_staff FROM shift_patterns WHERE shop_id=?",
                               (shop_id,))["required_staff"] == 1


class TestPeriodDateValidation:
    def test_non_date_start_date_is_rejected(self, client):
        shop_id = insert_shop(code="S1")
        r = client.post("/api/shop/periods", headers=auth(_shop_token(shop_id)),
                        json={"start_date": '2026-08-01" autofocus onfocus="alert(1)',
                              "end_date": "2026-08-31", "deadline": "2026-07-25"})
        assert r.status_code == 400
        assert dbmod.query_one("SELECT id FROM shift_request_periods WHERE shop_id=?",
                               (shop_id,)) is None

    def test_all_date_fields_are_validated(self, client):
        shop_id = insert_shop(code="S1")
        tok = _shop_token(shop_id)
        base = {"start_date": "2026-08-01", "end_date": "2026-08-31",
                "deadline": "2026-07-25"}
        for key in ("start_date", "end_date", "deadline"):
            body = dict(base)
            body[key] = "<img src=x onerror=alert(1)>"
            r = client.post("/api/shop/periods", headers=auth(tok), json=body)
            assert r.status_code == 400, f"{key} が非日付を受理してしまう"

    def test_impossible_date_is_rejected(self, client):
        """形だけ YYYY-MM-DD でも実在しない日付は弾く（2026-02-30 等）。"""
        shop_id = insert_shop(code="S1")
        r = client.post("/api/shop/periods", headers=auth(_shop_token(shop_id)),
                        json={"start_date": "2026-02-30", "end_date": "2026-08-31",
                              "deadline": "2026-07-25"})
        assert r.status_code == 400

    def test_deadline_on_update_is_validated(self, client):
        shop_id = insert_shop(code="S1")
        pid = dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline) "
            "VALUES (?,?,?,?)", (shop_id, "2026-08-01", "2026-08-31", "2026-07-25")
        )["last_row_id"]
        r = client.put(f"/api/shop/periods/{pid}", headers=auth(_shop_token(shop_id)),
                       json={"is_active": True, "deadline": '2026-07-25"><img src=x>'})
        assert r.status_code == 400
        assert dbmod.query_one("SELECT deadline FROM shift_request_periods WHERE id=?",
                               (pid,))["deadline"] == "2026-07-25"

    def test_normal_period_payload_still_works(self, client):
        """public/app.js の #addPer が送る形（<input type="date"> の値）。"""
        shop_id = insert_shop(code="S1")
        r = client.post("/api/shop/periods", headers=auth(_shop_token(shop_id)),
                        json={"start_date": "2026-08-01", "end_date": "2026-08-31",
                              "deadline": "2026-07-25"})
        assert r.status_code == 200, r.get_data(as_text=True)
        row = dbmod.query_one("SELECT * FROM shift_request_periods WHERE shop_id=?", (shop_id,))
        assert row["start_date"] == "2026-08-01" and row["deadline"] == "2026-07-25"

    def test_period_update_without_deadline_still_works(self, client):
        """締切を送らずに有効/終了だけ切り替える既存操作（#addPer 以外の導線）。"""
        shop_id = insert_shop(code="S1")
        pid = dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline) "
            "VALUES (?,?,?,?)", (shop_id, "2026-08-01", "2026-08-31", "2026-07-25")
        )["last_row_id"]
        r = client.put(f"/api/shop/periods/{pid}", headers=auth(_shop_token(shop_id)),
                       json={"is_active": False})
        assert r.status_code == 200, r.get_data(as_text=True)
