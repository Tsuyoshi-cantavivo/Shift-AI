"""ドラフトの直接調整に必要なフロントエンド契約を守る。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_timeline_includes_draft_only_drag_hooks_and_patch_endpoint():
    source = (ROOT / "public" / "app.js").read_text(encoding="utf-8")

    assert "function installDraftTimelineDrag" in source
    assert "data-draft-editable" in source
    assert "tl-drag-handle-start" in source
    assert "tl-drag-handle-end" in source
    assert "/draft-time" in source
    assert "method: 'PATCH'" in source
    assert "pointerdown" in source
    assert "window.addEventListener('pointerup'" in source


def test_timeline_styles_communicate_draft_drag_state():
    styles = (ROOT / "public" / "style.css").read_text(encoding="utf-8")

    assert ".tl-bar-draft[data-draft-editable=\"true\"]" in styles
    assert ".tl-drag-handle" in styles
    assert ".tl-bar-dragging" in styles
    assert ".tl-bar-save-error" in styles


def test_inline_ai_generation_saves_draft_for_timeline_adjustment():
    source = (ROOT / "public" / "app.js").read_text(encoding="utf-8")
    start = source.index("async function runShiftGenInline")
    end = source.index("\nfunction openAddShiftModal", start)
    inline_generation = source[start:end]

    assert "ドラフトとして保存して調整" in inline_generation
    assert "body: JSON.stringify({ start_date: start, end_date: end, draft: true })" in inline_generation
    assert "この内容で確定" not in inline_generation
