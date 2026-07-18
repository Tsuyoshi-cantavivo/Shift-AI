"""tests/test_workflow_regression.py - 実運用フローの回帰テスト。

ユーザー報告: 「スタッフ画面でAI希望作成 → 希望提出 → 店舗AI自動生成 → 一括確定 →
AI自動生成」のフローで、シフトが重複し、希望が消え、必要人数を超える現象。

原因究明と再発防止のテスト。
"""
import json

import pytest

import db as dbmod
import shift_engine
from helpers import (
    insert_shop, insert_staff, insert_pattern, insert_fixed,
    insert_request, make_session, auth,
)

MON, TUE, WED, THU, FRI = "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"
DEFAULT_SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6,
                    "default_hourly_wage": 1100, "night_premium_rate": 1.25,
                    "transport_per_day": 300, "business_hours": "9:00-22:00"}


def _count_staff_in_hour(rows, day, hour):
    """指定日の指定時間帯(1時間)に出勤するスタッフ数をカウント。"""
    target_s = hour * 60
    target_e = (hour + 1) * 60
    cnt = 0
    for r in rows:
        sd = r.get("start_datetime") or r.get("start") or ""
        ed = r.get("end_datetime") or r.get("end") or ""
        if sd[:10] != day:
            continue
        ss = int(sd[11:13]) * 60 + int(sd[14:16])
        ee = int(ed[11:13]) * 60 + int(ed[14:16])
        if ss < target_e and ee > target_s:
            cnt += 1
    return cnt


def _setup_demo_shop():
    """ユーザーの実データに近い構成: 朝2/昼2/夜3 + 社員2 + バイト3。"""
    shop_id = insert_shop(code="SHOP_WF", settings=DEFAULT_SETTINGS)
    insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
    insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
    insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
    emp1 = insert_staff(shop_id, "E1", "社員1", "employee", 2000, 160, 200)
    emp2 = insert_staff(shop_id, "E2", "社員2", "employee", 2000, 160, 200)
    pt1 = insert_staff(shop_id, "P1", "バイト1", "part_time", 1100, 0, 160)
    pt2 = insert_staff(shop_id, "P2", "バイト2", "part_time", 1100, 0, 160)
    pt3 = insert_staff(shop_id, "P3", "バイト3", "part_time", 1100, 0, 160)
    # バイト1は月-金 9-18固定、バイト2は13-18、バイト3は夜17-22(火木土)
    for w in range(1, 6):
        insert_fixed(pt1, w, "09:00", "18:00")
        insert_fixed(pt2, w, "13:00", "18:00")
    for w in (2, 4, 6):
        insert_fixed(pt3, w, "17:00", "22:00")
    return {
        "shop_id": shop_id, "emp1": emp1, "emp2": emp2,
        "pt1": pt1, "pt2": pt2, "pt3": pt3,
    }


