"""管理者自身の通知ベルが /api/admin/notifications の新契約で壊れないことを守る。

Task 14 のレビューで、GET /api/admin/notifications の契約変更
（{"notifications": [...], "unread": ...} → {"announcements": [...]}）により、
管理者自身が代理閲覧をしていない状態でヘッダの通知ベルを開くと
`d.notifications.length` が undefined 参照で例外になる回帰が指摘された。

サーバ側は新契約が固定されていること、フロント（public/app.js）は
`role === 'admin'` の分岐で新契約を正しく扱い、旧契約前提のキーに
触れていないことを、それぞれ機械的に検査する。
"""
from pathlib import Path

from helpers import insert_admin

ROOT = Path(__file__).resolve().parents[1]


def _admin_token(client):
    insert_admin("admin", "Admin123")
    r = client.post("/api/login", json={"user_code": "admin", "password": "Admin123"})
    return r.get_json()["token"]


class TestAdminNotificationsContract:
    def test_response_has_only_new_contract_keys(self, client):
        """announcements のみを返し、Phase1 の空スタブ契約（notifications/unread）
        が復活していないこと。"""
        t = _admin_token(client)
        r = client.get("/api/admin/notifications",
                        headers={"Authorization": f"Bearer {t}"})
        assert r.status_code == 200
        data = r.get_json()
        assert "announcements" in data
        assert "notifications" not in data
        assert "unread" not in data


class TestAdminBellFrontendGuard:
    def test_refresh_notif_badge_short_circuits_for_admin_without_calling_api(self):
        """admin 分岐は d.unread に触れる前（=api() を呼ぶ前）に return する。"""
        source = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        start = source.index("async function refreshNotifBadge")
        end = source.index("\nfunction openNotifications", start)
        block = source[start:end]
        assert "role === 'admin'" in block

        admin_branch_start = block.index("if (role === 'admin')")
        admin_branch_end = block.index("return;", admin_branch_start) + len("return;")
        admin_branch = block[admin_branch_start:admin_branch_end]
        # 管理者分岐では api() を呼ばない（=unread という概念自体に触れない）
        assert "api(" not in admin_branch
        # 分岐より後ろ（店舗/スタッフ用の経路）には従来どおり d.unread が残る
        assert "d.unread" in block[admin_branch_end:]

    def test_open_notifications_uses_announcements_key_for_admin(self):
        """admin 分岐は d.announcements を使い、旧契約の d.notifications には
        一切触れない（触れると必ず例外になるため）。"""
        source = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
        start = source.index("function openNotifications")
        end = source.index("\nconst NAV_DEFS", start)
        block = source[start:end]
        assert "role === 'admin'" in block
        assert "d.announcements" in block

        admin_branch_start = block.index("if (role === 'admin')")
        admin_branch_end = block.index("return;", admin_branch_start) + len("return;")
        admin_branch = block[admin_branch_start:admin_branch_end]
        assert "d.notifications" not in admin_branch
        # 分岐より後ろ（店舗/スタッフ用の経路）には従来どおり d.notifications が残る
        assert "d.notifications" in block[admin_branch_end:]
