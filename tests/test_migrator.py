"""migrator.py のテスト。

背景: 0003 の適用が本番D1で「前半失敗・後半成功」の部分適用になった事故がある
(migrations/0004_fix_student_role_check.sql:3-27)。ファイル単位の記録では
この状態を表現できないため、ステートメント単位で記録する。
"""
import os

import pytest

import db as dbmod
import migrator


class TestSplitStatements:
    def test_splits_on_semicolon(self):
        sql = "CREATE TABLE a (id INTEGER); CREATE TABLE b (id INTEGER);"
        assert migrator.split_statements(sql) == [
            "CREATE TABLE a (id INTEGER)",
            "CREATE TABLE b (id INTEGER)",
        ]

    def test_drops_line_comments(self):
        sql = "-- これはコメント\nCREATE TABLE a (id INTEGER);\n-- 末尾コメント"
        assert migrator.split_statements(sql) == ["CREATE TABLE a (id INTEGER)"]

    def test_drops_block_comments(self):
        sql = "/* 複数行\n   コメント */\nCREATE TABLE a (id INTEGER);"
        assert migrator.split_statements(sql) == ["CREATE TABLE a (id INTEGER)"]

    def test_keeps_semicolon_inside_string_literal(self):
        """文字列リテラル内のセミコロンで分割しないこと。"""
        sql = "INSERT INTO t (v) VALUES ('a;b'); INSERT INTO t (v) VALUES ('c');"
        assert migrator.split_statements(sql) == [
            "INSERT INTO t (v) VALUES ('a;b')",
            "INSERT INTO t (v) VALUES ('c')",
        ]

    def test_handles_escaped_quote(self):
        """'' でエスケープされたシングルクォートを正しく扱うこと。"""
        sql = "INSERT INTO t (v) VALUES ('it''s;fine'); SELECT 1;"
        assert migrator.split_statements(sql) == [
            "INSERT INTO t (v) VALUES ('it''s;fine')",
            "SELECT 1",
        ]

    def test_ignores_empty_statements(self):
        assert migrator.split_statements(";;\n\n;") == []

    def test_double_dash_inside_string_is_not_a_comment(self):
        """文字列リテラル内の `--` は行コメントとして読み飛ばさないこと。

        コメント除去を別パスで行うと、リテラルの内外を区別できず
        `'a--b'` の `--b'` 以降を消してクォート閉じ忘れの壊れた文になる。
        """
        sql = "INSERT INTO t (v) VALUES ('a--b');"
        assert migrator.split_statements(sql) == [
            "INSERT INTO t (v) VALUES ('a--b')",
        ]

    def test_block_comment_markers_inside_string_are_not_stripped(self):
        """文字列リテラル内の `/* */` はブロックコメントとして除去しないこと。

        これを誤って除去すると、例外も出さずに値が静かに欠落する
        （'a/*b*/c' が 'ac' になる等）。
        """
        sql = "INSERT INTO t (v) VALUES ('a/*b*/c');"
        assert migrator.split_statements(sql) == [
            "INSERT INTO t (v) VALUES ('a/*b*/c')",
        ]


class TestStatus:
    def test_legacy_files_are_backfilled_as_applied(self):
        """schema_migrations が空のとき、0004 以前は適用済みとして記録されること。

        0004 は staffs テーブルの再構築を含み、再実行が危険なため。
        """
        rows = migrator.status()
        legacy = [r for r in rows if r["filename"] in migrator.LEGACY_FILES]
        assert legacy, "レガシーのマイグレーションファイルが見つからない"
        assert all(r["applied"] for r in legacy), "レガシーが未適用と判定されている"

    def test_new_migration_is_pending(self):
        rows = migrator.status()
        new = [r for r in rows if r["filename"] == "0005_admin_console.sql"]
        assert new, "0005 が status に現れない"


