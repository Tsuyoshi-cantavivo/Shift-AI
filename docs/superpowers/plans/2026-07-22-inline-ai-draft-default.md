# インラインAI生成のドラフト保存 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 通常のAI生成をドラフト保存に統一し、確定操作を明示的に分離する。

**Architecture:** 通常のシフト管理画面はドラフト保存を明示して呼び出す。API既定値は既存連携との互換性のため維持し、公開は既存の明示的な確定・通知操作に分離する。

**Tech Stack:** Flask、SQLite、バニラJavaScript、pytest。

## Global Constraints

- 利用者画面からのAI生成だけでは通知・公開を行わない。
- 既存の確定シフトをドラッグ可能にしない。
- 即確定は明示的な `draft: false` に限定する。

---

### Task 1: AI生成のドラフト既定化

**Files:**
- Modify: `tests/test_ai_draft_finalize.py`
- Modify: `tests/test_draft_timeline_assets.py`
- Modify: `src/app.py:2285-2287`
- Modify: `public/app.js:2021-2050`

**Interfaces:**
- Consumes: `POST /api/shop/shifts/auto` の `draft` ブール値。
- Produces: インライン画面の主操作は `draft: true` を送信する。

- [ ] **Step 1: Write the failing front-end contract test**

```python
assert "id=\"saveInlineDraft\"" in source
assert "draft: true" in inline_generation_block
```

- [ ] **Step 2: Run the front-end contract test to verify it fails**

Run: `pytest tests/test_draft_timeline_assets.py -k inline -q`

Expected: the inline generation block still sends no `draft` value and labels the action as confirmation.

- [ ] **Step 3: Implement the minimum behavior**

```javascript
body: JSON.stringify({ start_date: start, end_date: end, draft: true })
```

Use a primary label of `ドラフトとして保存して調整`. Publish through the existing explicit `ドラフトを確定・通知` action.

- [ ] **Step 4: Run focused tests and the full suite**

Run: `pytest tests/test_ai_draft_finalize.py tests/test_draft_timeline_assets.py -q && pytest tests -q && node --check public/app.js`

Expected: all tests pass and JavaScript syntax validation exits 0.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-07-22-inline-ai-draft-default-design.md docs/superpowers/plans/2026-07-22-inline-ai-draft-default.md public/app.js tests/test_draft_timeline_assets.py
git commit -m "fix: save inline AI generation as draft"
```
