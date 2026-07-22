# ドラフト運用の一本化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 利用者画面のAI生成から公開までをドラフト調整後の明示的な確定操作に統一する。

**Architecture:** `public/app.js` のUIだけから即確定・自動一括確定を外す。既存バックエンドAPIは削除せず、画面の唯一の公開操作である `finalizeDraftBtn` を維持する。CSSではドラフトの機能的な見た目を残し、文字ラベルだけを外す。

**Tech Stack:** バニラJavaScript、CSS、pytest。

## Global Constraints

- AI生成はドラフト保存だけを利用者に提示する。
- 公開・通知は「ドラフトを確定・通知」だけから行う。
- 既存の自動確定APIは削除しない。
- ドラフトバーのドラッグ操作と視覚的な区別は維持する。

---

### Task 1: ドラフトフローUIの整理

**Files:**
- Modify: `tests/test_draft_timeline_assets.py`
- Modify: `public/app.js:1813-1842,1860-1865,1972-1989`
- Modify: `public/style.css:618-638`

**Interfaces:**
- Consumes: `#finalizeDraftBtn` による `POST /api/shop/shifts/finalize`。
- Produces: 即確定・自動一括確定のUIがないドラフト調整フロー。

- [ ] **Step 1: Write failing front-end contract tests**

```python
assert "autoConfirmBtn" not in source
assert "今すぐ確定・通知" not in source
assert "finalizeDraftBtn" in source
assert "content: 'ドラフト'" not in styles
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_draft_timeline_assets.py -k "flow or label" -q`

Expected: FAIL because the current screen still offers automatic or immediate confirmation and the CSS label exists.

- [ ] **Step 3: Implement minimum UI removal**

Remove the `confirmGen` and `autoConfirmBtn` markup and listeners. Keep `finalizeDraftBtn` and remove only `.tl-bar.tl-bar-draft::before`.

- [ ] **Step 4: Run focused and full validation**

Run: `pytest tests/test_draft_timeline_assets.py -q && pytest tests -q && node --check public/app.js && git diff --check`

Expected: all tests pass and no whitespace or syntax errors are reported.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-22-draft-flow-simplification-design.md docs/superpowers/plans/2026-07-22-draft-flow-simplification.md public/app.js public/style.css tests/test_draft_timeline_assets.py
git commit -m "feat: simplify draft shift workflow"
```
