"""tests/test_wish_image_import.py — 希望表の画像取込。

実行: ./.venv/bin/python -m pytest tests/test_wish_image_import.py -v

外部APIは叩かない。_call_llm_messages を monkeypatch して検証ロジックだけを見る
（tests/test_wish_text_import.py の _use_llm と同じ作法）。
"""
import base64
import json

import pytest

from src import ai
import app as appmod

_PNG_1PX = ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _use_vision(monkeypatch, response, capture=None):
    """LLM を有効にし、_call_llm_messages の戻り値を差し替える。"""
    monkeypatch.setattr(ai, "is_llm_available", lambda: True)

    def fake(messages, *a, **k):
        if capture is not None:
            capture.append(messages)
        return response

    monkeypatch.setattr(ai, "_call_llm_messages", fake)


class TestParseWishImage:
    def test_returns_none_when_llm_unavailable(self, monkeypatch):
        """AI未接続では画像を読めない。正規表現フォールバックは存在しない。"""
        monkeypatch.setattr(ai, "is_llm_available", lambda: False)
        assert ai.parse_wish_image([_PNG_1PX], "2026-08", []) is None

    def test_parses_valid_response(self, monkeypatch):
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "田中 8/3 休み",
            "entries": [{"staff_hint": "田中", "dates": ["2026-08-03"],
                         "availability": "rest", "start": None, "end": None,
                         "raw": "田中 8/3 休み"}],
            "unparsed": [],
        }))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", ["田中太郎"])
        assert r["entries"][0]["dates"] == ["2026-08-03"]
        assert r["entries"][0]["availability"] == "rest"
        assert r["ocr_text"] == "田中 8/3 休み"

    def test_image_is_sent_as_vision_content(self, monkeypatch):
        """messages の content が配列で、image_url を含むこと。"""
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert cap, "_call_llm_messages が呼ばれていない"
        user_msgs = [m for m in cap[0] if m.get("role") == "user"]
        assert user_msgs, "user メッセージが無い"
        content = user_msgs[-1]["content"]
        assert isinstance(content, list), "content が配列でない（vision 形式になっていない）"
        assert any(p.get("type") == "image_url" for p in content), "image_url が含まれていない"

    def test_multiple_images_are_all_sent(self, monkeypatch):
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX, _PNG_1PX], "2026-08", [])
        content = [m for m in cap[0] if m.get("role") == "user"][-1]["content"]
        assert sum(1 for p in content if p.get("type") == "image_url") == 2

    def test_non_json_response_does_not_crash(self, monkeypatch):
        _use_vision(monkeypatch, "すみません、読み取れませんでした")
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r is not None
        assert r["entries"] == []
        assert r["unparsed"], "読めなかった旨が unparsed に残っていない"

    def test_none_response_does_not_crash(self, monkeypatch):
        _use_vision(monkeypatch, None)
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r is not None
        assert r["entries"] == []

    def test_missing_ocr_text_defaults_to_empty(self, monkeypatch):
        _use_vision(monkeypatch, json.dumps({"entries": [], "unparsed": []}))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r["ocr_text"] == ""

    def test_invalid_entries_go_to_unparsed(self, monkeypatch):
        """既存の契約（検証で落ちた entry は捨てず unparsed に積む）を守ること。"""
        _use_vision(monkeypatch, json.dumps({
            "ocr_text": "x",
            "entries": [{"staff_hint": "田中", "dates": ["めちゃくちゃな日付"],
                         "availability": "rest", "raw": "田中 ??"}],
            "unparsed": [],
        }))
        r = ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        assert r["entries"] == []
        assert r["unparsed"], "落とした entry が unparsed に残っていない"

    def test_prompt_contains_injection_guard(self, monkeypatch):
        """画像内に書かれた指示に従わない旨がプロンプトに含まれること。"""
        cap = []
        _use_vision(monkeypatch, json.dumps({"ocr_text": "", "entries": [], "unparsed": []}), cap)
        ai.parse_wish_image([_PNG_1PX], "2026-08", [])
        sys_msgs = [m for m in cap[0] if m.get("role") == "system"]
        assert sys_msgs
        text = json.dumps(sys_msgs, ensure_ascii=False)
        assert "指示" in text, "画像内の指示に従わない旨の記述が無い"


class TestPostLlmTimeout:
    def test_post_llm_accepts_timeout(self):
        """画像は推論が長いので timeout を延ばせること（署名の確認）。"""
        import inspect
        sig = inspect.signature(ai._post_llm)
        assert "timeout" in sig.parameters, "_post_llm に timeout 引数が無い"


