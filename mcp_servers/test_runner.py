"""ShiftAI テスト実行 MCP サーバ.

opencode (または他のMCPホスト) から pytest の実行とシフトエンジンの検証を
行うためのローカル stdio MCP サーバ。

ツール:
  - run_tests: pytest を実行して結果サマリを返す
  - run_legacy_tests: 既存の tests/run_tests.py を実行
  - lint_check: 構文チェック (py_compile + app.js)
  - verify_shift: 指定日付範囲で実際のDBからシフト自動作成を行い時間帯別人数を返す

起動方法 (opencode.json の mcp.test_runner.command):
  [".venv/bin/python", "mcp_servers/test_runner.py"]
"""
import os
import re
import subprocess
import sys

from mcp.server.fastmcp import FastMCP

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(BASE_DIR, ".venv", "bin", "python")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

mcp = FastMCP("shift-test-runner")


def _run(cmd, cwd=BASE_DIR, timeout=180):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "タイムアウト(%ds)" % timeout
    except Exception as e:
        return 1, "", f"実行エラー: {e}"


@mcp.tool()
def run_tests(target: str = "all") -> str:
    """pytest を実行して結果サマリを返す。

    Args:
        target: "all"(全テスト) | "app"(test_app.py) | "engine"(シフトエンジン関連のみ)
                または特定テストパス (例 "tests/test_app.py::TestShiftEngine")
    """
    if target in ("all", "app"):
        path = "tests/test_app.py"
    elif target == "engine":
        path = "tests/test_app.py::TestShiftEngine tests/test_app.py::TestShiftApi"
    else:
        path = target
    rc, out, err = _run([PYTHON, "-m", "pytest", path, "-v", "--tb=short", "-q"])
    # 結果要約
    summary = ""
    for line in out.splitlines():
        if re.search(r"passed|failed|error", line) and "%" in line:
            summary = line
    if not summary:
        m = re.search(r"=+ (.+?) =+\s*$", out)
        summary = m.group(1) if m else ""
    tail = "\n".join(out.splitlines()[-30:])
    status = "PASS" if rc == 0 else "FAIL/ERROR"
    return f"[{status}] (exit={rc}) {summary}\n--- 出力末尾 ---\n{tail}\n--- stderr ---\n{err[-500:]}"


@mcp.tool()
def run_legacy_tests() -> str:
    """従来の tests/run_tests.py（シフトエンジン不変量テスト）を実行して結果を返す。"""
    rc, out, err = _run([PYTHON, "tests/run_tests.py"])
    return f"[{'PASS' if rc == 0 else 'FAIL'}] (exit={rc})\n{out[-1500:]}\n{err[-300:]}"


@mcp.tool()
def lint_check() -> str:
    """Python 全ソースと app.js の構文チェックを行う。"""
    issues = []
    # Python: py_compile
    for root in ("src", "tests", "mcp_servers"):
        for dirpath, _dirs, files in os.walk(os.path.join(BASE_DIR, root)):
            if "__pycache__" in dirpath:
                continue
            for f in files:
                if f.endswith(".py"):
                    fp = os.path.join(dirpath, f)
                    rc, _o, e = _run([PYTHON, "-m", "py_compile", fp])
                    if rc != 0:
                        issues.append(f"{fp}: {e.strip()}")
    # JS: node --check
    js = os.path.join(BASE_DIR, "public", "app.js")
    if os.path.exists(js):
        rc, _o, e = _run(["node", "--check", js])
        if rc != 0:
            issues.append(f"{js}: {e.strip()}")
    if not issues:
        return "[OK] 構文エラーなし (src/, tests/, mcp_servers/*.py, public/app.js)"
    return "[NG] 構文エラー:\n" + "\n".join(issues)


@mcp.tool()
def verify_shift(start_date: str, end_date: str, shop_code: str = "SHOP001") -> str:
    """実際のDBを読み込み、指定日付範囲でシフト自動作成(dry)を行い時間帯別人数を返す。

    Args:
        start_date: 開始日 (YYYY-MM-DD)
        end_date: 終了日 (YYYY-MM-DD)
        shop_code: 店舗コード (既定 SHOP001)
    """
    script = (
        "import sys; sys.path.insert(0,'src')\n"
        "import db, shift_engine, json\n"
        f"shop = db.query_one('SELECT * FROM shops WHERE shop_code=?', ('{shop_code}',))\n"
        "if not shop:\n"
        "    print('店舗が見つかりません: " + shop_code + "'); sys.exit(0)\n"
        "settings = json.loads(shop['settings'] or '{}')\n"
        f"res = shift_engine.auto_generate(shop['id'], settings, '{start_date}', '{end_date}')\n"
        "print('confirmed:', len(res['confirmed']), 'shortage:', len(res['shortage']))\n"
        "for s in res['shortage']:\n"
        "    print('  SHORTAGE', s['date'], s['pattern'], '必要', s['required'], '配置', s['placed'])\n"
        "days = sorted(set(s['start'][:10] for s in res['confirmed']))\n"
        "for day in days:\n"
        "    hrs = {}\n"
        "    for s in res['confirmed']:\n"
        "        if s['start'][:10] != day: continue\n"
        "        for hr in range(int(s['start'][11:13]), int(s['end'][11:13])):\n"
        "            hrs[hr] = hrs.get(hr, 0) + 1\n"
        "    parts = [f'{h}:00={hrs[h]}人' + ('<不足' if hrs[h] < 2 and 9 <= h < 22 else '') for h in sorted(hrs)]\n"
        "    print(day, '|', ' '.join(parts))\n"
    )
    rc, out, err = _run([PYTHON, "-c", script])
    return f"[{'OK' if rc == 0 else 'ERR'}] (exit={rc})\n{out}\n{err[-400:]}"


if __name__ == "__main__":
    mcp.run()
