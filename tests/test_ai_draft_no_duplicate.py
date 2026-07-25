"""tests/test_ai_draft_no_duplicate.py

回帰: ドラフト生成後に、スタッフ希望の元 requested 行と AIドラフト配置が
二重に残って過剰配置に見えないこと（必要人数を超えない）。

再現していたバグ:
  ドラフト保存時、スタッフ希望(status='requested', reason='スタッフ希望提出')を
  保持したまま、エンジンが同じ希望を 'AIドラフト: 希望シフト' として再配置するため、
  同一スタッフ・同一時間が2件になり、タイムラインで過剰配置に見えていた。
  （希望表管理は wish_history を参照するので、元 requested 行は保持不要。）
"""
import db as dbmod
from helpers import insert_shop, insert_staff, insert_pattern, insert_wish, make_session, auth

DAY = "2026-08-01"
SETTINGS = {"min_daily_hours": 4, "max_consecutive_days": 6, "default_hourly_wage": 1100}


def _submit_wish(shop_id, staff_id, st, en):
    """実運用と同様に wish_history と shifts.requested の両方へ登録。"""
    insert_wish(shop_id, staff_id, DAY, st, en)
    dbmod.execute(
        "INSERT INTO shifts (shop_id, staff_id, start_datetime, end_datetime, status, reason) "
        "VALUES (?,?,?,?,?,?)",
        (shop_id, staff_id, f"{DAY}T{st}:00", f"{DAY}T{en}:00", "requested", "スタッフ希望提出"))


def test_draft_generation_has_no_duplicate_wish_rows(client):
    shop_id = insert_shop(settings=SETTINGS)
    insert_pattern(shop_id, "午前", "09:00", "12:00", 1)  # 必要1
    pt = insert_staff(shop_id, "P1", "バイト太郎", "part_time", 1200)
    insert_staff(shop_id, "E1", "社員花子", "employee", 2000, 0, 200)
    _submit_wish(shop_id, pt, "09:00", "11:00")  # 2h（min_daily未満でも尊重される）

    tok = make_session("shop", shop_id, shop_id)
    r = client.post("/api/shop/shifts/auto",
                    json={"start_date": DAY, "end_date": DAY, "draft": True}, headers=auth(tok))
    assert r.status_code == 200, r.get_json()

    rows = dbmod.query_all(
        "SELECT staff_id, start_datetime, end_datetime, status, reason FROM shifts "
        "WHERE shop_id=? AND start_datetime>=? AND start_datetime<=?",
        (shop_id, DAY + "T00:00:00", DAY + "T23:59:59"))

    # バイト太郎の 09:00-11:00 は1件だけ（元希望とドラフトの二重が無い）
    pt_0911 = [r for r in rows if r["staff_id"] == pt
               and r["start_datetime"].endswith("T09:00:00")
               and r["end_datetime"].endswith("T11:00:00")]
    assert len(pt_0911) == 1, f"希望が二重に残っている: {pt_0911}"

    # スロット被り（必要人数1を超える配置）が無い
    cov = {}
    for r in rows:
        sh = int(r["start_datetime"][11:13])
        eh = int(r["end_datetime"][11:13])
        for h in range(sh, eh):
            cov[h] = cov.get(h, 0) + 1
    over = {h: c for h, c in cov.items() if c > 1}
    assert not over, f"必要人数1を超える過剰配置がある: {over} / rows={rows}"
