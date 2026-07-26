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
