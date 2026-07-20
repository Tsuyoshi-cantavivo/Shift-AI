"""tests/test_japanese_holidays.py - 日本の祝日計算モジュールのテスト。"""
import pytest

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import holidays_jp


class TestJapaneseHolidays:
    """日本の祝日計算の正確性テスト。"""

    def test_2026_new_year_day(self):
        """2026/1/1 は元日。"""
        days = holidays_jp.japanese_holidays(2026)
        names = {h["date"]: h["name"] for h in days}
        assert names["2026-01-01"] == "元日"

    def test_2026_coming_of_age_day(self):
        """2026/1/12 は成人の日（1月第2月曜）。"""
        days = holidays_jp.japanese_holidays(2026)
        names = {h["date"]: h["name"] for h in days}
        assert names["2026-01-12"] == "成人の日"

    def test_2026_emperors_birthday(self):
        """2026/2/23 は天皇誕生日。"""
        days = holidays_jp.japanese_holidays(2026)
        names = {h["date"]: h["name"] for h in days}
        assert names["2026-02-23"] == "天皇誕生日"

    def test_2026_spring_equinox(self):
        """2026年の春分の日は3月20日。"""
        days = holidays_jp.japanese_holidays(2026)
        names = {h["date"]: h["name"] for h in days}
        assert names["2026-03-20"] == "春分の日"

    def test_2026_furikae_holiday(self):
        """2026/5/3（日）→ 5/6（火）が憲法記念日の振替休日。"""
        days = holidays_jp.japanese_holidays(2026)
        names = {h["date"]: h["name"] for h in days}
        # 5月3日が日曜 → 5月4日(月・みどりの日)、5月5日(火・こどもの日)、5月6日(水・振替)
        assert "2026-05-06" in names
        assert "振替休日" in names["2026-05-06"]

    def test_2026_kokumin_no_kyujitsu(self):
        """2026/9/22 は国民の休日（敬老の日9/21と秋分の日9/23に挟まれた日）。"""
        days = holidays_jp.japanese_holidays(2026)
        names = {h["date"]: h["name"] for h in days}
        assert names["2026-09-21"] == "敬老の日"
        assert names["2026-09-22"] == "国民の休日"
        assert names["2026-09-23"] == "秋分の日"

    def test_2026_autumn_equinox(self):
        """2026年の秋分の日は9月23日。"""
        days = holidays_jp.japanese_holidays(2026)
        names = {h["date"]: h["name"] for h in days}
        assert names["2026-09-23"] == "秋分の日"

    def test_2026_total_count(self):
        """2026年の祝日数は妥当な範囲（15〜22日）。"""
        days = holidays_jp.japanese_holidays(2026)
        assert 15 <= len(days) <= 22

    def test_2027_basic(self):
        """2027年も主要祝日が取得できる。"""
        days = holidays_jp.japanese_holidays(2027)
        names = {h["date"]: h["name"] for h in days}
        assert names["2027-01-01"] == "元日"
        # 2027年の成人の日は1月11日（第2月曜）
        assert names["2027-01-11"] == "成人の日"

    def test_no_duplicate_dates(self):
        """同一年の祝日リストに重複日付がないこと。"""
        for year in (2024, 2025, 2026, 2027, 2028, 2030):
            days = holidays_jp.japanese_holidays(year)
            dates = [h["date"] for h in days]
            assert len(dates) == len(set(dates)), f"{year}年に重複あり: {dates}"

    def test_dates_are_sorted(self):
        """祝日リストは日付順にソートされている。"""
        for year in (2026, 2027, 2028):
            days = holidays_jp.japanese_holidays(year)
            dates = [h["date"] for h in days]
            assert dates == sorted(dates), f"{year}年の日付がソートされていません"


class TestHolidaysInRange:
    """期間指定の祝日取得テスト。"""

    def test_one_year_range(self):
        """1年間の範囲指定で祝日が取れる。"""
        days = holidays_jp.japanese_holidays_in_range("2026-01-01", "2026-12-31")
        assert len(days) >= 15

    def test_short_range(self):
        """短い範囲（1月のみ）は該当祝日のみ。"""
        days = holidays_jp.japanese_holidays_in_range("2026-01-01", "2026-01-31")
        names = {h["date"]: h["name"] for h in days}
        assert "2026-01-01" in names  # 元日
        assert "2026-01-12" in names  # 成人の日
        # 2月の祝日は含まれない
        assert all("2026-02" not in h["date"] for h in days)

    def test_invalid_date_returns_empty(self):
        """不正な日付フォーマットは空リスト。"""
        days = holidays_jp.japanese_holidays_in_range("invalid", "2026-12-31")
        assert days == []

    def test_multi_year_range(self):
        """複数年またぎの範囲指定。"""
        days = holidays_jp.japanese_holidays_in_range("2025-12-01", "2027-02-28")
        # 元日（2026/1/1 と 2027/1/1）の両方が含まれる
        dates = [h["date"] for h in days]
        assert "2026-01-01" in dates
        assert "2027-01-01" in dates