class TestApplyPending:
    def test_applies_pending_and_records(self, tmp_path, monkeypatch):
        """未適用のステートメントを適用し、schema_migrations に記録すること。"""
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        (tmp_path / "0009_test.sql").write_text(
            "CREATE TABLE IF NOT EXISTS mig_test (id INTEGER);\n"
            "CREATE INDEX IF NOT EXISTS idx_mig_test ON mig_test(id);\n",
            encoding="utf-8")

        result = migrator.apply_pending()
        assert result["failed"] is None
        assert len(result["applied"]) == 2

        # 2回目は何も適用されない（記録済みのため）
        result2 = migrator.apply_pending()
        assert result2["applied"] == []
        dbmod.execute("DROP TABLE IF EXISTS mig_test")

    def test_skips_add_column_when_column_exists(self, tmp_path, monkeypatch):
        """ALTER TABLE ADD COLUMN は冪等でないため、列が既にあればスキップすること。"""
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        dbmod.execute("CREATE TABLE IF NOT EXISTS mig_col (id INTEGER, already TEXT)")
        (tmp_path / "0009_test.sql").write_text(
            "ALTER TABLE mig_col ADD COLUMN already TEXT;\n", encoding="utf-8")

        result = migrator.apply_pending()
        assert result["failed"] is None, f"スキップされず失敗している: {result['failed']}"
        assert len(result["skipped"]) == 1
        dbmod.execute("DROP TABLE IF EXISTS mig_col")

    def test_skips_add_column_without_column_keyword(self, tmp_path, monkeypatch):
        """`COLUMN` キーワード省略でも検出できること（SQLiteでは有効な構文）。

        検出漏れがあると「列が既に存在する状態で再実行→duplicate columnで停止」
        という実害になる。
        """
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        dbmod.execute("CREATE TABLE IF NOT EXISTS mig_col2 (id INTEGER, already TEXT)")
        (tmp_path / "0009_test.sql").write_text(
            "ALTER TABLE mig_col2 ADD already TEXT;\n", encoding="utf-8")

        result = migrator.apply_pending()
        assert result["failed"] is None, f"スキップされず失敗している: {result['failed']}"
        assert len(result["skipped"]) == 1
        dbmod.execute("DROP TABLE IF EXISTS mig_col2")

    def test_skips_add_column_with_quoted_identifiers(self, tmp_path, monkeypatch):
        """ダブルクォートで囲まれたテーブル名・列名でも検出できること。"""
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        dbmod.execute("CREATE TABLE IF NOT EXISTS mig_col3 (id INTEGER, already TEXT)")
        (tmp_path / "0009_test.sql").write_text(
            'ALTER TABLE "mig_col3" ADD COLUMN "already" TEXT;\n', encoding="utf-8")

        result = migrator.apply_pending()
        assert result["failed"] is None, f"スキップされず失敗している: {result['failed']}"
        assert len(result["skipped"]) == 1
        dbmod.execute("DROP TABLE IF EXISTS mig_col3")

    def test_partial_failure_records_only_successes(self, tmp_path, monkeypatch):
        """途中で失敗したとき、成功した分だけが記録され、失敗箇所が返ること。"""
        monkeypatch.setattr(migrator, "MIGRATIONS_DIR", str(tmp_path))
        monkeypatch.setattr(migrator, "LEGACY_FILES", ())
        (tmp_path / "0009_test.sql").write_text(
            "CREATE TABLE IF NOT EXISTS mig_ok (id INTEGER);\n"
            "THIS IS NOT VALID SQL;\n"
            "CREATE TABLE IF NOT EXISTS mig_never (id INTEGER);\n",
            encoding="utf-8")

        result = migrator.apply_pending()
        assert len(result["applied"]) == 1
        assert result["failed"] is not None
        assert result["failed"]["stmt_index"] == 1
        # 3文目は実行されていない
        assert dbmod.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mig_never'") is None

        # 再実行すると失敗箇所から再開する（1文目は記録済みなのでスキップ）
        result2 = migrator.apply_pending()
        assert result2["applied"] == []
        assert result2["failed"]["stmt_index"] == 1
        dbmod.execute("DROP TABLE IF EXISTS mig_ok")


# ============================================================
# レガシー backfill の原子性（データ消失につながる部分適用の防止）
# ============================================================
# 旧実装は 39 本の個別 INSERT OR IGNORE でレガシーを「適用済み」として記録し、
# ガードは「schema_migrations が完全に空のときだけ backfill する」だった。
# D1 では 1 文 = 1 HTTP 往復・トランザクション無しなので、途中で落ちると
# 「1行だけ書けた」状態になり、その1行がガードを永久に閉じてしまう。
# すると残りのレガシー文が恒久的に「未適用」となり、次に「未適用を適用」を押すと
# migrations/0004 のバックアップ表作成（CREATE TABLE _mig_* AS SELECT *）だけが
# 「適用済み」としてスキップされ、DELETE FROM shifts / wish_history /
# fixed_shifts / change_requests だけが実行され得る（実測でデータ全消失）。
#
# しかもこれは本番D1でマイグレーション画面を初めて開いた瞬間
# （GET /api/admin/migrations → status() → _backfill_legacy()）に走る。
D1_MAX_BINDS = 100


def _legacy_row_count():
    return len(dbmod.query_all(
        "SELECT filename, stmt_index FROM schema_migrations WHERE filename IN ({})".format(
            ",".join("?" * len(migrator.LEGACY_FILES))), tuple(migrator.LEGACY_FILES)))


