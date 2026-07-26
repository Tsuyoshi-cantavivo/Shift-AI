"""tests/test_wish_text_import.py — 希望テキスト取り込みのテスト。

実行: ./.venv/bin/python -m pytest tests/test_wish_text_import.py -v

解析は LLM を使わないフォールバック経路のみを検証する（外部APIに依存させない）。
LLM 経路は本番でのみ動き、失敗時は自動でフォールバックに落ちる設計。
"""
import pytest
from src import ai

import app as appmod
import db as dbmod
from helpers import insert_shop, insert_staff, make_session, auth


class TestParseWishFallback:
    """正規表現ベースの解析。LLM 未設定でも機能が死なないことを保証する。"""

    def test_single_date_rest(self):
        r = ai._parse_wish_fallback("8/3は休みたいです", "2026-08")
        assert len(r["entries"]) == 1
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-03"]
        assert e["availability"] == "rest"
        assert "8/3" in e["raw"]

    def test_multiple_dates_same_content(self):
        r = ai._parse_wish_fallback("8/3、8/5、8/7 は17時から22時まで入れます", "2026-08")
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-03", "2026-08-05", "2026-08-07"]
        assert e["availability"] == "time"
        assert e["start"] == "17:00"
        assert e["end"] == "22:00"

    def test_different_content_splits_entries(self):
        r = ai._parse_wish_fallback("8/1 9-17\n8/3 13-22", "2026-08")
        assert len(r["entries"]) == 2
        assert r["entries"][0]["start"] == "09:00"
        assert r["entries"][1]["start"] == "13:00"

    def test_date_range(self):
        r = ai._parse_wish_fallback("8/10〜8/12 は休みです", "2026-08")
        assert r["entries"][0]["dates"] == ["2026-08-10", "2026-08-11", "2026-08-12"]
        assert r["entries"][0]["availability"] == "rest"

    def test_any_availability(self):
        r = ai._parse_wish_fallback("8/15 終日OK", "2026-08")
        assert r["entries"][0]["availability"] == "any"

    def test_unparsed_line_is_kept(self):
        r = ai._parse_wish_fallback("よろしくお願いします", "2026-08")
        assert r["entries"] == []
        assert "よろしくお願いします" in r["unparsed"]

    def test_empty_text_does_not_crash(self):
        r = ai._parse_wish_fallback("", "2026-08")
        assert r["entries"] == []

    def test_staff_hint_extracted(self):
        r = ai._parse_wish_fallback("小久保: 8/3休み", "2026-08", ["小久保", "佐藤"])
        assert r["entries"][0]["staff_hint"] == "小久保"

    def test_source_is_fallback(self):
        r = ai._parse_wish_fallback("8/3は休み", "2026-08")
        assert r["source"] == "fallback"

    def test_comma_separated_conflicting_conditions_split_into_entries(self):
        """1行に『休み』と時刻が『、』区切りで並ぶ場合、休みが時刻指定を握りつぶさないこと。

        fix round 1: 「8/3は休み、8/5は17-22」を1行のまま解析すると rest語が
        先勝ちし、8/5 まで rest として誤登録される事故があった。小節ごとに
        条件を対応付けることで正しく2エントリに分かれることを保証する。
        """
        r = ai._parse_wish_fallback("8/3は休み、8/5は17-22", "2026-08")
        assert len(r["entries"]) == 2
        rest_entry = next(e for e in r["entries"] if e["availability"] == "rest")
        time_entry = next(e for e in r["entries"] if e["availability"] == "time")
        assert rest_entry["dates"] == ["2026-08-03"]
        assert time_entry["dates"] == ["2026-08-05"]
        assert time_entry["start"] == "17:00"
        assert time_entry["end"] == "22:00"

    def test_fullwidth_digits_are_normalized(self):
        """全角数字（IME変換でありがち）も日付として認識できること。"""
        r = ai._parse_wish_fallback("８/５は休みです", "2026-08")
        assert r["entries"][0]["dates"] == ["2026-08-05"]
        assert r["entries"][0]["availability"] == "rest"

    def test_date_before_kara_is_not_read_as_hour(self):
        """fix round 2: 「8/5からは休みです」の『5』が『N時から』パターンに
        誤って時刻として拾われ、休みだけの文が rest/time 混在の競合と
        誤判定されて unparsed に落ちていた回帰。日付トークンを取り除いてから
        時刻の有無を判定することで、正しく rest として抽出されること。
        """
        r = ai._parse_wish_fallback("8/5からは休みです", "2026-08")
        assert r["unparsed"] == []
        assert len(r["entries"]) == 1
        assert r["entries"][0]["dates"] == ["2026-08-05"]
        assert r["entries"][0]["availability"] == "rest"

    def test_date_before_made_is_not_read_as_hour(self):
        """fix round 2: 「8/9までNGです」の『9』が『N時まで』パターンに
        誤って時刻として拾われ unparsed に落ちていた回帰。
        """
        r = ai._parse_wish_fallback("8/9までNGです", "2026-08")
        assert r["unparsed"] == []
        assert len(r["entries"]) == 1
        assert r["entries"][0]["dates"] == ["2026-08-09"]
        assert r["entries"][0]["availability"] == "rest"

    def test_made_kara_split_across_comma_is_not_falsely_unparsed(self):
        """fix round 2/3: 「8/3までは休み、8/9からは出れます」の前半が
        『まで』により日番号3を時刻と誤読され、小節分割後も unparsed に
        落ちていた回帰（round 2）。前半は正しく rest/8-3 の1エントリになること。

        後半「8/9からは出れます」は休み語を含まないため round 2 の安全網
        (_wish_has_conflicting_signals) を通らない。round 3 修正前はここで
        日番号9が『9時から』と誤読され、availability="time", start="09:00"
        として安全網に掛からずそのまま黙って登録されてしまっていた
        （元テストはこのエントリのフィールドを assert していなかったため、
        この誤りを見逃していた）。round 3 修正後は availability を判定できず
        unparsed に送られ、誤った時刻が黙って確定しないことを検証する。
        """
        r = ai._parse_wish_fallback("8/3までは休み、8/9からは出れます", "2026-08")
        assert len(r["entries"]) == 1
        rest_entry = r["entries"][0]
        assert rest_entry["dates"] == ["2026-08-03"]
        assert rest_entry["availability"] == "rest"
        assert rest_entry["start"] is None
        assert rest_entry["end"] is None
        # round 3 の核心: 「8/9からは出れます」が time/09:00 として黙って登録されていないこと
        assert not any(e["availability"] == "time" and e["start"] == "09:00" for e in r["entries"])
        assert "8/9からは出れます" in r["unparsed"]

    def test_date_before_kara_without_rest_word_is_not_read_as_hour(self):
        """fix round 3: 休み語を伴わない「8/9からは出れます」でも、日番号9が
        『9時から』と誤読されて availability=time/start=09:00 が黙って
        登録されないこと。休み語が無いため round 2 の安全網
        (_wish_has_conflicting_signals) には掛からず、_extract_wish_availability
        自身が時刻判定の前に日付トークンを除去する必要があった。
        """
        availability, start, end = ai._extract_wish_availability("8/9からは出れます")
        assert not (availability == "time" and start == "09:00")

        r = ai._parse_wish_fallback("8/9からは出れます", "2026-08")
        assert not any(e["availability"] == "time" and e["start"] == "09:00" for e in r["entries"])

    def test_time_after_date_with_space_separator_is_still_time(self):
        """fix round 3のガード: 日付トークン除去が本物の時刻表現まで
        握りつぶさないこと。「8/5 5時から」は日付の『8/5』だけが除去され、
        本当の時刻表現である『5時から』は正しく time/05:00 として残ること。
        """
        r = ai._parse_wish_fallback("8/5 5時から", "2026-08")
        assert len(r["entries"]) == 1
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-05"]
        assert e["availability"] == "time"
        assert e["start"] == "05:00"
        assert e["end"] is None

    def test_single_segment_conflicting_signals_still_goes_to_unparsed(self):
        """(b) 最終防衛線の単一小節（カンマ分割を経由しない）経路の専用テスト。

        「8/3は休みだけど17-22なら」はカンマを含まないため _parse_wish_line が
        小節を1つしか作らない。日付トークン除去後も『休み』と有効な時刻範囲が
        同一小節に残る本物の競合であり、rest/time どちらかを黙って確定せず
        unparsed に送られ続けること（fix round 2 のリグレッション防止に対する回帰）。
        """
        r = ai._parse_wish_fallback("8/3は休みだけど17-22なら", "2026-08")
        assert r["entries"] == []
        assert r["unparsed"] == ["8/3は休みだけど17-22なら"]


    def test_ambiguous_segment_before_rest_does_not_flip_polarity(self):
        """fix round 4: 「8/9からは出れます」（＝出勤可能）が、後続の小節の
        rest に黙って吸収されて 8/9 が『休み』として登録される極性反転の回帰。

        round 3 で「8/9からは出れます」の availability が None になった結果、
        _parse_wish_line の `elif seg_dates:` 分岐（日付だけの小節を後続へ
        引き継ぐ経路）にこの小節が流れ込み、次の小節が宣言した rest に
        dates ごと吸収されていた。書かれた内容の真逆が unparsed も残さずに
        確定するため、譲れない原則（誤登録より unparsed）に反する。
        """
        r = ai._parse_wish_fallback("8/9からは出れます、8/12は休み", "2026-08")
        # 8/9 が rest として登録されていないこと（極性反転の核心）
        assert not any("2026-08-09" in e["dates"] for e in r["entries"])
        assert "8/9からは出れます" in r["unparsed"]
        # 8/12 は正しく rest のままであること（安全側に倒しすぎていないこと）
        assert len(r["entries"]) == 1
        assert r["entries"][0]["dates"] == ["2026-08-12"]
        assert r["entries"][0]["availability"] == "rest"

    def test_ambiguous_segment_between_dates_and_time_is_not_absorbed(self):
        """fix round 4: 曖昧な小節が実条件の小節より前にあるとき、述べられて
        いない時間帯が黙って割り当てられない回帰。

        「8/3、8/9からは出れます、8/12は17-22」は、修正前は3日付＋17:00-22:00の
        1エントリになり、8/9 に一度も述べられていない 17-22 が付いていた。

        設計判断: 未認識の小節が挟まったら、それ以前に溜まっている
        pending_dates（ここでは 8/3）も unparsed に送る。「8/3、8/9からは
        出れます」と読めば 8/3 は後続の 17-22 ではなく落とした文に係っている
        可能性があり、どちらに紐づくか決められないため。
        """
        r = ai._parse_wish_fallback("8/3、8/9からは出れます、8/12は17-22", "2026-08")
        # 核心: 8/9 に 17:00-22:00 が割り当たっていないこと
        assert not any("2026-08-09" in e["dates"] for e in r["entries"])
        assert len(r["entries"]) == 1
        assert r["entries"][0]["dates"] == ["2026-08-12"]
        assert r["entries"][0]["availability"] == "time"
        assert r["entries"][0]["start"] == "17:00"
        assert r["entries"][0]["end"] == "22:00"
        # 挟まれた pending_dates（8/3）も曖昧な小節も人に渡すこと
        assert "8/3" in r["unparsed"]
        assert "8/9からは出れます" in r["unparsed"]

    def test_ambiguous_segment_without_kara_is_also_not_absorbed(self):
        """fix round 4: 同じ穴は『から』『まで』に依存しない。
        「8/9出勤希望」も availability を判定できない日付付き小節であり、
        後続の rest に吸収されてはいけない（round 3 以前から存在した経路）。
        """
        r = ai._parse_wish_fallback("8/9出勤希望、8/12は休み", "2026-08")
        assert not any("2026-08-09" in e["dates"] for e in r["entries"])
        assert "8/9出勤希望" in r["unparsed"]
        assert len(r["entries"]) == 1
        assert r["entries"][0]["dates"] == ["2026-08-12"]
        assert r["entries"][0]["availability"] == "rest"

    def test_date_only_segment_with_particle_still_carries_over(self):
        """fix round 4 のガード: 助詞つきの『日付だけの小節』は引き継ぎが
        壊れていないこと。「8/3は」は日付＋助詞のみで意味を持つ残骸が無いため、
        後続の「8/5は17-22」に条件を引き継ぎ、1エントリ2日付になる。
        """
        r = ai._parse_wish_fallback("8/3は、8/5は17-22", "2026-08")
        assert r["unparsed"] == []
        assert len(r["entries"]) == 1
        e = r["entries"][0]
        assert e["dates"] == ["2026-08-03", "2026-08-05"]
        assert e["availability"] == "time"
        assert e["start"] == "17:00"
        assert e["end"] == "22:00"

    def test_segment_is_dates_only_classification(self):
        """fix round 4 の判定基準そのものを直接検証する。

        日付範囲・日付トークン・助詞・記号を取り除いて実質空なら『日付だけ』。
        『から』『まで』『出れます』のような意味を持つ語は残骸として扱わない。
        """
        assert ai._wish_segment_is_dates_only("8/3") is True
        assert ai._wish_segment_is_dates_only("8/3は") is True
        assert ai._wish_segment_is_dates_only("8月3日") is True
        assert ai._wish_segment_is_dates_only("8/10〜8/12") is True
        assert ai._wish_segment_is_dates_only("8/10から8/12") is True  # 範囲の区切りの『から』
        assert ai._wish_segment_is_dates_only("8/9からは出れます") is False
        assert ai._wish_segment_is_dates_only("8/9出勤希望") is False
        assert ai._wish_segment_is_dates_only("8/9は無理かも") is False

    def test_staff_prefix_before_date_only_segment_still_carries_over(self):
        """fix round 4 のガード: 行頭の『名前:』『【名前】』は希望内容ではなく
        行単位のメタ情報なので、先頭小節を『日付だけ』と見なす妨げにならないこと。
        """
        r = ai._parse_wish_fallback("小久保: 8/3、8/5は17-22", "2026-08", ["小久保"])
        assert r["unparsed"] == []
        assert len(r["entries"]) == 1
        e = r["entries"][0]
        assert e["staff_hint"] == "小久保"
        assert e["dates"] == ["2026-08-03", "2026-08-05"]
        assert e["availability"] == "time"