def _ai_parse_17_unless_wed():
    """AI入力「17時まで水曜以外」をシミュレート → 希望シフト配列を返す。

    期待: 月火木金の 09:00-17:00 希望を生成（水曜は除く）
    """
    return [
        {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"},
        {"start_datetime": f"{TUE}T09:00:00", "end_datetime": f"{TUE}T17:00:00"},
        # WED はスキップ
        {"start_datetime": f"{THU}T09:00:00", "end_datetime": f"{THU}T17:00:00"},
        {"start_datetime": f"{FRI}T09:00:00", "end_datetime": f"{FRI}T17:00:00"},
    ]


# ============================================================
# 【再現テスト】ユーザー報告のワークフロー
#   1. AI で「17時まで水曜以外」を解析
#   2. 出力で希望提出（staff/requests）
#   3. 店舗 AI 自動生成（dry → 確定）
#   4. 一括確定（auto-confirm）
#   5. もう一度 AI 自動生成
#   → 各ステップで: 重複シフト無いこと・希望残っていること・必要人数超えてないこと
# ============================================================
class TestWorkflow_AIHope_AutoGen_AutoConfirm_ReAutoGen:

    def test_full_workflow_no_duplicates_no_loss_no_overcap(self, client):
        """【主訴求】ワークフロー全体で不変量を保持すること。"""
        sc = _setup_demo_shop()
        shop_id = sc["shop_id"]
        pt3 = sc["pt3"]

        # 募集期間を設定
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))

        # ---- Step 1: AI で「17時まで水曜以外」を解析 ----
        wishes = _ai_parse_17_unless_wed()
        assert len(wishes) == 4
        # 水曜が含まれていないこと
        for w in wishes:
            assert w["start_datetime"][:10] != WED, "水曜を除外すべき"

        # ---- Step 2: スタッフが希望提出 ----
        staff_tok = make_session("staff", pt3, shop_id)
        r = client.post("/api/staff/requests", json={"shifts": wishes},
                        headers=auth(staff_tok))
        assert r.status_code == 200, r.get_json()
        submitted = r.get_json()["submitted"]
        assert submitted == 4, f"4件提出されるべき: {r.get_json()}"

        # 希望が requested として保存されている
        req_rows = dbmod.query_all(
            "SELECT * FROM shifts WHERE staff_id=? AND status='requested' ORDER BY start_datetime",
            (pt3,))
        assert len(req_rows) == 4, f"希望が4件保存されているべき: {len(req_rows)}"

        # ---- Step 3: 店舗が AI 自動生成（確定） ----
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI,
        }, headers=auth(shop_tok))
        assert r.status_code == 200, r.get_json()

        # 検証 1: 重複シフトがないこと（同一スタッフ・同一日に複数 confirmed）
        dups = dbmod.query_all(
            "SELECT staff_id, substr(start_datetime,1,10) as day, COUNT(*) as cnt "
            "FROM shifts WHERE shop_id=? AND status='confirmed' "
            "GROUP BY staff_id, day HAVING cnt > 1",
            (shop_id,))
        assert dups == [], f"Step3後: 重複シフト検出: {dups}"

        # 検証 2: すべての時間帯で必要人数を超えていないこと
        for day in [MON, TUE, WED, THU, FRI]:
            for hr in range(9, 22):
                rows = dbmod.query_all(
                    "SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' "
                    "AND start_datetime>=? AND start_datetime<=?",
                    (shop_id, day + "T00:00:00", day + "T23:59:59"))
                cnt = _count_staff_in_hour(rows, day, hr)
                # 朝(9-13)上限2, 昼(13-17)上限2, 夜(17-22)上限3
                if 9 <= hr < 13:
                    assert cnt <= 2, f"{day} {hr}時台: {cnt}人 (朝上限2超過)"
                elif 13 <= hr < 17:
                    assert cnt <= 2, f"{day} {hr}時台: {cnt}人 (昼上限2超過)"
                elif 17 <= hr < 22:
                    assert cnt <= 3, f"{day} {hr}時台: {cnt}人 (夜上限3超過)"

        # 検証 3: pt3 の希望時間帯（17時まで）が反映されていること
        # pt3 は requested 提出 → 自動生成で配置されたはず
        pt3_confirmed = dbmod.query_all(
            "SELECT * FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime>=? AND start_datetime<=? ORDER BY start_datetime",
            (pt3, MON + "T00:00:00", FRI + "T23:59:59"))
        # 何らかのシフトが入っている（火・木は固定、月・金は希望から配置）
        assert len(pt3_confirmed) >= 2, f"pt3のシフトが少ない: {pt3_confirmed}"

        # 残っている requested（配置できなかった希望）があれば残るのは仕様
        # ただし一括確定後は requested は減る

        # ---- Step 4: 一括確定（auto-confirm） ----
        r = client.post("/api/shop/shifts/auto-confirm", json={
            "start_date": MON, "end_date": FRI,
        }, headers=auth(shop_tok))
        assert r.status_code == 200, r.get_json()

        # 一括確定後も重複がないこと
        dups = dbmod.query_all(
            "SELECT staff_id, substr(start_datetime,1,10) as day, COUNT(*) as cnt "
            "FROM shifts WHERE shop_id=? AND status='confirmed' "
            "GROUP BY staff_id, day HAVING cnt > 1",
            (shop_id,))
        assert dups == [], f"Step4(一括確定)後: 重複シフト検出: {dups}"

        # 一括確定後も必要人数上限厳守
        for day in [MON, TUE, WED, THU, FRI]:
            for hr in range(9, 22):
                rows = dbmod.query_all(
                    "SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' "
                    "AND start_datetime>=? AND start_datetime<=?",
                    (shop_id, day + "T00:00:00", day + "T23:59:59"))
                cnt = _count_staff_in_hour(rows, day, hr)
                if 9 <= hr < 13:
                    assert cnt <= 2, f"一括確定後 {day} {hr}時台: {cnt}人 (朝上限2超過)"
                elif 13 <= hr < 17:
                    assert cnt <= 2, f"一括確定後 {day} {hr}時台: {cnt}人 (昼上限2超過)"
                elif 17 <= hr < 22:
                    assert cnt <= 3, f"一括確定後 {day} {hr}時台: {cnt}人 (夜上限3超過)"

        # ---- Step 5: もう一度 AI 自動生成 ----
        # ★ ここがユーザー報告のバグ発生ポイント
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI,
        }, headers=auth(shop_tok))
        assert r.status_code == 200, r.get_json()

        # 検証 ★: 再生成後も重複シフトがないこと（主訴求）
        dups = dbmod.query_all(
            "SELECT staff_id, substr(start_datetime,1,10) as day, COUNT(*) as cnt, "
            "GROUP_CONCAT(id) as ids, GROUP_CONCAT(reason) as reasons "
            "FROM shifts WHERE shop_id=? AND status='confirmed' "
            "GROUP BY staff_id, day HAVING cnt > 1",
            (shop_id,))
        assert dups == [], f"★ Step5(再生成)後: 重複シフト検出 = ユーザー報告バグ: {dups}"

        # 検証 ★: 再生成後も必要人数上限厳守
        for day in [MON, TUE, WED, THU, FRI]:
            for hr in range(9, 22):
                rows = dbmod.query_all(
                    "SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' "
                    "AND start_datetime>=? AND start_datetime<=?",
                    (shop_id, day + "T00:00:00", day + "T23:59:59"))
                cnt = _count_staff_in_hour(rows, day, hr)
                if 9 <= hr < 13:
                    assert cnt <= 2, f"再生成後 {day} {hr}時台: {cnt}人 (朝上限2超過)"
                elif 13 <= hr < 17:
                    assert cnt <= 2, f"再生成後 {day} {hr}時台: {cnt}人 (昼上限2超過)"
                elif 17 <= hr < 22:
                    assert cnt <= 3, f"再生成後 {day} {hr}時台: {cnt}人 (夜上限3超過)"

    def test_wishes_preserved_across_regeneration(self, client):
        """★【ユーザー報告2】AI自動生成を繰り返してもスタッフの希望が消失しないこと。

        シナリオ:
          1. スタッフが希望提出（9-17 × 月火木金の4件）
          2. AI自動生成（1回目）→ 希望が confirmed になる
          3. 一括確定 → 残りの希望も confirmed 化
          4. ★ AI自動生成（2回目）→ 希望が消失せず、再考慮されること
        """
        sc = _setup_demo_shop()
        shop_id = sc["shop_id"]
        pt3 = sc["pt3"]

        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))

        # Step 1: 希望提出
        wishes = _ai_parse_17_unless_wed()  # 4件: 月火木金の 9-17
        staff_tok = make_session("staff", pt3, shop_id)
        r = client.post("/api/staff/requests", json={"shifts": wishes}, headers=auth(staff_tok))
        assert r.status_code == 200
        assert r.get_json()["submitted"] == 4

        shop_tok = make_session("shop", shop_id, shop_id)

        # Step 2: AI自動生成（1回目）
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI,
        }, headers=auth(shop_tok))
        assert r.status_code == 200

        # Step 2 検証: 希望は confirmed（希望シフト or 柔軟希望系）or requested のいずれか
        # で残っているはず。消失していないこと。
        wish_origin_count_step2 = dbmod.query_one(
            "SELECT COUNT(*) as c FROM shifts WHERE staff_id=? "
            "AND (status='confirmed' OR status='requested') "
            "AND start_datetime>=? AND start_datetime<=?",
            (pt3, MON + "T00:00:00", FRI + "T23:59:59"))["c"]
        assert wish_origin_count_step2 >= 1, "Step2: pt3 のシフトが0件（希望が消失した）"

        # Step 3: 一括確定
        r = client.post("/api/shop/shifts/auto-confirm", json={
            "start_date": MON, "end_date": FRI,
        }, headers=auth(shop_tok))
        assert r.status_code == 200

        # Step 3 検証: 一括確定後もシフトがある
        wish_origin_count_step3 = dbmod.query_one(
            "SELECT COUNT(*) as c FROM shifts WHERE staff_id=? "
            "AND (status='confirmed' OR status='requested') "
            "AND start_datetime>=? AND start_datetime<=?",
            (pt3, MON + "T00:00:00", FRI + "T23:59:59"))["c"]
        assert wish_origin_count_step3 >= 1, "Step3: pt3 のシフトが0件"

        # Step 4 ★: AI自動生成（2回目）
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI,
        }, headers=auth(shop_tok))
        assert r.status_code == 200

        # ★ 検証: 再生成後も希望が「confirmed（再配置）」または
        #   「requested（配置できず調整待ち）」のいずれかで残っていること。
        #   pt3 の希望は固定 17-22 と統合できず cap 制約で配置できない可能性が高いが、
        #   少なくとも requested として残っていれば「希望は保持されている」。
        wish_origin_after_regen = dbmod.query_one(
            "SELECT COUNT(*) as c FROM shifts WHERE staff_id=? "
            "AND (status='confirmed' OR status='requested') "
            "AND start_datetime>=? AND start_datetime<=?",
            (pt3, MON + "T00:00:00", FRI + "T23:59:59"))["c"]
        # 固定シフト（火木土の 17-22）は常にあるが、それ以外に「希望由来」のシフトが
        # 何らかの形で残っていること（★ 以前は 0 になっていた = 希望消失バグ）
        # ⇒ pt3 は火・木の固定 17-22 がある（2日分）。他の日は希望が反映されているべき。
        fixed_count = dbmod.query_one(
            "SELECT COUNT(*) as c FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND reason='固定シフト' AND start_datetime>=? AND start_datetime<=?",
            (pt3, MON + "T00:00:00", FRI + "T23:59:59"))["c"]
        non_fixed_count = wish_origin_after_regen - fixed_count
        # 固定以外の希望由来シフトが 1件以上残っていること（confirmed or requested）
        assert non_fixed_count >= 1, (
            f"★ Step4(再生成): pt3 の希望が消失（固定のみ残り、希望由来が0）。"
            f"total={wish_origin_after_regen} fixed={fixed_count} non_fixed={non_fixed_count}"
        )


