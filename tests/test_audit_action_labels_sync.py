"""AUDIT_ACTION_LABELS（public/admin.js）が src/*.py の audit() 呼び出しと同期していることを検査する。

Task 13 レビュアー提案: 監査ログ画面（システム管理者コンソール）は AUDIT_ACTION_LABELS に
無いキーだと action コードがそのまま出てしまう（auditActionLabel() のフォールバック）。
バックエンドで新しい audit("...") 呼び出しを追加したのにフロントの辞書更新を忘れる、
または逆にフロントに残骸キーが残る、を機械的に検知する。

先例: tests/test_draft_timeline_assets.py と同じ手法（フロントエンドの JS をテキストとして
読み、正規表現で UI 契約を検査する。ブラウザや Flask アプリの起動は不要）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# audit("action.name", ...) 形式の呼び出しから action 文字列リテラルを抽出する。
# def audit(action, ...) のような定義側（クォートなし）は拾わない。
_AUDIT_CALL_RE = re.compile(r'audit\(\s*["\']([\w.]+)["\']')
# AUDIT_ACTION_LABELS = { 'xxx.yyy': '...', ... } のキーを抽出する。
_LABEL_KEY_RE = re.compile(r"'([\w.]+)':")


def _actions_used_in_backend():
    actions = set()
    for path in sorted((ROOT / "src").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        actions.update(_AUDIT_CALL_RE.findall(text))
    return actions


def _labeled_actions_in_admin_js():
    source = (ROOT / "public" / "admin.js").read_text(encoding="utf-8")
    start = source.index("const AUDIT_ACTION_LABELS = {")
    end = source.index("\n};", start)
    block = source[start:end]
    return set(_LABEL_KEY_RE.findall(block))


def test_audit_action_labels_match_backend_actions():
    backend_actions = _actions_used_in_backend()
    frontend_labels = _labeled_actions_in_admin_js()

    # 抽出そのものが壊れていないことの前提チェック（0件ならテストが無意味になる）
    assert len(backend_actions) >= 20, (
        f"src/*.py から抽出できた audit() アクションが少なすぎます（{len(backend_actions)}件）。"
        "正規表現が壊れている可能性があります。"
    )
    assert len(frontend_labels) >= 20, (
        f"public/admin.js の AUDIT_ACTION_LABELS から抽出できたキーが少なすぎます"
        f"（{len(frontend_labels)}件）。正規表現かブロック抽出が壊れている可能性があります。"
    )

    missing_in_frontend = backend_actions - frontend_labels
    extra_in_frontend = frontend_labels - backend_actions

    assert not missing_in_frontend, (
        "バックエンドで audit() されているが AUDIT_ACTION_LABELS に無いaction: "
        f"{sorted(missing_in_frontend)}"
    )
    assert not extra_in_frontend, (
        "AUDIT_ACTION_LABELS にあるがバックエンドで使われていない（削除漏れの可能性がある）action: "
        f"{sorted(extra_in_frontend)}"
    )
