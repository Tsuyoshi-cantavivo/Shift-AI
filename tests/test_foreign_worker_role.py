"""tests/test_foreign_worker_role.py — 外国籍アルバイトロールと週28時間上限。

実行: ./.venv/bin/python -m pytest tests/test_foreign_worker_role.py -v

設計書: docs/superpowers/specs/2026-08-02-foreign-worker-role-design.md

このロールに付く制約は週28hのみ。学生ロールの月80h上限・学生のみシフト禁止は
重ねない（月80hは週換算18.6hで、重ねると週28hの判定がほぼ発火しなくなるため）。
"""
import pytest

from helpers import insert_shop, insert_staff, make_session, auth


class TestForeignWorkerRoleAccepted:
    def test_create_staff_with_foreign_worker_role(self, client):
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "FW1", "name": "外国籍太郎",
            "password": "Fwk12345", "role": "foreign_worker",
        }, headers=auth(tok))
        assert r.status_code == 200, r.get_data(as_text=True)
        from db import query_one
        row = query_one("SELECT role FROM staffs WHERE id=?", (r.get_json()["id"],))
        assert row["role"] == "foreign_worker"

    def test_foreign_worker_is_not_capped_at_80_hours(self):
        """学生の月80h上限は重ねない（週28hだけが効く）。"""
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "FW2", "外国籍花子", "foreign_worker", 1100, 0, 160)
        from db import query_one
        row = query_one("SELECT max_hours_per_month FROM staffs WHERE id=?", (sid,))
        assert row["max_hours_per_month"] == 160

    def test_unknown_role_is_still_rejected(self, client):
        """新しい値を足しても、でたらめなロールは拒否され続けること。"""
        shop_id = insert_shop()
        tok = make_session("shop", shop_id, shop_id)
        r = client.post("/api/shop/staffs", json={
            "staff_code": "XX1", "name": "不正", "password": "Xxx12345",
            "role": "not_a_role",
        }, headers=auth(tok))
        assert r.status_code == 400


class TestShiftsWeeklyCapAckColumn:
    def test_column_exists_with_default_zero(self):
        """承諾フラグの列があり、既定が0であること。"""
        from db import execute, query_one
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "FW3", "外国籍次郎", "foreign_worker")
        meta = execute(
            "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status) "
            "VALUES (?,?,?,?,'confirmed')",
            (shop_id, sid, "2026-08-10T09:00:00", "2026-08-10T17:00:00"))
        row = query_one("SELECT weekly_cap_ack FROM shifts WHERE id=?", (meta["last_row_id"],))
        assert row["weekly_cap_ack"] == 0