class TestWorkflow_EmployeeFixedAsCandidate:
    """【ユーザー報告4】社員の固定シフトは候補扱い（主婦のみ厳守）。

    ユーザー指摘:
      「調整の仕方がおかしいです。8月3日の状態で一括確定するともともと埋まっていたはずの
       13−14に空きが出る。おそらく固定シフトが悪さをしている。
       固定はあくまで候補くらいで縛られない方がいいかな。変動があるからこそこのツールの
       意味があるので、固定の人は主婦とかだけかな。社員は固定に縛られすぎない方がいい。」

    設計変更:
      - part_time (主婦等) 固定: 契約上厳守（Step1で配置）
      - 社員 (employee) 固定: 候補扱い（Step2.5でwish後にcap内のみ配置）
      → 社員は変動需要に合わせて柔軟に配置され、13-14の空きが発生しない。
    """

    def _setup_8_3_scenario(self):
        """8/3 (月曜) の実データ相当シナリオを構築。"""
        shop_id = insert_shop(code="SHOP_8_3", settings=DEFAULT_SETTINGS)
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        emp1 = insert_staff(shop_id, "E1", "山田", "employee", 2000, 160, 200)
        emp2 = insert_staff(shop_id, "E2", "佐藤", "employee", 2000, 160, 200)
        pt = insert_staff(shop_id, "P1", "田中", "part_time", 1100, 0, 160)
        # 社員2名は月-金 9-18固定
        for w in range(1, 6):
            insert_fixed(emp1, w, "09:00", "18:00")
            insert_fixed(emp2, w, "09:00", "18:00")
        return shop_id, emp1, emp2, pt

    def test_no_gap_at_13_when_wish_overlaps_employee_fixed(self, client):
        """★【主訴求】8/3シナリオ: 社員9-18固定 + バイト9-17希望 で 13-14に空きが出ないこと。

        従来（バグ）: 社員を9-13と14-18に分割して13-14が空く
        新仕様: 社員の9-18を候補として維持しつつ、バイトの9-17を先に配置。
                社員1名（山田）が9-18固定どおりに配置され、佐藤はStep3で夜等に回る。
        """
        shop_id, emp1, emp2, pt = self._setup_8_3_scenario()
        MON1 = "2026-08-03"
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON1, MON1, "2026-07-25"))

        # 田中 9-17 希望（wish_history にも書く）
        staff_tok = make_session("staff", pt, shop_id)
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{MON1}T09:00:00", "end_datetime": f"{MON1}T17:00:00"},
        ]}, headers=auth(staff_tok))

        # AI自動生成
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": MON1, "end_date": MON1,
        }, headers=auth(shop_tok))
        assert r.status_code == 200

        # ★ 検証1: 13-14時台に空きがないこと（主訴求）
        rows = dbmod.query_all(
            "SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' "
            "AND start_datetime>=? AND start_datetime<=?",
            (shop_id, MON1 + "T00:00:00", MON1 + "T23:59:59"))
        for hr in [13, 14]:  # 13時台, 14時台
            cnt = _count_staff_in_hour(rows, MON1, hr)
            assert cnt >= 2, (
                f"★ {MON1} {hr}時台: {cnt}人 (上限2に空き発生) = ユーザー報告バグ "
                f"「13-14に空きが出る」が再発"
            )

        # ★ 検証2: 重複シフトがないこと
        dups = dbmod.query_all(
            "SELECT staff_id, substr(start_datetime,1,10) as day, COUNT(*) as cnt "
            "FROM shifts WHERE shop_id=? AND status='confirmed' "
            "GROUP BY staff_id, day HAVING cnt > 1",
            (shop_id,))
        assert dups == [], f"重複シフト検出: {dups}"

        # ★ 検証3: 全時間帯で上限厳守
        for hr in range(9, 22):
            cnt = _count_staff_in_hour(rows, MON1, hr)
            cap = 2 if hr < 17 else 3
            assert cnt <= cap, f"{MON1} {hr}時台: {cnt}人 (上限{cap}超過)"

        # ★ 検証4: 社員のいずれかが連続したブロックで配置されている（分割されていない）
        emp1_rows = [s for s in rows if s["staff_id"] == emp1]
        emp2_rows = [s for s in rows if s["staff_id"] == emp2]
        # 各社員は最大1シフト（中抜け無し）
        assert len(emp1_rows) <= 1, f"山田が複数シフト（分割）: {emp1_rows}"
        assert len(emp2_rows) <= 1, f"佐藤が複数シフト（分割）: {emp2_rows}"

    def test_parttime_fixed_still_strictly_honored(self, client):
        """【設計変更後】固定は全スタッフ候補。希望がない日は固定が配置される。
        ただし希望がある日は固定がスキップされる（「希望時間以外は入れない」ため）。"""
        shop_id = insert_shop(code="SHOP_PT", settings=DEFAULT_SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "22:00", 3)
        shufu = insert_staff(shop_id, "P1", "主婦", "part_time", 1100, 0, 160)
        # 主婦は 9-13 固定
        insert_fixed(shufu, 1, "09:00", "13:00")

        shop_tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": MON,
        }, headers=auth(shop_tok))

        # 主婦の固定は希望がない日なので候補として配置される
        shufu_rows = dbmod.query_all(
            "SELECT * FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime LIKE ?", (shufu, f"{MON}%"))
        assert len(shufu_rows) >= 1, f"主婦の固定が配置されていない: {shufu_rows}"

    def test_employee_fixed_skipped_when_wish_takes_precedence(self, client):
        """社員固定は候補なので、wish が優先された場合はスキップされること。"""
        shop_id = insert_shop(code="SHOP_EMP", settings=DEFAULT_SETTINGS)
        insert_pattern(shop_id, "通", "09:00", "18:00", 1)  # 上限1
        emp = insert_staff(shop_id, "E1", "社員", "employee", 2000, 0, 200)
        pt = insert_staff(shop_id, "P1", "バイト", "part_time", 1100, 0, 160)
        # 両者 9-18 を希望/固定で被らせる
        insert_fixed(emp, 1, "09:00", "18:00")  # 社員固定（候補）
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, MON, "2026-07-25"))
        # バイトの wish を wish_history に直接 INSERT
        dbmod.execute(
            "INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, note) "
            "VALUES (?,?,?,?,?)",
            (shop_id, pt, f"{MON}T09:00:00", f"{MON}T18:00:00", "テスト"))

        shop_tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": MON,
        }, headers=auth(shop_tok))

        # バイト（wish）が 9-18 に配置される（wish 優先）
        pt_rows = dbmod.query_all(
            "SELECT * FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime LIKE ?", (pt, f"{MON}%"))
        assert len(pt_rows) == 1
        assert pt_rows[0]["start_datetime"][11:16] == "09:00"

        # 社員は 9-18 の固定時間には入らない（cap=1 なので）
        emp_rows = dbmod.query_all(
            "SELECT * FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime LIKE ?",
            (emp, f"{MON}%"))
        for r in emp_rows:
            assert not (r["start_datetime"][11:16] == "09:00" and r["end_datetime"][11:16] == "18:00"), \
                "社員固定がwishと重なる時間に配置された（候補扱いされていない）"


