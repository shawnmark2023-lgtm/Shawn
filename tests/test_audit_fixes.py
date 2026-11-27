"""Regression tests for engineering audit bug fixes and modular UI integrity."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

import pytest

from cctr_export.core import database as db
from cctr_export.core import kernels
from cctr_export.core.api import CCTRClient
from cctr_export.core.formats import export_questions_to_xlsx
from cctr_export.core.models import Option, Practice, Question


def test_database_scrubs_passwords_recovers_stale_and_cleans_snapshot(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    db.init_db()

    # 1. add_task must never persist plaintext passwords
    tid = db.add_task(
        "u101441",
        "student1",
        "SuperSecretPassword123!",
        db.ExportType.PDF,
        [{"practice": "{}"}],
        auth_key="tok",
    )
    t = db.get_task(tid)
    assert t is not None
    assert t.password == ""

    # 2. Simulate legacy DB row with plaintext password + stuck processing state
    conn = sqlite3.connect(str(tmp_path / ".cctr_export" / "tasks.db"))
    conn.execute("UPDATE tasks SET password='legacy_leak', status='processing' WHERE id=?", (tid,))
    conn.commit()
    conn.close()

    snap_dir = tmp_path / ".cctr_export" / "task_questions"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_file = snap_dir / f"{tid}.json"
    snap_file.write_text('{"questions": []}', encoding="utf-8")

    # Re-running init_db must scrub password and mark interrupted processing task as failed
    db.init_db()
    t2 = db.get_task(tid)
    assert t2 is not None
    assert t2.password == ""
    assert t2.status == db.TaskStatus.FAILED
    logs = db.get_logs(tid)
    assert any("异常中断" in row["message"] for row in logs)

    # 3. delete_task must also remove question snapshot file
    db.delete_task(tid)
    assert not snap_file.exists()


def test_kernels_save_settings_concurrent_atomic(monkeypatch, tmp_path: Path):
    sfile = tmp_path / ".cctr_export" / "settings.json"
    monkeypatch.setattr(kernels, "SETTINGS_FILE", sfile)

    errors: list[Exception] = []

    def worker(i: int):
        try:
            for j in range(10):
                kernels.save_settings({f"k_{i}_{j}": j})
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors
    loaded = json.loads(sfile.read_text(encoding="utf-8"))
    for i in range(8):
        for j in range(10):
            assert loaded[f"k_{i}_{j}"] == j


def test_api_single_split_short_count_raises(monkeypatch):
    c = CCTRClient("u1", "user", "pw")
    c.key = "k"
    p = Practice(id=1, courseid="10", practise_id="20", teacher_id="30", lesson_name="L", practice_name="P", teacher_name="", finish_time="", question_count=10)

    monkeypatch.setattr(c, "get_chapter_and_question_type", lambda _p: ([], []))
    # Only 2 items returned while split advertises 4 ids in 1 split chunk
    monkeypatch.setattr(
        c,
        "_fetch_question_page_raw",
        lambda *_a, **_kw: ([{"questionid": "1"}, {"questionid": "2"}], ["1,2,3,4"], 4),
    )

    with pytest.raises(RuntimeError, match="分页"):
        c.query_questions(p, practiced=False)


def test_dropped_files_worker_workspace_mode(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from cctr_export.ui.studio_web import _DroppedFilesWorker

    qs = [
        Question(
            id="q1",
            question_type="单选题",
            subject_html="<p>测试题</p>",
            answer="A",
            has_options=True,
            options=[Option(True, "正确"), Option(False, "错误")],
            subject_name="测试课程",
            chapter_name="第一章",
            is_choice=True,
        )
    ]
    xlsx = export_questions_to_xlsx(qs, tmp_path / "demo.xlsx", title="测试课程")
    worker = _DroppedFilesWorker([str(xlsx)], "workspace")
    results: list[tuple[str, object]] = []
    worker.done.connect(lambda z, res: results.append((z, res)))
    worker.run()

    assert len(results) == 1
    zone, payload = results[0]
    assert zone == "workspace"
    assert isinstance(payload, dict) and payload["ok"] is True
    assert isinstance(payload["practices"][0], dict)
    assert "json" in payload["practices"][0]


def test_modular_ui_ids_bridge_and_no_inline_escaped_onclick():
    web_dir = Path("cctr_export/ui/web")
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    css = (web_dir / "styles.css").read_text(encoding="utf-8")
    js = (web_dir / "app.js").read_text(encoding="utf-8")
    py = Path("cctr_export/ui/studio_web.py").read_text(encoding="utf-8")

    assert len(css) > 2000 and len(js) > 3000
    assert "./styles.css" in html and "./app.js" in html

    ids_in_dom = set(re.findall(r'\bid="([^"]+)"', html))
    qs_calls = set(re.findall(r"qs\(['\"]([^'\"]+)['\"]\)", js))
    assert qs_calls - ids_in_dom == set()

    slots = set(re.findall(r"@Slot\([^)]*\)\s+def\s+(\w+)", py))
    signals = set(re.findall(r"^\s+(\w+)\s*=\s*Signal\(", py, re.M))
    bridge_calls = set(re.findall(r"\bStore\.bridge\.(\w+)", js)) - {"bridge"}
    # Also verify b.<method> inside QWebChannel block maps cleanly
    qwc_block = js.split("const b = channel.objects.cctrBridge;", 1)[1]
    qwc_calls = {m for m in re.findall(r"\bb\.(\w+)", qwc_block) if m not in ("classList", "dataset")}
    assert (bridge_calls | qwc_calls) - slots - signals == set()

    # Zero dangerous inline onclick="... '${escapeHtml(...)}' ..."
    assert not re.search(r"onclick=\"[^\"]*'\\$\\{escapeHtml", html + js)