class TestParseWishText:
    """LLM が使えない環境では自動でフォールバックに落ちること。"""

    def test_falls_back_when_llm_unavailable(self, monkeypatch):
        monkeypatch.setattr(ai, "is_llm_available", lambda: False)
        r = ai.parse_wish_text("8/3は休み", "2026-08")
        assert r["source"] == "fallback"
        assert r["entries"][0]["availability"] == "rest"


class TestWishParseApi:
    """POST /api/shop/wishes/parse — 解析のみ。保存はしない。"""

    def _counts(self):
        wh = dbmod.query_one("SELECT COUNT(*) as c FROM wish_history")["c"]
        sh = dbmod.query_one("SELECT COUNT(*) as c FROM shifts")["c"]
        return wh, sh

    def test_parse_returns_entries_without_saving(self, client):
        """解析しても DB には保存されないこと。staff_hint が一致すれば staff_id が付く。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)
        before_wh, before_sh = self._counts()

        r = client.post("/api/shop/wishes/parse",
                         json={"text": "小久保: 8/3は休みたいです", "year_month": "2026-08"},
                         headers=auth(token))

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert len(body["entries"]) == 1
        e = body["entries"][0]
        assert e["dates"] == ["2026-08-03"]
        assert e["availability"] == "rest"
        assert e["staff_hint"] == "小久保"
        assert e["staff_id"] == staff_id
        assert body["source"] == "fallback"

        after_wh, after_sh = self._counts()
        assert after_wh == before_wh
        assert after_sh == before_sh

    def test_parse_unresolved_staff_hint_is_null(self, client):
        """スタッフ名と一致しない staff_hint は推測せず None のままにする。"""
        shop_id = insert_shop()
        insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                         json={"text": "リーダー: 8/3は休みたいです", "year_month": "2026-08"},
                         headers=auth(token))

        assert r.status_code == 200, r.get_json()
        e = r.get_json()["entries"][0]
        assert e["staff_hint"] == "リーダー"
        assert e["staff_id"] is None

    def test_parse_excludes_resigned_staff(self, client):
        """退職者は staff_hint 解決の候補から外れる。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        dbmod.execute("UPDATE staffs SET is_resigned=1 WHERE id=?", (staff_id,))
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                         json={"text": "小久保: 8/3は休みたいです", "year_month": "2026-08"},
                         headers=auth(token))

        assert r.status_code == 200, r.get_json()
        e = r.get_json()["entries"][0]
        assert e["staff_id"] is None

    def test_parse_requires_shop_role(self, client):
        """staff ロールでは 403。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("staff", staff_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                         json={"text": "8/3は休みたいです", "year_month": "2026-08"},
                         headers=auth(token))

        assert r.status_code == 403

    def test_parse_with_staff_id_assigns_all_entries(self, client):
        """staff_id を指定すると staff_hint を無視して全件その人になる。"""
        shop_id = insert_shop()
        insert_staff(shop_id, "E1", "小久保")
        target_id = insert_staff(shop_id, "E2", "佐藤")
        token = make_session("shop", shop_id, shop_id)

        r = client.post(
            "/api/shop/wishes/parse",
            json={
                "text": "小久保: 8/3は休みたいです\n8/5、8/7 は17時から22時まで入れます",
                "year_month": "2026-08",
                "staff_id": target_id,
            },
            headers=auth(token))

        assert r.status_code == 200, r.get_json()
        entries = r.get_json()["entries"]
        assert len(entries) == 2
        for e in entries:
            assert e["staff_id"] == target_id

    def test_parse_empty_text_returns_400(self, client):
        """text が空なら 400。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                         json={"text": "", "year_month": "2026-08"},
                         headers=auth(token))

        assert r.status_code == 400

    def test_parse_missing_text_returns_400(self, client):
        """text キー自体が無くても 400。"""
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/wishes/parse",
                         json={"year_month": "2026-08"},
                         headers=auth(token))

        assert r.status_code == 400