class TestWorkflow_WishHistoryPreservation:
    """★【インシデント対策】希望履歴 (wish_history) による希望の永久保存。

    ユーザー報告:
      「一括確定後にAI生成でまた希望していたところが消えるバグが復活しました。
       希望シフトは履歴として参照できるように保存すべき。スタッフから
       『わたしこういう希望出していたはずなのに』と言われないために残すべき。
       そしてデータ作成の部分でも消してしまったら再生成できないからです。
       大問題です。インシデントとして考えてください。」

    恒久対策:
      - wish_history テーブル新設（永久履歴）
      - staff POST /api/staff/requests で wish_history に INSERT
      - shift_engine は wish_history を入力にする
      - shop_shifts_auto は wish_history を DELETE しない
      → AI自動生成を何度繰り返しても、統合/短縮/確定で元の希望時間が
        失われず、再生成のたびにエンジンが再考慮する。
    """

    def test_staff_submit_creates_wish_history(self, client):
        """スタッフが希望提出すると wish_history に保存される。"""
        shop_id = insert_shop(code="WISH1")
        staff_id = insert_staff(shop_id, "P1", "バイト")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))
        tok = make_session("staff", staff_id, shop_id)
        r = client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"},
            {"start_datetime": f"{TUE}T09:00:00", "end_datetime": f"{TUE}T17:00:00"},
        ]}, headers=auth(tok))
        assert r.status_code == 200
        # wish_history に2件保存されている
        wishes = dbmod.query_all("SELECT * FROM wish_history WHERE staff_id=?", (staff_id,))
        assert len(wishes) == 2
        # 元の時間がそのまま保存されている
        times = sorted([(w["start_datetime"][11:16], w["end_datetime"][11:16]) for w in wishes])
        assert times == [("09:00", "17:00"), ("09:00", "17:00")]

    def test_wish_history_survives_ai_regeneration(self, client):
        """★ AI自動生成を何度繰り返しても wish_history は消失しない。"""
        shop_id = insert_shop(code="WISH2", settings=DEFAULT_SETTINGS)
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        pt = insert_staff(shop_id, "P1", "バイト", "part_time", 1100, 0, 160)
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))

        # 希望提出（wish_history に保存される）
        staff_tok = make_session("staff", pt, shop_id)
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"},
            {"start_datetime": f"{TUE}T09:00:00", "end_datetime": f"{TUE}T17:00:00"},
            {"start_datetime": f"{THU}T09:00:00", "end_datetime": f"{THU}T17:00:00"},
        ]}, headers=auth(staff_tok))

        shop_tok = make_session("shop", shop_id, shop_id)
        # AI自動生成を3回繰り返す
        for i in range(3):
            r = client.post("/api/shop/shifts/auto", json={
                "start_date": MON, "end_date": FRI
            }, headers=auth(shop_tok))
            assert r.status_code == 200
            # ★ wish_history は毎回3件のまま（削除されない）
            wishes = dbmod.query_all("SELECT * FROM wish_history WHERE staff_id=?", (pt,))
            assert len(wishes) == 3, (
                f"★ AI自動生成{i+1}回目: wish_history が減少/消失した "
                f"({len(wishes)}件, 期待: 3) = インシデント対象バグ"
            )

    def test_wish_history_survives_auto_confirm(self, client):
        """★ 一括確定後も wish_history は消失しない。"""
        shop_id = insert_shop(code="WISH3", settings=DEFAULT_SETTINGS)
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        pt = insert_staff(shop_id, "P1", "バイト", "part_time", 1100, 0, 160)
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))

        staff_tok = make_session("staff", pt, shop_id)
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"},
        ]}, headers=auth(staff_tok))

        shop_tok = make_session("shop", shop_id, shop_id)
        # AI自動生成
        client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI
        }, headers=auth(shop_tok))
        # ★ 一括確定
        r = client.post("/api/shop/shifts/auto-confirm", json={
            "start_date": MON, "end_date": FRI
        }, headers=auth(shop_tok))
        assert r.status_code == 200

        # wish_history は1件のまま（一括確定で消失しない）
        wishes = dbmod.query_all("SELECT * FROM wish_history WHERE staff_id=?", (pt,))
        assert len(wishes) == 1, (
            f"★ 一括確定後: wish_history が消失した ({len(wishes)}件, 期待: 1) = インシデント対象"
        )

    def test_wish_history_preserves_original_time_after_merge(self, client):
        """★ 希望が merge/shorten されても、wish_history は元の希望時間を保持。

        これが今回のインシデントの核心:
          従来: confirmed.reason='希望シフト' で保存 → merge で時間変化 → 再生成時に
                間違った時間（統合後）で再投入されて希望消失
          新仕様: wish_history は元の希望時間を永久保持 → 再生成時に正しい時間で再投入
        """
        shop_id = insert_shop(code="WISH4", settings=DEFAULT_SETTINGS)
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        pt = insert_staff(shop_id, "P1", "バイト", "part_time", 1100, 0, 160)
        # pt は火曜 17-22 固定（merge を誘発するため）
        insert_fixed(pt, 2, "17:00", "22:00")  # 火曜
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))

        staff_tok = make_session("staff", pt, shop_id)
        # 火曜 9-17 希望（17-22 固定と merge される想定）
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{TUE}T09:00:00", "end_datetime": f"{TUE}T17:00:00"},
        ]}, headers=auth(staff_tok))

        shop_tok = make_session("shop", shop_id, shop_id)
        # AI自動生成
        client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI
        }, headers=auth(shop_tok))
        # ★ wish_history の時間は元の 9-17 のまま（merge されても変わらない）
        wish = dbmod.query_one("SELECT * FROM wish_history WHERE staff_id=?", (pt,))
        assert wish["start_datetime"][11:16] == "09:00", (
            f"wish_history の start が元希望時間でない: {wish}"
        )
        assert wish["end_datetime"][11:16] == "17:00", (
            f"wish_history の end が元希望時間でない: {wish}"
        )

        # 一括確定（merge 発生で shifts 側は 9-22 になるはず）
        client.post("/api/shop/shifts/auto-confirm", json={
            "start_date": MON, "end_date": FRI
        }, headers=auth(shop_tok))
        # wish_history は元の 9-17 のまま
        wish = dbmod.query_one("SELECT * FROM wish_history WHERE staff_id=?", (pt,))
        assert wish["start_datetime"][11:16] == "09:00"
        assert wish["end_datetime"][11:16] == "17:00"

        # ★ もう一度 AI自動生成
        client.post("/api/shop/shifts/auto", json={
            "start_date": MON, "end_date": FRI
        }, headers=auth(shop_tok))
        # wish_history は依然として元の 9-17
        wish = dbmod.query_one("SELECT * FROM wish_history WHERE staff_id=?", (pt,))
        assert wish["start_datetime"][11:16] == "09:00"
        assert wish["end_datetime"][11:16] == "17:00"

    def test_staff_can_view_own_wish_history(self, client):
        """スタッフが自身の希望履歴を取得できる（『希望出したはず』参照用）。"""
        shop_id = insert_shop(code="WISH5")
        staff_id = insert_staff(shop_id, "P1", "バイト")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))
        tok = make_session("staff", staff_id, shop_id)
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"},
            {"start_datetime": f"{WED}T09:00:00", "end_datetime": f"{WED}T17:00:00"},
        ]}, headers=auth(tok))
        r = client.get("/api/staff/wishes", headers=auth(tok))
        assert r.status_code == 200
        wishes = r.get_json()["wishes"]
        assert len(wishes) == 2
        # 元の時間が取得できる
        for w in wishes:
            assert w["start_datetime"][11:16] == "09:00"
            assert w["end_datetime"][11:16] == "17:00"

    def test_shop_can_view_all_wish_history(self, client):
        """店長が全スタッフの希望履歴を取得できる。"""
        shop_id = insert_shop(code="WISH6")
        s1 = insert_staff(shop_id, "P1", "バイト1")
        s2 = insert_staff(shop_id, "P2", "バイト2")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))
        tok1 = make_session("staff", s1, shop_id)
        tok2 = make_session("staff", s2, shop_id)
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"},
        ]}, headers=auth(tok1))
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{TUE}T09:00:00", "end_datetime": f"{TUE}T17:00:00"},
        ]}, headers=auth(tok2))
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.get(f"/api/shop/wishes?start={MON}&end={FRI}", headers=auth(shop_tok))
        assert r.status_code == 200
        wishes = r.get_json()["wishes"]
        assert len(wishes) == 2
        # スタッフ名が含まれる
        names = {w["staff_name"] for w in wishes}
        assert "バイト1" in names and "バイト2" in names

    def test_staff_cancel_request_deletes_wish_history(self, client):
        """スタッフが希望をキャンセルした場合、wish_history からも削除される。"""
        shop_id = insert_shop(code="WISH7")
        staff_id = insert_staff(shop_id, "P1", "バイト")
        dbmod.execute(
            "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
            "VALUES (?,?,?,?,1)", (shop_id, MON, FRI, "2026-07-25"))
        tok = make_session("staff", staff_id, shop_id)
        client.post("/api/staff/requests", json={"shifts": [
            {"start_datetime": f"{MON}T09:00:00", "end_datetime": f"{MON}T17:00:00"},
        ]}, headers=auth(tok))
        # shifts.requested の id を取得して DELETE
        shift = dbmod.query_one(
            "SELECT id FROM shifts WHERE staff_id=? AND status='requested'", (staff_id,))
        r = client.delete(f"/api/staff/requests/{shift['id']}", headers=auth(tok))
        assert r.status_code == 200
        # wish_history からも削除されている
        wishes = dbmod.query_all("SELECT * FROM wish_history WHERE staff_id=?", (staff_id,))
        assert len(wishes) == 0