class TestHolidaysApi:
    """日本の祝日APIのテスト。"""

    def test_preview_returns_holidays(self, client):
        """japanese-preview エンドポイントが祝日リストを返す。"""
        from helpers import insert_shop, make_session, auth
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/holidays/japanese-preview", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert "holidays" in body
        assert len(body["holidays"]) > 0
        # 各祝日に date と name がある
        for h in body["holidays"]:
            assert "date" in h
            assert "name" in h

    def test_preview_with_years_param(self, client):
        """years クエリで年指定。"""
        from helpers import insert_shop, make_session, auth
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.get("/api/shop/holidays/japanese-preview?years=2026,2027", headers=auth(tok))
        assert r.status_code == 200
        body = r.get_json()
        assert 2026 in body["years"]
        assert 2027 in body["years"]
        dates = [h["date"] for h in body["holidays"]]
        assert "2026-01-01" in dates
        assert "2027-01-01" in dates

    def test_import_japanese_holidays(self, client):
        """import-japanese エンドポイントで祝日を一括登録。"""
        import db as dbmod
        from helpers import insert_shop, make_session, auth
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/holidays/import-japanese",
                        json={"years": [2026, 2027]},
                        headers=auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["imported"] > 0
        # DBに登録されていることを検証
        rows = dbmod.query_all("SELECT * FROM shop_holidays WHERE shop_id=?", (shop_id,))
        assert len(rows) >= 30  # 2年分で30日以上
        # 元日が含まれる
        dates = [row["holiday_date"] for row in rows]
        assert "2026-01-01" in dates
        assert "2027-01-01" in dates

    def test_import_is_idempotent(self, client):
        """2回インポートしても重複しない（既存はスキップ）。"""
        import db as dbmod
        from helpers import insert_shop, make_session, auth
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        # 1回目
        r1 = client.post("/api/shop/holidays/import-japanese",
                         json={"years": [2026]},
                         headers=auth(tok))
        assert r1.status_code == 200
        imported1 = r1.get_json()["imported"]
        # 2回目（重複スキップ）
        r2 = client.post("/api/shop/holidays/import-japanese",
                         json={"years": [2026]},
                         headers=auth(tok))
        assert r2.status_code == 200
        imported2 = r2.get_json()["imported"]
        assert imported2 == 0  # 2回目は全てスキップ
        assert r2.get_json()["skipped"] >= imported1

    def test_import_with_overwrite(self, client):
        """overwrite=True で既存祝日を上書き。"""
        import db as dbmod
        from helpers import insert_shop, make_session, auth
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        # 事前に1件手動追加（別のnote）
        dbmod.execute("INSERT INTO shop_holidays (shop_id, holiday_date, note) VALUES (?,?,?)",
                      (shop_id, "2026-01-01", "手動メモ"))
        # 上書きインポート
        r = client.post("/api/shop/holidays/import-japanese",
                        json={"years": [2026], "overwrite": True},
                        headers=auth(tok))
        assert r.status_code == 200
        # noteが「元日」に上書きされている
        row = dbmod.query_one("SELECT note FROM shop_holidays WHERE shop_id=? AND holiday_date=?",
                              (shop_id, "2026-01-01"))
        assert row["note"] == "元日"

    def test_import_holidays_isolated_per_shop(self, client):
        """店舗Aの祝日インポートは店舗Bに影響しない。"""
        import db as dbmod
        from helpers import insert_shop, make_session, auth
        shop_a = insert_shop(code="A")
        shop_b = insert_shop(code="B")
        tok_a = make_session("shop", shop_a, shop_a)
        tok_b = make_session("shop", shop_b, shop_b)
        # Aだけインポート
        client.post("/api/shop/holidays/import-japanese",
                    json={"years": [2026]},
                    headers=auth(tok_a))
        rows_b = dbmod.query_all("SELECT * FROM shop_holidays WHERE shop_id=?", (shop_b,))
        assert len(rows_b) == 0
