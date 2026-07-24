"""tests/test_e2e_flow_integration.py - クロス機能のE2E結合テスト。

HTTP API 経由で以下の新テーマを一気通貫で検証する（内部関数は直接呼ばない）:
  1. 店舗/パターン/スタッフA・B/募集期間のセットアップ
  2. AIドラフト（over capacity）を A・B に投入
  3. 店長の一括確定 → over_cap==2 / over_cap_flag=1 / audit(shift.finalize)
  4. 店長メモ（note）の設定と永続化
  5. スタッフ視点のプライバシー（over_cap_flag/note 非公開・reason に超過情報なし）
  6. スタッフの変更申請 → 店長承認（時間更新・通知・audit creq.approve）
  7. 2件目の変更申請を却下（通知・audit creq.reject）
  8. 管理者によるスタッフ編集（audit staff.update）
  9. 管理者の監査ログ一覧と ?action= フィルタ
"""
import pytest

import db as dbmod
from helpers import (
    insert_admin, insert_shop, insert_staff, insert_pattern,
    make_session, auth,
)

DAY = "2026-08-03"  # 月曜
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


def _insert_period(shop_id, start="2026-08-01", end="2026-08-31", deadline="2099-12-31"):
    dbmod.execute(
        "INSERT INTO shift_request_periods (shop_id, start_date, end_date, deadline, is_active) "
        "VALUES (?,?,?,?,1)",
        (shop_id, start, end, deadline))


def _insert_draft(shop_id, staff_id, reason):
    return dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{DAY}T09:00:00", f"{DAY}T18:00:00", "requested", reason),
    )["last_row_id"]