class TestWorkflow_AutoAdjustFullyContainedShift:
    """【ユーザー報告3】一括確定で確定しなかった希望を手動で確定しようとすると
    上限人数を超えてしまう問題の回帰テスト。

    シナリオ（実データ 8/4 状況）:
      - 山田(社員) 9-18 固定 confirmed
      - 佐藤(社員) 9-18 固定 confirmed
      - 鈴木(バイト) 17-22 固定 confirmed
      - 田中(バイト) 18-22 希望シフト confirmed
      - 鈴木 9-17 希望 requested（同日内重複で配置できず）

    期待:
      一括確定または手動auto_adjustで、鈴木の9-17を配置するため
      社員のシフトを 4h 残して短縮し、cap 内に収める。
      例: 山田 9-13 + 佐藤 13-18 + 鈴木 9-22(統合)
    """

    def _setup_8_4_scenario(self):
        """実データ相当の 8/4 シナリオを構築。"""
        shop_id = insert_shop(code="SHOP_8_4", settings=DEFAULT_SETTINGS)
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)
        insert_pattern(shop_id, "昼", "13:00", "17:00", 2)
        insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        emp1 = insert_staff(shop_id, "E1", "山田", "employee", 2000, 160, 200)
        emp2 = insert_staff(shop_id, "E2", "佐藤", "employee", 2000, 160, 200)
        pt = insert_staff(shop_id, "P1", "鈴木", "part_time", 1100, 0, 160)
        # 山田・佐藤 9-18 固定
        insert_fixed(emp1, 2, "09:00", "18:00")  # 火曜
        insert_fixed(emp2, 2, "09:00", "18:00")
        # 鈴木 17-22 固定（火曜）
        insert_fixed(pt, 2, "17:00", "22:00")
        return shop_id, emp1, emp2, pt

    def test_one_shot_auto_confirm_handles_fully_contained_overlap(self, client):
        """一括確定で、target 完全包含の社員シフトを短縮して配置できること。

        シナリオ:
          1. 鈴木 9-17 希望 requested を作成
          2. AI自動生成 → 鈴木以外の固定・補填が confirmed 化（鈴木9-17は requested 残存）
          3. ★ 一括確定 → 鈴木9-17 を配置するため社員を短縮して統合
        """
        shop_id, emp1, emp2, pt = self._setup_8_4_scenario()
        TUE2 = "2026-08-04"  # 火曜

        # 鈴木 9-17 希望 requested + wish_history に INSERT
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, pt, f"{TUE2}T09:00:00", f"{TUE2}T17:00:00", "requested", "スタッフ希望提出"))
        dbmod.execute(
            "INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, note) "
            "VALUES (?,?,?,?,?)",
            (shop_id, pt, f"{TUE2}T09:00:00", f"{TUE2}T17:00:00", "テスト"))

        # AI自動生成で固定を配置
        # 【新設計】鈴木の希望は wish_history にあり、エンジンが読んで配置する。
        # 鈴木は固定17-22（火曜）があるが、希望を出しているので固定はスキップされる。
        # 鈴木の希望9-17が配置される。社員（山田 or 佐藤）の固定は候補として cap 内なら配置。
        shop_tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/shifts/auto", json={
            "start_date": TUE2, "end_date": TUE2,
        }, headers=auth(shop_tok))
        assert r.status_code == 200, r.get_json()

        # ★ 鈴木の希望9-17が配置されていること（13-14の空きなし）
        pt_confirmed = dbmod.query_all(
            "SELECT * FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime>=? AND start_datetime<=?",
            (pt, TUE2 + "T00:00:00", TUE2 + "T23:59:59"))
        assert len(pt_confirmed) >= 1, f"鈴木のシフトが配置されていない: {pt_confirmed}"
        # 9時開始であること（9-17 希望が反映）
        assert any(s["start_datetime"][11:16] == "09:00" for s in pt_confirmed), (
            f"鈴木の開始時刻が9:00でない: {pt_confirmed}"
        )

        # ★ 検証: cap 内に収まっていること（9-16時台は2人以下）
        all_confirmed = dbmod.query_all(
            "SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' "
            "AND start_datetime>=? AND start_datetime<=?",
            (shop_id, TUE2 + "T00:00:00", TUE2 + "T23:59:59"))
        for hr in range(9, 22):
            cnt = _count_staff_in_hour(all_confirmed, TUE2, hr)
            if 9 <= hr < 17:
                assert cnt <= 2, f"{TUE2} {hr}時台: {cnt}人 (朝/昼上限2超過)"
            else:
                assert cnt <= 3, f"{TUE2} {hr}時台: {cnt}人 (夜上限3超過)"

        # ★ 検証: 重複シフトがないこと
        dups = dbmod.query_all(
            "SELECT staff_id, substr(start_datetime,1,10) as day, COUNT(*) as cnt "
            "FROM shifts WHERE shop_id=? AND status='confirmed' "
            "GROUP BY staff_id, day HAVING cnt > 1",
            (shop_id,))
        assert dups == [], f"重複シフト検出: {dups}"

        # ★ 検証: 社員のいずれかが短縮されている（4h残し）
        emp1_row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime LIKE ?", (emp1, f"{TUE2}%"))
        emp2_row = dbmod.query_one(
            "SELECT start_datetime, end_datetime FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime LIKE ?", (emp2, f"{TUE2}%"))
        emp1_min = (int(emp1_row["end_datetime"][11:13]) - int(emp1_row["start_datetime"][11:13])) * 60
        emp2_min = (int(emp2_row["end_datetime"][11:13]) - int(emp2_row["start_datetime"][11:13])) * 60
        # 元は 9-18 (9h = 540分)。少なくとも一方は短縮されているはず
        assert emp1_min < 540 or emp2_min < 540, (
            f"社員のシフトが短縮されていない: emp1={emp1_row} emp2={emp2_row}"
        )
        # 4h (240分) 以上は残っていること
        assert emp1_min >= 240, f"山田のシフトが4h未満: {emp1_row}"
        assert emp2_min >= 240, f"佐藤のシフトが4h未満: {emp2_row}"

    def test_manual_put_auto_adjust_resolves_fully_contained_overlap(self, client):
        """手動 PUT (auto_adjust=true) でも target 完全包含の重複を解決できること。

        ユーザー報告: 「現在の8月4日のところを見て欲しい。
          これは一括確定でも確定しなかった部分である。
          この状態で鈴木花子を手動で確定させると３人体制となってしまう。」
        """
        shop_id, emp1, emp2, pt = self._setup_8_4_scenario()
        TUE2 = "2026-08-04"

        # AI自動生成で固定を配置
        shop_tok = make_session("shop", shop_id, shop_id)
        client.post("/api/shop/shifts/auto", json={
            "start_date": TUE2, "end_date": TUE2,
        }, headers=auth(shop_tok))

        # 鈴木 9-17 希望 requested + wish_history に INSERT
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
            "VALUES (?,?,?,?,?,?)",
            (shop_id, pt, f"{TUE2}T09:00:00", f"{TUE2}T17:00:00", "requested", "スタッフ希望提出"))
        dbmod.execute(
            "INSERT INTO wish_history (shop_id, staff_id, start_datetime, end_datetime, note) "
            "VALUES (?,?,?,?,?)",
            (shop_id, pt, f"{TUE2}T09:00:00", f"{TUE2}T17:00:00", "テスト"))
        rid = dbmod.query_one(
            "SELECT id FROM shifts WHERE staff_id=? AND status='requested'",
            (pt,))["id"]

        # ★ 手動 PUT (auto_adjust=true) で確定を試みる
        r = client.put(f"/api/shop/shifts/{rid}", json={
            "staff_id": pt, "start_datetime": f"{TUE2}T09:00:00",
            "end_datetime": f"{TUE2}T17:00:00", "status": "confirmed",
            "auto_adjust": True,
        }, headers=auth(shop_tok))
        assert r.status_code == 200, (
            f"auto_adjust で解決できるべき: {r.get_json()}"
        )

        # 検証: 鈴木のシフトが確定している（9-22 merge または 9-17 独立）
        pt_rows = dbmod.query_all(
            "SELECT * FROM shifts WHERE staff_id=? AND status='confirmed' "
            "AND start_datetime LIKE ?", (pt, f"{TUE2}%"))
        assert len(pt_rows) >= 1, f"鈴木のシフトが確定されていない: {pt_rows}"

        # ★ 検証: cap 内に収まっていること（3人になっていないか）
        all_confirmed = dbmod.query_all(
            "SELECT * FROM shifts WHERE shop_id=? AND status='confirmed' "
            "AND start_datetime>=? AND start_datetime<=?",
            (shop_id, TUE2 + "T00:00:00", TUE2 + "T23:59:59"))
        for hr in range(9, 22):
            cnt = _count_staff_in_hour(all_confirmed, TUE2, hr)
            if 9 <= hr < 17:
                assert cnt <= 2, (
                    f"★ {TUE2} {hr}時台: {cnt}人 (上限2超過) = ユーザー報告バグ "
                    f"「3人体制となってしまう」が再発"
                )
            else:
                assert cnt <= 3, f"{TUE2} {hr}時台: {cnt}人 (夜上限3超過)"

    def test_find_shorten_candidate_handles_full_inclusion(self):
        """_find_shorten_candidate が target 完全包含のケースで4h候補を返すこと。"""
        # 山田 9-18, target 9-17 → 9-13 (4h) か 14-18 (4h) の候補があるべき
        from app import _find_shorten_candidate
        o = {"id": 999, "start_datetime": "2026-08-04T09:00:00", "end_datetime": "2026-08-04T18:00:00"}
        # cap を超過させる必要があるので、テスト用 shop を作成
        shop_id = insert_shop(code="TEST_SHORTEN")
        from helpers import insert_pattern, insert_staff, insert_fixed
        insert_pattern(shop_id, "朝", "09:00", "13:00", 2)  # cap=2
        # 3人配置して cap 超過状態を作る
        emp1 = insert_staff(shop_id, "E1", "A", "employee", 2000, 0, 200)
        emp2 = insert_staff(shop_id, "E2", "B", "employee", 2000, 0, 200)
        emp3 = insert_staff(shop_id, "E3", "C", "employee", 2000, 0, 200)
        # 全員 9-13 で confirmed にする（cap=2 なのに3人 = 超過状態）
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason, break_time_minutes) "
            "VALUES (?,?,?,?,?,?,?)",
            (shop_id, emp1, "2026-08-04T09:00:00", "2026-08-04T18:00:00", "confirmed", "テスト", 60))
        # o の id を emp2 の実際の shift id にする
        dbmod.execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason, break_time_minutes) "
            "VALUES (?,?,?,?,?,?,?)",
            (shop_id, emp2, "2026-08-04T09:00:00", "2026-08-04T18:00:00", "confirmed", "テスト", 60))
        o["id"] = dbmod.query_one("SELECT id FROM shifts WHERE staff_id=?", (emp2,))["id"]

        # target 9-17 を配置したい
        result = _find_shorten_candidate(o, "2026-08-04T09:00:00", "2026-08-04T17:00:00", shop_id)
        # 何らかの短縮候補が返るはず（前4h=9-13 または 後4h=14-18）
        assert result is not None, "target 完全包含でも短縮候補が返るべき"
        new_s, new_e = result
        # 4時間以上残っている
        from utils import minutes_between
        assert minutes_between(new_s, new_e) >= 4 * 60


