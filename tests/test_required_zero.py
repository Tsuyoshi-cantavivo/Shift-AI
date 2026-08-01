"""tests/test_required_zero.py — 必要人数0の意味を実装と説明で一致させる。

実行: ./.venv/bin/python -m pytest tests/test_required_zero.py -v

背景: UI は「0 を入れるとその曜日は募集しません」と説明しているが、
実装では required <= 0 のスロットが「上限なし」として扱われ、
何人でも配置できてしまっていた（意味が正反対）。

このファイルは req_map のキーの有無と値0を区別する契約を固定する。
  - キーが無い    = パターン圏外 = 上限なし
  - 値が 0        = 明示的に0人 = 配置禁止
  - 値が 1 以上   = その人数が上限かつ必要人数
"""
import pytest

import shift_engine
from helpers import insert_shop, insert_staff, insert_pattern, insert_request


class TestDayRequirementsZero:
    """_day_requirements が「0」と「圏外」を区別すること。"""

    def test_zero_pattern_writes_key_with_value_zero(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 0}]
        req = shift_engine._day_requirements(pats)
        # 09:00-12:00 のスロットにキーがあり、値が 0 であること
        assert req.get(540) == 0, "明示的に0人のスロットにキーが無い（圏外と区別できない）"
        assert req.get(690) == 0

    def test_out_of_pattern_slot_has_no_key(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 2}]
        req = shift_engine._day_requirements(pats)
        # 08:30 はパターン圏外なのでキーが無いこと
        assert 510 not in req

    def test_overlapping_zero_does_not_lower_positive(self):
        """0のパターンが重なっても、正の要件を0に引き下げないこと。"""
        pats = [
            {"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 3},
            {"id": 2, "start_time": "10:00", "end_time": "11:00", "required_staff": 0},
        ]
        req = shift_engine._day_requirements(pats)
        assert req.get(600) == 3, "重なりの集約は max のはず"

    def test_weekday_override_zero_is_respected(self):
        pats = [{"id": 1, "start_time": "09:00", "end_time": "12:00", "required_staff": 3}]
        # 土曜(6)だけ 0 人
        req = shift_engine._day_requirements(pats, weekday=6, weekday_overrides={(1, 6): 0})
        assert req.get(540) == 0, "曜日別の0が反映されていない"

    def test_overnight_zero_extends_to_next_day_slots(self):
        """日またぎパターンの0も拡張スロットに書かれること。"""
        pats = [{"id": 1, "start_time": "22:00", "end_time": "02:00", "required_staff": 0}]
        req = shift_engine._day_requirements(pats)
        assert req.get(1320) == 0        # 22:00
        assert req.get(1500) == 0        # 翌 01:00 = 1440 + 60


class TestCapOkZero:
    """必要人数0のスロットに配置できないこと（cap_ok の契約）。

    cap_ok は auto_generate 内のクロージャなので直接は呼べない。
    実際にシフト生成を走らせて確認する。

    注意: 元の brief 案（社員のみ登録して auto_generate を走らせ、
    result["shifts"] が空であることを見る）には2つの欠陥があった。
      1. auto_generate の戻り値に "shifts" キーは存在しない
         （"confirmed"/"pending"）。result.get("shifts", []) は
         常に [] を返すため、このテストは何も検証していなかった。
      2. 希望（wish）が無い状態では、0人の枠は _day_shortage_segments
         上「不足0」になり、社員の自動穴埋め配置自体が最初から
         発生しない。つまり cap_ok の分岐（required<=0: continue）を
         一度も通らないまま「配置されない」が成立してしまい、
         cap_ok を壊しても赤くならない（実際に確認済み。レポート参照）。

    cap_ok のバグを実際に踏むのは「本人が明示的に時間指定した希望
    （timed wish）」の配置経路（auto_generate 内 can_place 経由）。
    そこで希望シフトを明示的に挿入し、confirmed に入らないことを見る。
    """

    def test_zero_weekday_blocks_wish_placement(self):
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "P1", "アルバイトA", role="part_time")
        pid = insert_pattern(shop_id, "通し", "09:00", "22:00", 2)
        # 2026-08-01 は土曜。土曜(6)だけ 0 人にする
        from db import execute
        execute("INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                (pid, shop_id, 6, 0))
        # 本人が明示的に時間指定で希望を出す（timed wish、min_daily チェックは
        # 本人希望なので免除される＝cap 以外の理由で弾かれないようにする）
        insert_request(shop_id, staff_id, "2026-08-01", "09:00", "17:00")

        settings = {}
        result = shift_engine.auto_generate(shop_id, settings, "2026-08-01", "2026-08-01")
        confirmed_staff_ids = {c["staff_id"] for c in result.get("confirmed", [])}
        assert staff_id not in confirmed_staff_ids, (
            "0人設定の土曜に希望シフトが confirmed された（上限なし扱いのまま）"
        )


class TestManualAddZero:
    """手動追加でも0人の時間帯には置けないこと。

    _check_slot_cap（src/app.py）は req_map.get(sl, 0) で「パターン圏外」と
    「明示的に0人」を同じ0として扱っており、0人設定の時間帯だけに触れる
    手動追加が「上限なし」と誤判定されて通ってしまっていた。
    """

    def test_manual_add_blocked_on_zero_weekday(self, client):
        from helpers import make_session, auth
        from db import execute
        shop_id = insert_shop()
        staff_id = insert_staff(shop_id, "E1", "社員A", role="employee")
        pid = insert_pattern(shop_id, "通し", "09:00", "22:00", 2)
        execute("INSERT INTO shift_pattern_weekday_required (pattern_id, shop_id, weekday, required_staff) VALUES (?,?,?,?)",
                (pid, shop_id, 6, 0))
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/shifts", json={
            "staff_id": staff_id,
            "start_datetime": "2026-08-01T09:00:00",
            "end_datetime": "2026-08-01T17:00:00",
        }, headers=auth(token))
        assert r.status_code in (400, 409), \
            f"0人設定の土曜に手動追加できてしまった（status={r.status_code}）"


class TestPatternZeroPersistence:
    """基本必要人数 0 が保存され、読み出せること。"""

    def test_post_pattern_with_zero(self, client):
        from helpers import make_session, auth
        shop_id = insert_shop()
        token = make_session("shop", shop_id, shop_id)

        r = client.post("/api/shop/patterns", json={
            "pattern_name": "休止中", "start_time": "09:00", "end_time": "12:00",
            "required_staff": 0,
        }, headers=auth(token))
        assert r.status_code == 200

        d = client.get("/api/shop/patterns", headers=auth(token)).get_json()
        pat = [p for p in d["patterns"] if p["pattern_name"] == "休止中"][0]
        assert pat["required_staff"] == 0, "0 が 1 に丸められている"

    def test_put_pattern_to_zero(self, client):
        from helpers import make_session, auth
        shop_id = insert_shop()
        pid = insert_pattern(shop_id, "夜", "17:00", "22:00", 3)
        token = make_session("shop", shop_id, shop_id)

        r = client.put(f"/api/shop/patterns/{pid}", json={
            "pattern_name": "夜", "start_time": "17:00", "end_time": "22:00",
            "required_staff": 0,
        }, headers=auth(token))
        assert r.status_code == 200

        d = client.get("/api/shop/patterns", headers=auth(token)).get_json()
        assert d["patterns"][0]["required_staff"] == 0