def _expected_legacy_rows():
    import os
    n = 0
    for f in migrator.LEGACY_FILES:
        if os.path.exists(os.path.join(migrator.MIGRATIONS_DIR, f)):
            n += len(migrator._read_statements(f))
    return n


class TestBackfillAtomicity:
    def test_backfill_is_a_single_statement(self, monkeypatch):
        """39往復ではなく1文で記録すること（D1では1文=1HTTP往復）。"""
        migrator._ensure_table()
        calls = []
        orig = migrator.execute

        def spy(sql, params=()):
            calls.append((sql, params))
            return orig(sql, params)

        monkeypatch.setattr(migrator, "execute", spy)
        migrator._backfill_legacy()
        inserts = [c for c in calls if "INSERT" in c[0].upper()
                   and "schema_migrations" in c[0]]
        assert len(inserts) == 1, (
            f"backfill が {len(inserts)} 文に分かれている（1文のまとめINSERTのはず）")
        assert _legacy_row_count() == _expected_legacy_rows()

    def test_backfill_binds_stay_within_d1_limit(self, monkeypatch):
        """まとめ INSERT でも D1 のバインド上限（1クエリ100個）を超えないこと。"""
        migrator._ensure_table()
        counts = []
        orig = migrator.execute

        def spy(sql, params=()):
            counts.append(len(params or ()))
            return orig(sql, params)

        monkeypatch.setattr(migrator, "execute", spy)
        migrator._backfill_legacy()
        assert counts, "execute が呼ばれていない"
        assert max(counts) <= D1_MAX_BINDS, (
            f"D1のバインド上限({D1_MAX_BINDS})を超えている: 最大{max(counts)}バインド")

    def test_failed_backfill_leaves_no_partial_rows(self, monkeypatch):
        """記録が失敗したとき、部分的な行が残らないこと（1文なので全か無か）。"""
        migrator._ensure_table()
        orig = migrator.execute

        def boom(sql, params=()):
            if "INSERT" in sql.upper() and "schema_migrations" in sql:
                raise RuntimeError("D1 API error: 模擬的な途中失敗")
            return orig(sql, params)

        monkeypatch.setattr(migrator, "execute", boom)
        with pytest.raises(Exception):
            migrator._backfill_legacy()
        assert _legacy_row_count() == 0, "部分的に記録された行が残っている"

    def test_partial_backfill_is_completed_on_next_call(self):
        """途中で落ちて一部だけ記録された状態からでも、次回の呼び出しで完了すること。

        【なぜ必須か】旧ガード（schema_migrations が空でなければ何もしない）だと、
        1行でも書けた時点で backfill が二度と走らず、残りのレガシー文が恒久的に
        「未適用」になる。その状態で「未適用を適用」を押すと 0004 の
        バックアップ表作成だけがスキップされ、DELETE 群だけが走ってデータが消える。
        """
        migrator._ensure_table()
        first = migrator.LEGACY_FILES[0]
        # 「1行だけ書けたところで落ちた」状態を作る
        dbmod.execute("INSERT INTO schema_migrations (filename, stmt_index) VALUES (?,?)",
                      (first, 0))
        migrator._backfill_legacy()
        assert _legacy_row_count() == _expected_legacy_rows(), (
            "部分的な backfill から復帰できていない（レガシーが未適用のまま残る）")

    def test_all_legacy_statements_are_applied_after_backfill(self):
        """backfill 後、レガシーの全ステートメントが status() で適用済みになること。"""
        rows = migrator.status()
        legacy = [r for r in rows if r["filename"] in migrator.LEGACY_FILES]
        assert legacy
        assert all(r["applied"] for r in legacy)

    def test_backfill_does_not_run_once_db_is_managed(self, monkeypatch):
        """レガシー以外の記録が1つでもあれば、このDBは既に migrator の管理下。
        レガシーを「適用済み」と勝手に決めつけない（本来の backfill ガードの意図）。"""
        migrator._ensure_table()
        dbmod.execute("INSERT INTO schema_migrations (filename, stmt_index) VALUES (?,?)",
                      ("0005_admin_console.sql", 0))
        calls = []
        orig = migrator.execute
        monkeypatch.setattr(migrator, "execute",
                            lambda sql, params=(): (calls.append(sql), orig(sql, params))[1])
        migrator._backfill_legacy()
        assert not [c for c in calls if "INSERT" in c.upper()], (
            "既に管理下のDBでレガシーを勝手に適用済み記録している")
        assert _legacy_row_count() == 0