class TestWishBulkApi:
    """POST /api/shop/wishes/bulk — プレビューで確定した希望を実際に登録する。

    最重要: shifts(status=requested) と wish_history の両方に入ること。
    片方だけでは機能しない（前者はAI生成の入力、後者は希望表管理画面が読む）。
    """

    def _shifts_requested_count(self, shop_id=None):
        if shop_id is None:
            return dbmod.query_one("SELECT COUNT(*) as c FROM shifts WHERE status='requested'")["c"]
        return dbmod.query_one(
            "SELECT COUNT(*) as c FROM shifts WHERE status='requested' AND shop_id=?", (shop_id,))["c"]

    def _wish_history_count(self, shop_id=None):
        if shop_id is None:
            return dbmod.query_one("SELECT COUNT(*) as c FROM wish_history")["c"]
        return dbmod.query_one("SELECT COUNT(*) as c FROM wish_history WHERE shop_id=?", (shop_id,))["c"]

    def test_creates_in_both_tables(self, client):
        """shifts(status=requested) と wish_history の両方に入ること。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)

        wishes = [
            {"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
             "start": None, "end": None, "raw": "8/3は休みたいです"},
            {"staff_id": staff_id, "date": "2026-08-04", "availability": "any",
             "start": None, "end": None, "raw": "8/4は終日OK"},
            {"staff_id": staff_id, "date": "2026-08-05", "availability": "time",
             "start": "17:00", "end": "22:00", "raw": "8/5は17-22"},
        ]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["ok"] is True
        assert body["created"] == 3
        assert body["skipped"] == 0
        assert self._shifts_requested_count(shop_id) == 3
        assert self._wish_history_count(shop_id) == 3

    def test_rest_uses_full_day(self, client):
        """availability=rest は 00:00:00〜23:59:59 で入ること。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)

        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
                   "start": None, "end": None, "raw": "8/3は休み"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='requested'",
            (staff_id,))
        assert row["start_datetime"] == "2026-08-03T00:00:00"
        assert row["end_datetime"] == "2026-08-03T23:59:59"
        wh = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM wish_history WHERE staff_id=?", (staff_id,))
        assert wh["start_datetime"] == "2026-08-03T00:00:00"
        assert wh["end_datetime"] == "2026-08-03T23:59:59"

    def test_availability_uses_shop_end_time(self, client):
        """any/morning/evening は 09:00 開始・店舗の終了時刻で入ること。"""
        shop_id = insert_shop(settings={"shift_hours": {"bulk": {"start_time": "09:00", "end_time": "21:30"}}})
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)

        wishes = [
            {"staff_id": staff_id, "date": "2026-08-03", "availability": "morning",
             "start": None, "end": None, "raw": "8/3は早番希望"},
            {"staff_id": staff_id, "date": "2026-08-04", "availability": "evening",
             "start": None, "end": None, "raw": "8/4は遅番希望"},
        ]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        rows = dbmod.query_all(
            "SELECT start_datetime, end_datetime, availability FROM shifts "
            "WHERE staff_id=? AND status='requested' ORDER BY start_datetime", (staff_id,))
        assert len(rows) == 2
        for row in rows:
            assert row["start_datetime"].endswith("T09:00:00")
            assert row["end_datetime"].endswith("T21:30:00")

    def test_time_overnight_wraps_to_next_day(self, client):
        """availability=time で end<=start なら翌日扱いになること。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)

        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "time",
                   "start": "22:00", "end": "05:00", "raw": "8/3夜勤"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='requested'",
            (staff_id,))
        assert row["start_datetime"] == "2026-08-03T22:00:00"
        assert row["end_datetime"] == "2026-08-04T05:00:00"

    def test_duplicate_is_skipped(self, client):
        """同じ (staff_id, date) を2回登録したら2回目はスキップされること。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)
        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
                   "start": None, "end": None, "raw": "8/3は休み"}]

        r1 = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))
        r2 = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r1.get_json()["created"] == 1
        assert r2.get_json()["created"] == 0
        assert r2.get_json()["skipped"] == 1
        assert self._shifts_requested_count(shop_id) == 1
        assert self._wish_history_count(shop_id) == 1

    def test_overwrite_replaces_existing(self, client):
        """overwrite=true なら既存を消して入れ直すこと。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)
        first = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
                  "start": None, "end": None, "raw": "8/3は休み"}]
        second = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "time",
                   "start": "17:00", "end": "22:00", "raw": "8/3は17-22に変更"}]

        client.post("/api/shop/wishes/bulk", json={"wishes": first}, headers=auth(token))
        r = client.post("/api/shop/wishes/bulk", json={"wishes": second, "overwrite": True}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        assert r.get_json()["created"] == 1
        assert self._shifts_requested_count(shop_id) == 1
        assert self._wish_history_count(shop_id) == 1
        row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='requested'",
            (staff_id,))
        assert row["start_datetime"] == "2026-08-03T17:00:00"
        assert row["end_datetime"] == "2026-08-03T22:00:00"

    def test_ignores_deadline(self, client):
        """締切を過ぎていても店長は登録できること（スタッフの提出とは違う）。

        募集期間(shift_request_periods)を一切作らない店舗でも通ることを確認する。
        """
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)
        # 締切が過去の募集期間をわざと作る（スタッフ提出なら 400 になる状況）
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)",
            (shop_id, "2026-08-01", "2026-08-31", "2020-01-01"))

        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
                   "start": None, "end": None, "raw": "8/3は休み"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        assert r.get_json()["created"] == 1

    def test_rejects_other_shop_staff(self, client):
        """他店舗の staff_id は拒否されること。"""
        shop_id = insert_shop(code="SHOP1")
        other_shop_id = insert_shop(code="SHOP2")
        other_staff_id = insert_staff(other_shop_id, "E1", "他店舗スタッフ")
        token = make_session("shop", shop_id, shop_id)

        wishes = [{"staff_id": other_staff_id, "date": "2026-08-03", "availability": "rest",
                   "start": None, "end": None, "raw": "8/3は休み"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["created"] == 0
        assert body["skipped"] == 1
        assert self._shifts_requested_count() == 0
        assert self._wish_history_count() == 0

    def test_requires_shop_role(self, client):
        """staff ロールでは 403。"""
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("staff", staff_id, shop_id)

        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
                   "start": None, "end": None, "raw": "8/3は休み"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 403

    def test_rejects_unknown_availability(self, client):
        """rest/any/morning/evening/time 以外の availability はスキップされること。

        既知語彙以外を any/morning/evening 扱いにフォールバックさせると、
        希望表管理画面の .wmark が未知トークンで表示崩れを起こすため。
        """
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)

        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "typo",
                   "start": None, "end": None, "raw": "不明な値"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["created"] == 0
        assert body["skipped"] == 1
        assert self._shifts_requested_count(shop_id) == 0
        assert self._wish_history_count(shop_id) == 0

    def test_wish_history_duplicate_without_shift_overlap_stays_in_sync(self, client):
        """wish_history に既存行があるが shifts には重なりが無い状況でも、
        created が実際にDBへ入った件数と一致すること（両テーブル非対称の回帰）。

        _check_staff_overlap（shifts側・重なり判定）と wish_history の完全一致
        判定は基準が異なる。shifts に何も無ければ overlap は False になるため、
        wish_history 側だけを見ずに INSERT すると、shifts にだけ新規行ができて
        wish_history には入らない（=非対称）まま created が加算されてしまう。
        """
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)
        # wish_history にだけ既存の rest 希望を直接投入する（shifts 側は空のまま）。
        # _wish_times("2026-08-03", "rest", ...) が返す値と完全一致させる。
        dbmod.execute(
            "INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, availability, note) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, staff_id, "2026-08-03T00:00:00", "2026-08-03T23:59:59", "rest", "手動投入"))
        assert self._shifts_requested_count(shop_id) == 0
        assert self._wish_history_count(shop_id) == 1

        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
                   "start": None, "end": None, "raw": "8/3は休み"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["created"] == 0
        assert body["skipped"] == 1
        # shifts に孤立した新規行ができていないこと（＝wish_historyに入らないのに shiftsだけ増える、を防ぐ）
        assert self._shifts_requested_count(shop_id) == 0
        # wish_history も重複INSERTされていないこと
        assert self._wish_history_count(shop_id) == 1

    def test_wish_history_insert_failure_rolls_back_shift(self, client, monkeypatch):
        """wish_history への INSERT が本物のDBエラーで失敗したら、直前に作った
        shifts 行を取り消し、created ではなく skipped に計上すること。

        「登録できていないのに登録した」と表示する事故を防ぐための回帰。
        """
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "小久保")
        token = make_session("shop", shop_id, shop_id)

        real_execute = appmod.execute

        def fake_execute(sql, params=()):
            if sql.strip().startswith("INSERT INTO wish_history"):
                raise RuntimeError("simulated wish_history insert failure")
            return real_execute(sql, params)

        monkeypatch.setattr(appmod, "execute", fake_execute)

        wishes = [{"staff_id": staff_id, "date": "2026-08-03", "availability": "rest",
                   "start": None, "end": None, "raw": "8/3は休み"}]
        r = client.post("/api/shop/wishes/bulk", json={"wishes": wishes}, headers=auth(token))

        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["created"] == 0
        assert body["skipped"] == 1
        # shifts 行がロールバックされ、孤立レコードが残っていないこと
        assert self._shifts_requested_count(shop_id) == 0
        assert self._wish_history_count(shop_id) == 0