from helpers import insert_shop, insert_staff, make_session, auth


def _use_vision_api(monkeypatch, response, capture=None):
    """POST /api/shop/wishes/parse-image 用: appmod.ai を差し替える。

    重要な落とし穴: app.py は `import ai`（bare の "ai"）で束縛しているため、
    本ファイル冒頭の `from src import ai` とは sys.modules 上で別オブジェクトに
    なる。エンドポイント経由のテストは必ず appmod.ai を差し替えること
    （tests/test_wish_text_import.py が既に同じ理由でこの作法を採用している。
    実測して確認済み）。
    """
    monkeypatch.setattr(appmod.ai, "is_llm_available", lambda: True)

    def fake(messages, *a, **k):
        if capture is not None:
            capture.append(messages)
        return response

    monkeypatch.setattr(appmod.ai, "_call_llm_messages", fake)


def _b64_data_url(mime, raw_bytes):
    return f"data:{mime};base64," + base64.b64encode(raw_bytes).decode("ascii")


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestParseImageApi:
    def _tok(self, shop_id):
        return make_session("shop", shop_id, shop_id)

    def test_requires_shop_role(self, client):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "PT1", "田中太郎")
        tok = make_session("staff", sid, shop_id)
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX]}, headers=auth(tok))
        assert r.status_code in (401, 403)

    def test_returns_503_when_llm_unavailable(self, client, monkeypatch):
        monkeypatch.setattr(appmod.ai, "is_llm_available", lambda: False)
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX], "year_month": "2026-08"},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 503
        assert "テキスト" in (r.get_json() or {}).get("error", ""), \
            "テキスト貼り付けへの誘導が無い"

    def test_rejects_empty_images(self, client):
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": []}, headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_non_image_data_url(self, client):
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": ["data:text/html;base64,PHNjcmlwdD4="]},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_too_many_images(self, client):
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX] * 4},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_oversized_image(self, client):
        shop_id = insert_shop()
        big = "data:image/png;base64," + ("A" * (5 * 1024 * 1024))
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [big]}, headers=auth(self._tok(shop_id)))
        assert r.status_code in (400, 413)

    def test_resolves_staff_and_returns_candidates(self, client, monkeypatch):
        shop_id = insert_shop()
        sid = insert_staff(shop_id, "PT1", "田中太郎")
        _use_vision_api(monkeypatch, json.dumps({
            "ocr_text": "田中 8/3 休み",
            "entries": [{"staff_hint": "田中", "dates": ["2026-08-03"],
                         "availability": "rest", "raw": "田中 8/3 休み"}],
            "unparsed": [],
        }))
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX], "year_month": "2026-08"},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 200
        d = r.get_json()
        assert d["entries"][0]["staff_id"] is None, "姓のみで自動確定してしまった"
        assert (d.get("name_candidates") or {}).get("0"), "候補が返っていない"
        assert d["ocr_text"] == "田中 8/3 休み"
        # raw が ocr_text 全文と完全一致する正常系では verified=True になること。
        # test_raw_verified_uses_ocr_text（raw がocr_textに無い場合）だけでは
        # 「照合元を空文字に差し替えても常にFalseなので気づけない」という
        # 抜け穴があるため、正例をここで確認する（実測して確認済み）。
        assert d["entries"][0]["raw_verified"] is True, \
            "OCR全文に実在する raw が検証で弾かれている"

    def test_raw_verified_uses_ocr_text(self, client, monkeypatch):
        """OCR全文に無い raw は raw_verified=False になること（創作の検出）。

        _sanitize_llm_wish_result（src/ai.py）が raw_verified を計算する
        わけではない（第2引数の text は entries/unparsed が両方空だった
        場合の沈黙防止にしか使わない、src/ai.py:1474-1475）。raw_verified は
        src/app.py の _wish_raw_verified をエンドポイント側で明示的に
        呼んで初めて付く。ここではその配線そのものを検証する。
        """
        shop_id = insert_shop()
        _use_vision_api(monkeypatch, json.dumps({
            "ocr_text": "田中 8/3 休み",
            "entries": [{"staff_hint": "田中", "dates": ["2026-08-03"],
                         "availability": "rest", "raw": "画像に無い作り話"}],
            "unparsed": [],
        }))
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX], "year_month": "2026-08"},
                        headers=auth(self._tok(shop_id)))
        d = r.get_json()
        assert d["entries"][0]["raw_verified"] is False, \
            "OCR全文に無い文が検証を通ってしまった（安全弁が効いていない）"

    def test_does_not_save_anything(self, client, monkeypatch):
        """解析だけで DB に書かないこと。"""
        from db import query_all
        shop_id = insert_shop()
        insert_staff(shop_id, "PT1", "田中太郎")
        _use_vision_api(monkeypatch, json.dumps({
            "ocr_text": "田中太郎 8/3 休み",
            "entries": [{"staff_hint": "田中太郎", "dates": ["2026-08-03"],
                         "availability": "rest", "raw": "田中太郎 8/3 休み"}],
            "unparsed": [],
        }))
        client.post("/api/shop/wishes/parse-image",
                    json={"images": [_PNG_1PX], "year_month": "2026-08"},
                    headers=auth(self._tok(shop_id)))
        assert not query_all("SELECT id FROM wish_history"), "wish_history に書かれた"
        assert not query_all("SELECT id FROM shifts"), "shifts に書かれた"

    # --- ここから: Task3 レビューの申し送り（追加要件1〜3）対応の回帰テスト ---

    def test_rejects_ssrf_style_url(self, client):
        """images に data: 以外のスキーム（http等）を渡すとSSRFになりうるため拒否する。

        src/ai.py の parse_wish_image は images の中身を検証せず、そのまま
        {"type": "image_url", "image_url": {"url": img}} としてLLMプロバイダに
        渡す。http(s) URL を許すと、LLM側にそのURLを取得させられる
        （内部ネットワーク・メタデータエンドポイント等へのSSRF）。
        エンドポイント側で data: スキームのみに限定して塞ぐ。
        """
        shop_id = insert_shop()
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": ["http://169.254.169.254/latest/meta-data/"]},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_wrong_magic_number_with_valid_header(self, client):
        """ヘッダは image/png を名乗るが中身がPNGでない場合を弾く（マジックナンバー検証単体）。

        test_rejects_non_image_data_url（text/htmlヘッダ）はヘッダの mime
        許可リストだけで弾かれてしまい、マジックナンバー検証を消しても
        green のまま残る（実測して確認済み・二重防御になっている）。
        マジックナンバー検証だけを独立して赤にするにはこちらが必要。
        """
        shop_id = insert_shop()
        fake_png = _b64_data_url("image/png", b"not a real png at all!!")
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [fake_png]}, headers=auth(self._tok(shop_id)))
        assert r.status_code == 400

    def test_rejects_oversized_with_valid_magic_number(self, client):
        """正しいPNGマジックナンバーで始まるが4MBを超える画像を弾く（サイズ上限検証単体）。

        brief記載の test_rejects_oversized_image（"A"*5MB）はマジックナンバーが
        PNGとして不正なため、サイズ上限チェックを消してもマジックナンバー検証が
        代わりに弾いてしまい green のまま残る（実測して確認済み）。
        サイズ上限だけを独立して赤にするにはこちらが必要。
        """
        shop_id = insert_shop()
        oversized = _PNG_MAGIC + b"\x00" * (4 * 1024 * 1024 + 4096)
        big = _b64_data_url("image/png", oversized)
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [big]}, headers=auth(self._tok(shop_id)))
        assert r.status_code in (400, 413)

    def test_surfaces_llm_error_detail_on_failure(self, client, monkeypatch):
        """AI呼び出し自体が失敗した（例: LLM_VISION_MODEL未設定でvision非対応モデルに
        画像を送りHTTP 400）ときは、定型文だけでなく ai.get_last_llm_error() の
        詳細もレスポンスに含めること。

        src/ai.py:1615-1617 は API呼び出し失敗と「JSON以外の応答」を同じ定型文
        （「画像を解析できませんでした…」）に潰しており、get_last_llm_error() の
        実情報（例: "HTTP 400: ... image_url"）が捨てられている。本番で最も
        起きやすい failure mode（vision非対応モデルへの誤送信）で店長の原因調査を
        誤誘導しないよう、エンドポイント側で補って返す。
        """
        shop_id = insert_shop()
        monkeypatch.setattr(appmod.ai, "is_llm_available", lambda: True)
        monkeypatch.setattr(appmod.ai, "_call_llm_messages", lambda *a, **k: None)
        monkeypatch.setattr(appmod.ai, "get_last_llm_error",
                            lambda: "HTTP 400: type=invalid_request_error "
                                    "message=model does not support image_url")
        r = client.post("/api/shop/wishes/parse-image",
                        json={"images": [_PNG_1PX], "year_month": "2026-08"},
                        headers=auth(self._tok(shop_id)))
        assert r.status_code == 200
        d = r.get_json()
        detail = d.get("llm_error_detail") or ""
        assert "image_url" in detail and "400" in detail, \
            "get_last_llm_error() の詳細がレスポンスに含まれていない"