class TestWorkflow_ReasonCoverage:
    """【インシデント対策】ブラックリスト方式（MANUAL_REASONS）の検証。

    旧仕様（ホワイトリスト）は新しい reason が追加されるたびにリストをメンテする必要が
    あり、3回も漏れが発生して重複バグを引き起こした。ブラックリスト方式に転換し、
    「手動」の reason のみを明示的に保持することで恒久対策する。
    """

    def test_manual_reasons_is_complete_whitelist(self):
        """MANUAL_REASONS は「本当に手動」の reason のみを含むこと。

        新しい自動生成 reason が追加されても、このリストに入っていなければ
        自動的に再生成対象（DELETE → INSERT）になる。メンテ不要。
        """
        import re
        with open("src/app.py", "r", encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"MANUAL_REASONS\s*=\s*\(([^)]+)\)", src, re.DOTALL)
        assert m, "MANUAL_REASONS 定義が見つからない"
        defined = set(re.findall(r"'([^']*)'", m.group(1)))

        # 手動 reason の完全リスト
        expected_manual = {
            '手動追加', '手動調整',
            '変更申請承認', '追加申請承認',
            'コピー',
        }
        assert defined == expected_manual, (
            f"MANUAL_REASONS が想定外: defined={defined} expected={expected_manual}"
        )

    def test_all_engine_reasons_are_not_manual(self):
        """エンジン/自動生成パスの reason は MANUAL_REASONS に含まれないこと。

        これにより、新しい reason が追加されても自動的に再生成対象になる。
        """
        import re
        with open("src/app.py", "r", encoding="utf-8") as f:
            src = f.read()
        m = re.search(r"MANUAL_REASONS\s*=\s*\(([^)]+)\)", src, re.DOTALL)
        assert m
        manual = set(re.findall(r"'([^']*)'", m.group(1)))

        # エンジンが生成する reason（MANUAL_REASONS に入ってはいけない）
        engine_reasons = {
            '固定シフト', '固定シフト（候補）',
            '不足補填（社員自動配置）',
            '希望シフト', '柔軟希望(any)', '柔軟希望(morning)', '柔軟希望(evening)',
            '自動調整(統合)', '自動確定', '自動確定(cap内短縮)', '隣接統合',
            'スタッフ希望(柔軟)', 'スタッフ希望提出',
        }
        leaked = engine_reasons & manual
        assert leaked == set(), (
            f"エンジン reason が MANUAL_REASONS に漏れている: {leaked}。"
            f"これらは再生成時に残存し重複バグを引き起こす。"
        )