class TestE2EFlow:
    """フル・クロス機能フロー。"""

    def test_full_cross_feature_flow(self, client):
        # ---------- Step 1: セットアップ ----------
        admin_id = insert_admin()
        admin_tok = make_session("admin", admin_id)

        shop_id = insert_shop(settings=SETTINGS)
        insert_pattern(shop_id, "通し", "09:00", "18:00", 1)  # 必要人数 1
        staff_a = insert_staff(shop_id, "A001", "スタッフA", "part_time", 1100)
        staff_b = insert_staff(shop_id, "B001", "スタッフB", "part_time", 1200)
        _insert_period(shop_id)

        shop_tok = make_session("shop", shop_id, shop_id)
        shop_hdr = auth(shop_tok)

        # ---------- Step 2: AIドラフト（over capacity）を A・B に投入 ----------
        _insert_draft(shop_id, staff_a, "AIドラフト: 希望シフト")
        _insert_draft(shop_id, staff_b, "AIドラフト: 不足補填")

        # ---------- Step 3: 店長が一括確定 ----------
        r = client.post("/api/shop/shifts/finalize",
                        json={"start_date": DAY, "end_date": DAY}, headers=shop_hdr)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body["finalized"] == 2, f"finalized should be 2, got {body}"
        assert body["over_cap"] == 2, (
            f"必要人数1に対しA・B2名配置 → over_cap は 2 のはず, got {body['over_cap']}")

        # 両シフトが confirmed + over_cap_flag=1
        conf = dbmod.query_all(
            "SELECT id, staff_id, status, over_cap_flag, reason FROM shifts "
            "WHERE shop_id=? ORDER BY id", (shop_id,))
        assert len(conf) == 2, f"expected 2 shifts, got {len(conf)}"
        for row in conf:
            assert row["status"] == "confirmed", f"shift {row['id']} should be confirmed"
            assert row["over_cap_flag"] == 1, (
                f"shift {row['id']} should be over_cap_flag=1, got {row['over_cap_flag']}")

        # audit_logs に shift.finalize が記録されている
        fin_logs = dbmod.query_all(
            "SELECT * FROM audit_logs WHERE action='shift.finalize' AND shop_id=?", (shop_id,))
        assert len(fin_logs) >= 1, "audit_logs に action='shift.finalize' が必要"

        # スタッフAの確定シフトIDを保持（後続ステップで使用）
        shift_a = next(row for row in conf if row["staff_id"] == staff_a)
        shift_a_id = shift_a["id"]

        # ---------- Step 4: 店長メモ（note）を設定 ----------
        r = client.patch(f"/api/shop/shifts/{shift_a_id}/note",
                         json={"note": "遅刻注意"}, headers=shop_hdr)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["note"] == "遅刻注意"
        db_note = dbmod.query_one(
            "SELECT note FROM shifts WHERE id=?", (shift_a_id,))["note"]
        assert db_note == "遅刻注意", f"note が永続化されていない: {db_note!r}"

        # ---------- Step 5: プライバシー検証（スタッフA視点） ----------
        staff_a_tok = make_session("staff", staff_a, shop_id)
        staff_a_hdr = auth(staff_a_tok)
        r = client.get(f"/api/staff/shifts?start={DAY}&end={DAY}", headers=staff_a_hdr)
        assert r.status_code == 200, r.get_json()
        staff_shifts = r.get_json()["shifts"]
        assert len(staff_shifts) >= 1, "スタッフAは自分の確定シフトを見られる"
        for s in staff_shifts:
            assert "over_cap_flag" not in s, (
                f"スタッフAPIに over_cap_flag が漏洩している: keys={list(s.keys())}")
            assert "note" not in s, (
                f"スタッフAPIに note が漏洩している: keys={list(s.keys())}")
            assert "必要人数超過" not in (s.get("reason") or ""), (
                f"reason に超過情報が漏洩している: {s.get('reason')!r}")

        # ---------- Step 6: スタッフAの変更申請 → 店長が承認 ----------
        new_start = f"{DAY}T10:00:00"
        new_end = f"{DAY}T19:00:00"
        r = client.post("/api/staff/change-requests", json={
            "request_type": "change", "shift_id": shift_a_id,
            "desired_start": new_start, "desired_end": new_end,
            "reason": "開始を遅らせたい",
        }, headers=staff_a_hdr)
        assert r.status_code == 200, r.get_json()

        crid_approve = dbmod.query_one(
            "SELECT id FROM change_requests WHERE staff_id=? ORDER BY id DESC LIMIT 1",
            (staff_a,))["id"]

        r = client.put(f"/api/shop/change-requests/{crid_approve}",
                       json={"action": "approve"}, headers=shop_hdr)
        assert r.status_code == 200, r.get_json()
        assert r.get_json().get("ok") is True

        # シフト時間が更新されている
        updated = dbmod.query_one(
            "SELECT start_datetime, end_datetime, status FROM shifts WHERE id=?", (shift_a_id,))
        assert updated["start_datetime"] == new_start, (
            f"承認後の開始時刻が更新されていない: {updated['start_datetime']}")
        assert updated["end_datetime"] == new_end, (
            f"承認後の終了時刻が更新されていない: {updated['end_datetime']}")
        assert updated["status"] == "confirmed"

        # スタッフAへ承認通知
        approve_notifs = dbmod.query_all(
            "SELECT * FROM notifications WHERE shop_id=? AND staff_id=? AND title LIKE '%承認%'",
            (shop_id, staff_a))
        assert len(approve_notifs) >= 1, "承認通知がスタッフAに届いていない"

        # audit_logs に creq.approve
        approve_logs = dbmod.query_all(
            "SELECT * FROM audit_logs WHERE action='creq.approve' AND shop_id=?", (shop_id,))
        assert len(approve_logs) >= 1, "audit_logs に action='creq.approve' が必要"

        # ---------- Step 7: 2件目の変更申請を却下 ----------
        r = client.post("/api/staff/change-requests", json={
            "request_type": "change", "shift_id": shift_a_id,
            "desired_start": f"{DAY}T11:00:00", "desired_end": f"{DAY}T20:00:00",
            "reason": "やっぱりもっと遅く",
        }, headers=staff_a_hdr)
        assert r.status_code == 200, r.get_json()
        crid_reject = dbmod.query_one(
            "SELECT id FROM change_requests WHERE staff_id=? ORDER BY id DESC LIMIT 1",
            (staff_a,))["id"]

        r = client.put(f"/api/shop/change-requests/{crid_reject}",
                       json={"action": "reject"}, headers=shop_hdr)
        assert r.status_code == 200, r.get_json()

        reject_row = dbmod.query_one(
            "SELECT status FROM change_requests WHERE id=?", (crid_reject,))
        assert reject_row["status"] == "rejected", "却下されたのに status が rejected でない"

        reject_notifs = dbmod.query_all(
            "SELECT * FROM notifications WHERE shop_id=? AND staff_id=? AND title LIKE '%却下%'",
            (shop_id, staff_a))
        assert len(reject_notifs) >= 1, "却下通知がスタッフAに届いていない"

        reject_logs = dbmod.query_all(
            "SELECT * FROM audit_logs WHERE action='creq.reject' AND shop_id=?", (shop_id,))
        assert len(reject_logs) >= 1, "audit_logs に action='creq.reject' が必要"

        # ---------- Step 8: 管理者がスタッフBを編集 ----------
        r = client.put(f"/api/admin/shops/{shop_id}/staffs/{staff_b}", json={
            "name": "スタッフB改", "hourly_wage": 1500, "is_resigned": True,
        }, headers=auth(admin_tok))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()["updated"] >= 1

        b_row = dbmod.query_one(
            "SELECT name, hourly_wage, is_resigned FROM staffs WHERE id=?", (staff_b,))
        assert b_row["name"] == "スタッフB改", f"name 未更新: {b_row['name']}"
        assert b_row["hourly_wage"] == 1500, f"wage 未更新: {b_row['hourly_wage']}"
        assert b_row["is_resigned"] == 1, f"is_resigned 未更新: {b_row['is_resigned']}"

        staff_update_logs = dbmod.query_all(
            "SELECT * FROM audit_logs WHERE action='staff.update' AND shop_id=?", (shop_id,))
        assert len(staff_update_logs) >= 1, "audit_logs に action='staff.update' が必要"

        # ---------- Step 9: 管理者が監査ログを一覧・フィルタ ----------
        r = client.get("/api/admin/audit-logs", headers=auth(admin_tok))
        assert r.status_code == 200, r.get_json()
        all_actions = {log["action"] for log in r.get_json()["logs"]}
        for expected in ("shift.finalize", "creq.approve", "creq.reject", "staff.update"):
            assert expected in all_actions, (
                f"監査ログ一覧に {expected} が含まれるべき: {all_actions}")

        # ?action= フィルタは一致するものだけを返す
        r = client.get("/api/admin/audit-logs?action=shift.finalize", headers=auth(admin_tok))
        assert r.status_code == 200, r.get_json()
        filtered = r.get_json()["logs"]
        assert len(filtered) >= 1, "フィルタ結果が空"
        assert all(log["action"] == "shift.finalize" for log in filtered), (
            f"?action= フィルタが効いていない: {[l['action'] for l in filtered]}")
