"""QWebEngineView + QWebChannel 桌面工作台桥接器（1:1 Figma Studio 视觉界面 + 本地 Python 引擎）"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, QFile, QIODevice, QObject, QSize, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QNetworkProxy, QNetworkProxyFactory
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.api import CCTRClient
from ..core.captcha_ocr import is_ocr_available, recognize_captcha
from ..core.database import (
    ExportType,
    TaskStatus,
    add_task,
    clear_all_logs,
    delete_task,
    get_all_logs,
    get_all_tasks,
    update_task_status,
)
from ..core.exporter import init_qt_pdf_bridge
from ..core.formats import (
    get_local_bank_filters,
    html_to_plain_text,
    import_questions_from_file,
    load_local_bank_questions,
    save_local_bank,
)
from ..plugins import list_registered_plugins
from ..plugins.ai_knowledge import (
    PROVIDER_PRESETS,
    WEB_ASSISTANT_PRESETS,
    KnowledgeTopic,
    KnowledgeWorkspace,
    ModelProfile,
    MultimodalModelClient,
    NoteItem,
    _expand_dropped_paths,
    ai_classify_and_fuse,
    analyze_note_item,
    build_workbench_handoff_pack,
    classify_and_import_dropped_paths,
    export_knowledge_bundle,
    get_handoff_dir,
    import_note_file,
    import_note_from_data_uri,
    load_ai_config,
    load_questions_from_task_snapshot,
    load_workspace_from_json,
    offline_classify_and_fuse,
    parse_external_assistant_reply,
    probe_local_cli_agents,
    save_ai_config,
)
from ..core.kernels import (
    KERNEL_LABELS,
    cleanup_empty_legacy_output_dirs,
    detect_all_kernels,
    download_and_install_kernel,
    get_export_dir,
    get_preferred_kernel,
    load_settings,
    save_settings,
    set_export_dir,
    set_preferred_kernel,
)
from ..core.models import ChapterType, Practice, QuestionType
from ..core.tasks import engine
from ..core.web_capture import (
    DETECT_PAGE_STATUS_JS,
    EXTRACT_QUESTIONS_JS,
    PLATFORM_PRESETS,
    XHR_INTERCEPT_JS,
    build_questions_from_capture,
)


def _build_atelier_icon() -> QIcon:
    """绘制 Editorial Atelier 裁切十字标应用图标。"""
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor("#141413"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 4, 56, 56, 12, 12)
    pen = QPen(QColor("#FAF9F6"), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap)
    painter.setPen(pen)
    # 四角裁切线
    painter.drawLine(14, 14, 23, 14)
    painter.drawLine(14, 14, 14, 23)
    painter.drawLine(50, 14, 41, 14)
    painter.drawLine(50, 14, 50, 23)
    painter.drawLine(14, 50, 23, 50)
    painter.drawLine(14, 50, 14, 41)
    painter.drawLine(50, 50, 41, 50)
    painter.drawLine(50, 50, 50, 41)
    # 中心十字准星
    painter.drawLine(32, 22, 32, 42)
    painter.drawLine(22, 32, 42, 32)
    painter.end()
    return QIcon(pix)


def _resolve_web_index() -> Path:
    local = Path(__file__).resolve().parent / "web" / "index.html"
    if local.exists():
        return local
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "cctr_export" / "ui" / "web" / "index.html"
        if bundled.exists():
            return bundled
    raise FileNotFoundError("未找到桌面端前端资源 web/index.html")


def _task_artifact_path(t) -> Path | None:
    if getattr(t, "output_path", ""):
        recorded = Path(t.output_path)
        if recorded.exists():
            return recorded
    base = Path.home() / ".cctr_export" / "output"
    if t.export_type == ExportType.ZIP:
        p = base / f"task_{t.id}_categorized.zip"
    elif t.export_type == ExportType.IMG:
        p = base / f"task_{t.id}_images.zip"
    else:
        p = base / str(t.id) / "output.pdf"
    return p if p.exists() else None


def _fmt_size(p: Path | None) -> str:
    if p is None or not p.exists():
        return ""
    b = p.stat().st_size
    if b >= 1048576:
        return f"{b / 1048576:.1f} MB"
    return f"{max(1, b // 1024)} KB"


def get_saved_accounts() -> list[dict[str, str]]:
    """读取常用账号记忆（仅存学校代码与学号，绝不落盘密码）；未初始化时从历史任务去重导入。"""
    settings = load_settings()
    raw_list = settings.get("saved_accounts")
    accounts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(sch: str, usr: str):
        s, u = (sch or "").strip(), (usr or "").strip()
        if not s or not u or (s, u) in seen:
            return
        seen.add((s, u))
        accounts.append({"school": s, "username": u})

    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                _add(str(item.get("school", "")), str(item.get("username", "")))
    else:
        try:
            for t in get_all_tasks():
                _add(t.school_id, t.username)
                if len(accounts) >= 6:
                    break
            if accounts:
                save_settings({"saved_accounts": accounts})
        except Exception:
            pass
    return accounts[:6]


def remember_saved_account(school: str, username: str) -> list[dict[str, str]]:
    """置顶并持久化最近成功登录的账号（仅学校代码与学号，最多保留 6 条）。"""
    s, u = (school or "").strip(), (username or "").strip()
    if not s or not u:
        return get_saved_accounts()
    cur = [x for x in get_saved_accounts() if not (x["school"] == s and x["username"] == u)]
    updated = ([{"school": s, "username": u}] + cur)[:6]
    save_settings({"saved_accounts": updated})
    return updated


def remove_saved_account(school: str, username: str) -> list[dict[str, str]]:
    s, u = (school or "").strip(), (username or "").strip()
    updated = [x for x in get_saved_accounts() if not (x["school"] == s and x["username"] == u)]
    save_settings({"saved_accounts": updated})
    return updated


class _CaptchaWorker(QThread):
    done = Signal(str)

    def __init__(self, client: CCTRClient):
        super().__init__()
        self._client = client

    def run(self):
        try:
            ocr_ready = is_ocr_available()
            data = self._client.fetch_captcha()
            b64 = base64.b64encode(data).decode("ascii")
            # 先立刻把验证码图片推送给前端展示，再异步/连续完成 OCR（避免冷启动首帧久等）
            if not ocr_ready:
                self.done.emit(
                    json.dumps(
                        {
                            "ok": True,
                            "imageDataUri": f"data:image/png;base64,{b64}",
                            "code": "",
                            "ocrAvailable": False,
                        },
                        ensure_ascii=False,
                    )
                )
                return

            code = ""
            for attempt in range(3):
                if attempt > 0:
                    data = self._client.fetch_captcha()
                    b64 = base64.b64encode(data).decode("ascii")
                try:
                    cand = recognize_captcha(data, expected_len=4)
                except Exception:
                    cand = ""
                if len(cand) == 4:
                    code = cand
                    break
            self.done.emit(
                json.dumps(
                    {
                        "ok": True,
                        "imageDataUri": f"data:image/png;base64,{b64}",
                        "code": code,
                        "ocrAvailable": ocr_ready,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            self.done.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


class _LoginWorker(QThread):
    done = Signal(str)

    def __init__(self, client: CCTRClient, captcha: str, auto_retry_ocr: bool):
        super().__init__()
        self._client = client
        self._captcha = captcha
        self._auto_retry_ocr = auto_retry_ocr

    def run(self):
        captcha = self._captcha
        max_attempts = 3 if (self._auto_retry_ocr and is_ocr_available()) else 1
        last_err = ""
        for attempt in range(max_attempts):
            if attempt > 0:
                captcha = ""
                for _ in range(3):
                    try:
                        img = self._client.fetch_captcha()
                        cand = recognize_captcha(img, expected_len=4)
                        if len(cand) == 4:
                            captcha = cand
                            break
                    except Exception:
                        pass
                if len(captcha) != 4:
                    break
            try:
                self._client.login(captcha=captcha)
                practices = self._client.get_practice_list()
                payload = [
                    {
                        "id": p.id,
                        "courseid": p.courseid,
                        "practise_id": p.practise_id,
                        "teacher_id": p.teacher_id,
                        "lesson_name": p.lesson_name,
                        "practice_name": p.practice_name,
                        "teacher_name": p.teacher_name,
                        "finish_time": p.finish_time,
                        "question_count": p.question_count,
                        "json": p.to_json(),
                    }
                    for p in practices
                ]
                self.done.emit(json.dumps({"ok": True, "practices": payload}, ensure_ascii=False))
                return
            except Exception as e:
                last_err = str(e)
                if "验证码" not in last_err:
                    break
        self.done.emit(json.dumps({"ok": False, "error": last_err or "登录失败"}, ensure_ascii=False))


def _practice_payload(p: Practice) -> dict:
    return {
        "id": p.id,
        "courseid": p.courseid,
        "practise_id": p.practise_id,
        "teacher_id": p.teacher_id,
        "lesson_name": p.lesson_name,
        "practice_name": p.practice_name,
        "teacher_name": p.teacher_name,
        "finish_time": p.finish_time,
        "question_count": p.question_count,
        "json": p.to_json(),
    }


class _FilterWorker(QThread):
    done = Signal(str)

    def __init__(self, client: CCTRClient | None, practice: Practice):
        super().__init__()
        self._client = client
        self._practice = practice
        self._practice_id = str(practice.id)

    def run(self):
        try:
            if self._practice.courseid == "__local_bank__":
                chapters, qtypes = get_local_bank_filters(self._practice.practise_id)
            else:
                if self._client is None:
                    raise RuntimeError("请先登录融智云考")
                chapters, qtypes = self._client.get_chapter_and_question_type(self._practice)
            self.done.emit(
                json.dumps(
                    {
                        "ok": True,
                        "practiceId": self._practice_id,
                        "chapters": [c.to_dict() for c in chapters],
                        "question_types": [q.to_dict() for q in qtypes],
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            self.done.emit(json.dumps({"ok": False, "practiceId": self._practice_id, "chapters": [], "question_types": [], "error": str(e)}, ensure_ascii=False))


class _ImportFilesWorker(QThread):
    done = Signal(str)

    def __init__(self, file_paths: list[str]):
        super().__init__()
        self._file_paths = file_paths

    def run(self):
        loaded: list[dict] = []
        errors: list[str] = []
        for fp in self._file_paths:
            try:
                title, qs = import_questions_from_file(fp)
                src = f"本地导入 ({Path(fp).suffix.lstrip('.').upper()})"
                p = save_local_bank(title=title, source_label=src, questions=qs)
                loaded.append(_practice_payload(p))
            except Exception as e:
                errors.append(f"{Path(fp).name}: {e}")
        self.done.emit(json.dumps({"ok": bool(loaded), "practices": loaded, "errors": errors}, ensure_ascii=False))


class _CaptureDecryptWorker(QThread):
    done = Signal(str)

    def __init__(self, raw_json: str):
        super().__init__()
        self._raw_json = raw_json

    def run(self):
        try:
            payload = json.loads(self._raw_json)
            title, qs = build_questions_from_capture(payload)
            src = str(payload.get("sourceLabel") or payload.get("platform") or "网页捕获")
            p = save_local_bank(title=title, source_label=src, questions=qs)
            self.done.emit(json.dumps({
                "ok": True,
                "practice": _practice_payload(p),
                "title": title,
                "count": len(qs),
                "decryptedGlyphs": len(payload.get("secretGlyphs") or []),
            }, ensure_ascii=False))
        except Exception as e:
            self.done.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


class PlatformBrowserDialog(QDialog):
    """内置多平台登录与作业捕获浏览器（持久化 Cookie，支持学习通 font-cxsecret 与雨课堂字体还原）。"""

    _CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, parent=None, initial_key: str = "chaoxing"):
        super().__init__(parent)
        self.setWindowTitle("CCTR Atelier · 网页登录与题库捕获（学习通 / 雨课堂）")
        self.resize(1180, 800)
        self.captured_practices: list[dict] = []
        self._workers: list[QThread] = []
        self._direct_mode = True
        self._last_requested_url = ""
        self._showing_error_page = False
        self._is_loading = False
        self._nav_history: list[str] = []
        self._history_idx = -1

        # 打开内置浏览器时默认直连教育平台，避免本地 Clash/VPN 代理握手中断造成白屏
        self._apply_proxy_mode(direct=True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top_wrap = QWidget(self)
        top_wrap.setStyleSheet("background:#f4f3ef;border-bottom:1px solid #e2dfd7;")
        top_box = QVBoxLayout(top_wrap)
        top_box.setContentsMargins(12, 8, 12, 8)
        top_box.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        for _key, (label, url) in PLATFORM_PRESETS.items():
            btn = QPushButton(label, self)
            btn.setStyleSheet(
                "QPushButton{padding:5px 10px;border:1px solid #cbc7bd;border-radius:5px;background:#faf9f6;color:#141413;font-size:12px;}"
                "QPushButton:hover{background:#eae7e0;}"
            )
            btn.clicked.connect(lambda _chk=False, u=url: self._navigate(u))
            row1.addWidget(btn)

        row1.addStretch(1)

        self._extract_btn = QPushButton("提取本页题目", self)
        self._extract_btn.setStyleSheet(
            "QPushButton{padding:6px 14px;border:none;border-radius:5px;background:#1e6b4e;color:#faf9f6;font-weight:600;font-size:12px;}"
            "QPushButton:hover{background:#18573f;}"
            "QPushButton:disabled{opacity:0.5;background:#8a867e;}"
        )
        row1.addWidget(self._extract_btn)

        self._finish_btn = QPushButton("送入工作台 (0)", self)
        self._finish_btn.setEnabled(False)
        self._finish_btn.setStyleSheet(
            "QPushButton{padding:6px 14px;border:none;border-radius:5px;background:#141413;color:#faf9f6;font-weight:600;font-size:12px;}"
            "QPushButton:disabled{background:#cbc7bd;color:#57544e;}"
        )
        row1.addWidget(self._finish_btn)
        top_box.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)

        self._back_btn = QPushButton("←", self)
        self._back_btn.setFixedWidth(34)
        self._back_btn.setToolTip("返回上一页")
        self._back_btn.setEnabled(False)
        self._reload_btn = QPushButton("↻", self)
        self._reload_btn.setFixedWidth(34)
        self._reload_btn.setToolTip("强制停止并重新加载当前页面")
        for b in (self._back_btn, self._reload_btn):
            b.setStyleSheet(
                "QPushButton{padding:5px;border:1px solid #cbc7bd;border-radius:5px;background:#fff;color:#141413;}"
                "QPushButton:hover{background:#eae7e0;}"
                "QPushButton:disabled{color:#b6b2a9;background:#f4f3ef;}"
            )
        row2.addWidget(self._back_btn)
        row2.addWidget(self._reload_btn)

        self._url_edit = QLineEdit(self)
        self._url_edit.setPlaceholderText("输入或粘贴学习通/雨课堂/学校测验网址，回车访问…")
        self._url_edit.setStyleSheet("padding:5px 10px;border:1px solid #cbc7bd;border-radius:5px;background:#fff;color:#141413;font-size:12px;")
        row2.addWidget(self._url_edit, 1)

        self._proxy_btn = QPushButton("直连模式 ✓", self)
        self._proxy_btn.setToolTip("默认直连国内教务/题库站点以避开代理握手失败；如需校园代理可点击切换")
        self._proxy_btn.setStyleSheet(
            "QPushButton{padding:5px 10px;border:1px solid #b5cfc4;border-radius:5px;background:#eef6f2;color:#1e6b4e;font-size:12px;font-weight:600;}"
            "QPushButton:hover{background:#dceee5;}"
        )
        self._proxy_btn.clicked.connect(self._toggle_proxy_mode)
        row2.addWidget(self._proxy_btn)

        self._status_lbl = QLabel("正在连接平台页面…", self)
        self._status_lbl.setMinimumWidth(250)
        self._status_lbl.setStyleSheet("color:#57544e;font-size:12px;padding:0 4px;")
        row2.addWidget(self._status_lbl)

        top_box.addLayout(row2)
        root.addWidget(top_wrap)

        self._progress = QProgressBar(self)
        self._progress.setFixedHeight(3)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 100)
        self._progress.setStyleSheet(
            "QProgressBar{border:none;background:#eae7e0;}"
            "QProgressBar::chunk{background:#1e6b4e;}"
        )
        root.addWidget(self._progress)

        profile_dir = Path.home() / ".cctr_export" / "browser_profile"
        (profile_dir / "storage").mkdir(parents=True, exist_ok=True)
        (profile_dir / "cache").mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("cctr_atelier_browser", self)
        self._profile.setHttpUserAgent(self._CHROME_UA)
        self._profile.setPersistentStoragePath(str(profile_dir / "storage"))
        self._profile.setCachePath(str(profile_dir / "cache"))
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)

        hook = QWebEngineScript()
        hook.setName("cctr_xhr_intercept")
        hook.setSourceCode(XHR_INTERCEPT_JS)
        hook.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        hook.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        hook.setRunsOnSubFrames(True)
        self._profile.scripts().insert(hook)

        self._page = QWebEnginePage(self._profile, self)
        self._web = QWebEngineView(self)
        self._web.setPage(self._page)
        s = self._web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        root.addWidget(self._web, 1)

        self._back_btn.clicked.connect(self._on_back_clicked)
        self._reload_btn.clicked.connect(self._on_reload_clicked)
        self._url_edit.returnPressed.connect(lambda: self._navigate(self._url_edit.text().strip()))
        self._web.urlChanged.connect(self._on_url_changed)
        self._web.loadStarted.connect(self._on_load_started)
        self._web.loadProgress.connect(self._on_load_progress)
        self._web.loadFinished.connect(self._on_load_finished)
        self._extract_btn.clicked.connect(self._on_extract_clicked)
        self._finish_btn.clicked.connect(self.accept)

        self._load_watchdog = QTimer(self)
        self._load_watchdog.setSingleShot(True)
        self._load_watchdog.timeout.connect(self._on_load_timeout)

        self._probe_timer = QTimer(self)
        self._probe_timer.timeout.connect(self._probe_page)
        self._probe_timer.start(1500)

        _, start_url = PLATFORM_PRESETS.get(initial_key, PLATFORM_PRESETS["chaoxing"])
        self._navigate(start_url)

    def _apply_proxy_mode(self, direct: bool) -> None:
        self._direct_mode = direct
        if direct:
            QNetworkProxyFactory.setUseSystemConfiguration(False)
            QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        else:
            QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.DefaultProxy))
            QNetworkProxyFactory.setUseSystemConfiguration(True)

    def _toggle_proxy_mode(self) -> None:
        self._web.stop()
        self._apply_proxy_mode(not self._direct_mode)
        self._profile.clearHttpCache()
        if self._direct_mode:
            self._proxy_btn.setText("直连模式 ✓")
            self._proxy_btn.setStyleSheet(
                "QPushButton{padding:5px 10px;border:1px solid #b5cfc4;border-radius:5px;background:#eef6f2;color:#1e6b4e;font-size:12px;font-weight:600;}"
            )
        else:
            self._proxy_btn.setText("系统代理")
            self._proxy_btn.setStyleSheet(
                "QPushButton{padding:5px 10px;border:1px solid #d7c39a;border-radius:5px;background:#fdf6e9;color:#925911;font-size:12px;font-weight:600;}"
            )
        target = self._last_requested_url or self._url_edit.text().strip()
        if target:
            self._navigate(target)

    def _record_history(self, url_str: str) -> None:
        if not url_str or url_str.startswith(("data:", "about:", "chrome-error:", "https://localhost/cctr-error")):
            return
        if self._history_idx >= 0 and self._nav_history[self._history_idx] == url_str:
            self._sync_nav_buttons()
            return
        if self._history_idx < len(self._nav_history) - 1:
            self._nav_history = self._nav_history[: self._history_idx + 1]
        self._nav_history.append(url_str)
        self._history_idx = len(self._nav_history) - 1
        self._sync_nav_buttons()

    def _sync_nav_buttons(self) -> None:
        can_back = self._web.history().canGoBack() or self._history_idx > 0
        self._back_btn.setEnabled(can_back)

    def _on_back_clicked(self) -> None:
        self._web.stop()
        if self._history_idx > 0:
            self._history_idx -= 1
            prev_url = self._nav_history[self._history_idx]
            self._navigate(prev_url, record=False)
            return
        if self._web.history().canGoBack():
            self._showing_error_page = False
            self._web.back()
            self._sync_nav_buttons()

    def _on_reload_clicked(self) -> None:
        self._web.stop()
        cur = self._web.url().toString()
        target = self._last_requested_url or self._url_edit.text().strip()
        if self._showing_error_page or not cur or cur.startswith(("data:", "about:", "chrome-error:", "https://localhost/cctr-error")):
            if target:
                self._navigate(target, record=False)
            return
        self._showing_error_page = False
        self._is_loading = True
        self._load_watchdog.start(15000)
        self._progress.setVisible(True)
        self._progress.setValue(12)
        self._status_lbl.setText("正在重新加载页面…")
        self._status_lbl.setStyleSheet("color:#57544e;font-size:12px;padding:0 4px;")
        self._web.triggerPageAction(QWebEnginePage.WebAction.ReloadAndBypassCache)

    def _on_load_timeout(self) -> None:
        if self._is_loading and not self._showing_error_page:
            self._status_lbl.setText("⏳ 加载较慢（15s），可点右侧切换直连/代理或点 ↻ 重试")
            self._status_lbl.setStyleSheet("color:#925911;font-weight:600;font-size:12px;padding:0 4px;")

    def _navigate(self, raw_url: str, *, record: bool = True) -> None:
        target = (raw_url or "").strip()
        if not target:
            return
        u = QUrl.fromUserInput(target)
        url_str = u.toString()
        self._web.stop()
        self._showing_error_page = False
        self._is_loading = True
        self._last_requested_url = url_str
        self._url_edit.setText(url_str)
        if record:
            self._record_history(url_str)
        else:
            self._sync_nav_buttons()
        self._progress.setVisible(True)
        self._progress.setValue(10)
        self._status_lbl.setText("正在加载网页，请稍候…")
        self._status_lbl.setStyleSheet("color:#57544e;font-size:12px;padding:0 4px;")
        self._load_watchdog.start(15000)
        self._web.load(u)

    def _on_url_changed(self, url: QUrl) -> None:
        s = url.toString()
        if not s or s.startswith(("data:", "about:", "chrome-error:", "https://localhost/cctr-error")):
            return
        self._last_requested_url = s
        self._record_history(s)
        if not self._url_edit.hasFocus():
            self._url_edit.setText(s)

    def _on_load_started(self) -> None:
        if not self._showing_error_page:
            self._is_loading = True
            self._progress.setVisible(True)
            self._progress.setValue(12)
            self._status_lbl.setText("正在加载网页，请稍候…")
            self._status_lbl.setStyleSheet("color:#57544e;font-size:12px;padding:0 4px;")
            self._load_watchdog.start(15000)

    def _on_load_progress(self, pct: int) -> None:
        if not self._showing_error_page:
            self._progress.setVisible(True)
            self._progress.setValue(max(5, pct))

    def _on_load_finished(self, ok: bool) -> None:
        self._is_loading = False
        self._load_watchdog.stop()
        self._progress.setVisible(False)
        self._sync_nav_buttons()
        if ok:
            self._showing_error_page = False
            return
        if self._showing_error_page:
            return
        self._showing_error_page = True
        failed_url = self._last_requested_url or self._url_edit.text().strip()
        mode_hint = "当前为「直连模式」" if self._direct_mode else "当前为「系统代理模式」"
        self._status_lbl.setText("✗ 页面加载失败，请切换代理或检查网络后点击 ↻ 重试")
        self._status_lbl.setStyleSheet("color:#c83e2b;font-weight:600;font-size:12px;padding:0 4px;")
        safe_url = failed_url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        err_html = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>"
            "body{margin:0;padding:48px 28px;font-family:'Microsoft YaHei UI',sans-serif;background:#faf9f6;color:#141413;}"
            ".card{max-width:620px;margin:0 auto;background:#fff;border:1px solid #e2dfd7;border-radius:10px;padding:28px;}"
            "h2{margin:0 0 10px;font-size:18px;color:#b91c1c;}p{margin:8px 0;font-size:13.5px;line-height:1.7;color:#44403c;}"
            "code{background:#f4f3ef;padding:2px 6px;border-radius:4px;word-break:break-all;}"
            "</style></head><body><div class='card'>"
            "<h2>无法打开当前网页</h2>"
            f"<p>目标地址：<code>{safe_url}</code>（{mode_hint}）</p>"
            "<p>可能原因：<br>1. 本机代理或防火墙拦截了教育平台 HTTPS 握手；可点击上方地址栏右侧的「直连模式 / 系统代理」按钮切换后重试。<br>"
            "2. 未连接校园网 / VPN 或网页暂时超时；也可先用浏览器导出/保存，或在主界面选择「本地导入题库」。</p>"
            "<p>请点击上方 <b>↻ 刷新</b> 或切换顶部平台按钮重试。</p>"
            "</div></body></html>"
        )
        self._web.setHtml(err_html, QUrl("https://localhost/cctr-error"))

    def _probe_page(self):
        if not self.isVisible() or self._showing_error_page:
            return
        self._page.runJavaScript(DETECT_PAGE_STATUS_JS, self._on_probe_result)

    def _on_probe_result(self, raw):
        try:
            info = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            return
        cnt = int(info.get("estCount") or 0)
        hint = info.get("platformHint") or "网页"
        if cnt > 0:
            self._status_lbl.setText(f"✓ 检测到 {hint} 约 {cnt} 道题，可点击「提取本页题目」")
            self._status_lbl.setStyleSheet("color:#1e6b4e;font-weight:600;font-size:12px;padding:0 6px;")
        else:
            self._status_lbl.setText(f"{hint} · 请进入具体作业/测验/练习页后提取")
            self._status_lbl.setStyleSheet("color:#57544e;font-size:12px;padding:0 6px;")

    def _on_extract_clicked(self):
        self._extract_btn.setEnabled(False)
        self._extract_btn.setText("提取与字形解密中…")
        self._page.runJavaScript(EXTRACT_QUESTIONS_JS, self._on_js_extracted)

    def _on_js_extracted(self, raw):
        if not isinstance(raw, str):
            self._extract_btn.setEnabled(True)
            self._extract_btn.setText("提取本页题目")
            self._status_lbl.setText("✗ 页面未返回有效数据，请刷新后重试")
            self._status_lbl.setStyleSheet("color:#c83e2b;font-weight:600;font-size:12px;padding:0 6px;")
            return
        w = _CaptureDecryptWorker(raw)
        self._workers.append(w)
        w.done.connect(self._on_decrypt_done)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        w.start()

    def _on_decrypt_done(self, res_json: str):
        self._extract_btn.setEnabled(True)
        self._extract_btn.setText("提取本页题目")
        try:
            res = json.loads(res_json)
        except Exception:
            res = {"ok": False, "error": "解析失败"}
        if not res.get("ok"):
            self._status_lbl.setText(f"✗ {res.get('error') or '提取失败'}")
            self._status_lbl.setStyleSheet("color:#c83e2b;font-weight:600;font-size:12px;padding:0 6px;")
            return
        self.captured_practices.append(res["practice"])
        g_cnt = int(res.get("decryptedGlyphs") or 0)
        extra = f"（已自动还原 {g_cnt} 个加密字形）" if g_cnt > 0 else ""
        self._status_lbl.setText(f"✓ 已加入「{res['title']}」{res['count']} 题{extra}")
        self._status_lbl.setStyleSheet("color:#1e6b4e;font-weight:600;font-size:12px;padding:0 6px;")
        self._finish_btn.setEnabled(True)
        self._finish_btn.setText(f"送入工作台 ({len(self.captured_practices)})")

    def _cleanup_browser(self) -> None:
        if getattr(self, "_cleaned_up", False):
            return
        self._cleaned_up = True
        self._load_watchdog.stop()
        self._probe_timer.stop()
        for w in list(self._workers):
            w.quit()
            w.wait(1500)
        self._web.stop()
        self._web.setPage(None)
        self._page.deleteLater()
        self._profile.deleteLater()
        self._apply_proxy_mode(direct=True)

    def done(self, r: int) -> None:
        self._cleanup_browser()
        super().done(r)

    def closeEvent(self, event):
        self._cleanup_browser()
        super().closeEvent(event)


class _DroppedFilesWorker(QThread):
    """后台智能分类解析拖拽入窗口的本地题库/笔记/知识图谱文件。"""

    done = Signal(str, object)

    def __init__(self, paths: list[str], zone: str):
        super().__init__()
        self._paths = list(paths)
        self._zone = zone

    def run(self):
        try:
            if self._zone == "workspace":
                practices = []
                errors = []
                for fp in _expand_dropped_paths(self._paths):
                    try:
                        title, qs = import_questions_from_file(fp)
                        src = f"拖拽导入 ({fp.suffix.lstrip('.').upper()})"
                        p = save_local_bank(title=title, source_label=src, questions=qs)
                        practices.append(_practice_payload(p))
                    except Exception as e:
                        errors.append(f"{fp.name}: {e}")
                payload = {
                    "ok": bool(practices),
                    "practices": practices,
                    "sourceLabel": "拖拽导入题库",
                    "errors": errors,
                    "error": "; ".join(errors),
                }
                self.done.emit(self._zone, payload)
            else:
                res = classify_and_import_dropped_paths(self._paths, zone_hint=self._zone)
                self.done.emit(self._zone, res)
        except Exception as e:
            self.done.emit(self._zone, {"ok": False, "error": str(e), "errors": [str(e)]})


_FILL_CHAT_PROMPT_JS = """(function(promptText) {
  const candidates = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], [role="textbox"]'))
    .filter(el => el.offsetParent !== null && !el.disabled && !el.readOnly);
  const el = candidates.sort((a, b) => (b.getBoundingClientRect().top || 0) - (a.getBoundingClientRect().top || 0))[0];
  if (!el) return JSON.stringify({filled: false});
  el.focus();
  if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
    if (setter) setter.call(el, promptText); else el.value = promptText;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  } else {
    el.innerText = promptText;
    el.dispatchEvent(new InputEvent('input', {bubbles: true, data: promptText}));
  }
  return JSON.stringify({filled: true});
})"""

_EXTRACT_ASSISTANT_REPLY_JS = """(function() {
  const pres = Array.from(document.querySelectorAll('pre, code'))
    .map(n => (n.innerText || n.textContent || '').trim())
    .filter(t => t.includes('"topics"') || t.includes('"name"'));
  if (pres.length) return pres[pres.length - 1];
  const sels = [
    '[data-role="assistant"]', '.assistant-message', '.markdown-body', '.tongyi-markdown',
    '[class*="markdown"]', '[class*="message-content"]', '[class*="hyc-content"]'
  ];
  for (const s of sels) {
    const nodes = Array.from(document.querySelectorAll(s))
      .map(n => (n.innerText || '').trim())
      .filter(t => t.length > 30);
    if (nodes.length) return nodes[nodes.length - 1];
  }
  const sel = (window.getSelection && String(window.getSelection() || '').trim()) || '';
  return sel;
})()"""


class AiWebAssistantDialog(QDialog):
    """免 API 内置大模型助手工作台（支持通义千问 / 腾讯元宝·WorkBuddy / DeepSeek / Kimi / 豆包）。"""

    replyCaptured = Signal(str, str)  # raw_reply, assistant_label
    _CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, parent=None, *, initial_key: str = "qianwen", handoff_info: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("CCTR Atelier · 免 API 千问 / WorkBuddy / AI 助手工作台")
        self.resize(1240, 820)
        self._handoff = handoff_info or {}
        self._direct_mode = True
        self._current_label = WEB_ASSISTANT_PRESETS.get(initial_key, WEB_ASSISTANT_PRESETS["qianwen"])[0]
        self._apply_proxy_mode(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        top_wrap = QWidget(self)
        top_wrap.setStyleSheet("background:#f4f3ef;border-bottom:1px solid #d8d4ca;")
        top_box = QVBoxLayout(top_wrap)
        top_box.setContentsMargins(10, 8, 10, 8)
        top_box.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        for key, (label, url) in WEB_ASSISTANT_PRESETS.items():
            short = label.split("（")[0]
            btn = QPushButton(short, self)
            btn.setStyleSheet(
                "QPushButton{padding:5px 10px;border:1px solid #cbc7bd;border-radius:5px;background:#fff;color:#141413;font-size:12px;}"
                "QPushButton:hover{background:#eae7e0;}"
            )
            btn.clicked.connect(lambda _chk=False, u=url, lbl=short: self._switch_site(u, lbl))
            row1.addWidget(btn)
        row1.addStretch(1)

        self._fill_btn = QPushButton("① 填入/复制题目指令", self)
        self._fill_btn.setStyleSheet(
            "QPushButton{padding:6px 12px;border:1px solid #cbc7bd;border-radius:5px;background:#fff;color:#141413;font-weight:600;font-size:12px;}"
            "QPushButton:hover{background:#eae7e0;}"
        )
        self._fill_btn.clicked.connect(self._fill_and_copy_prompt)
        row1.addWidget(self._fill_btn)

        self._dir_btn = QPushButton("📂 WorkBuddy 任务包", self)
        self._dir_btn.setStyleSheet(
            "QPushButton{padding:6px 11px;border:1px solid #d7c39a;border-radius:5px;background:#fdf6e9;color:#925911;font-weight:600;font-size:12px;}"
        )
        self._dir_btn.clicked.connect(self._open_handoff_folder)
        row1.addWidget(self._dir_btn)

        self._extract_btn = QPushButton("② 提取 AI 回复并生成考点树 ✨", self)
        self._extract_btn.setStyleSheet(
            "QPushButton{padding:6px 14px;border:none;border-radius:5px;background:#1e6b4e;color:#faf9f6;font-weight:700;font-size:12px;}"
            "QPushButton:hover{background:#18573f;}"
        )
        self._extract_btn.clicked.connect(self._extract_reply)
        row1.addWidget(self._extract_btn)
        top_box.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self._reload_btn = QPushButton("↻ 刷新", self)
        self._reload_btn.setStyleSheet("QPushButton{padding:4px 10px;border:1px solid #cbc7bd;border-radius:5px;background:#fff;font-size:12px;}")
        row2.addWidget(self._reload_btn)
        self._url_edit = QLineEdit(self)
        self._url_edit.setStyleSheet("padding:4px 8px;border:1px solid #cbc7bd;border-radius:5px;background:#fff;font-size:12px;")
        row2.addWidget(self._url_edit, 1)
        self._proxy_btn = QPushButton("直连模式 ✓", self)
        self._proxy_btn.setStyleSheet("QPushButton{padding:4px 10px;border:1px solid #b5cfc4;border-radius:5px;background:#eef6f2;color:#1e6b4e;font-size:12px;font-weight:600;}")
        self._proxy_btn.clicked.connect(self._toggle_proxy_mode)
        row2.addWidget(self._proxy_btn)
        self._status_lbl = QLabel("已将整理指令复制到剪贴板：登录后可直接粘贴（或点『① 填入指令』），回复完成后点『② 提取 AI 回复』", self)
        self._status_lbl.setStyleSheet("color:#1e6b4e;font-size:12px;font-weight:600;padding:0 4px;")
        row2.addWidget(self._status_lbl)
        top_box.addLayout(row2)
        root.addWidget(top_wrap)

        profile_dir = Path.home() / ".cctr_export" / "web_ai_profile"
        (profile_dir / "storage").mkdir(parents=True, exist_ok=True)
        (profile_dir / "cache").mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("cctr_ai_assistant_v5", self)
        self._profile.setHttpUserAgent(self._CHROME_UA)
        self._profile.setPersistentStoragePath(str(profile_dir / "storage"))
        self._profile.setCachePath(str(profile_dir / "cache"))
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)

        self._page = QWebEnginePage(self._profile, self)
        self._web = QWebEngineView(self)
        self._web.setPage(self._page)
        s = self._web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        root.addWidget(self._web, 1)

        self._reload_btn.clicked.connect(lambda: self._web.triggerPageAction(QWebEnginePage.WebAction.ReloadAndBypassCache))
        self._url_edit.returnPressed.connect(lambda: self._web.load(QUrl.fromUserInput(self._url_edit.text().strip())))
        self._web.urlChanged.connect(lambda u: self._url_edit.setText(u.toString()))

        _, start_url = WEB_ASSISTANT_PRESETS.get(initial_key, WEB_ASSISTANT_PRESETS["qianwen"])
        self._url_edit.setText(start_url)
        self._web.load(QUrl(start_url))

    def _apply_proxy_mode(self, direct: bool) -> None:
        self._direct_mode = direct
        if direct:
            QNetworkProxyFactory.setUseSystemConfiguration(False)
            QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        else:
            QNetworkProxy.setApplicationProxy(QNetworkProxy(QNetworkProxy.ProxyType.DefaultProxy))
            QNetworkProxyFactory.setUseSystemConfiguration(True)

    def _toggle_proxy_mode(self) -> None:
        self._apply_proxy_mode(not self._direct_mode)
        self._proxy_btn.setText("直连模式 ✓" if self._direct_mode else "系统代理")
        self._web.reload()

    def _switch_site(self, url: str, label: str) -> None:
        self._current_label = label
        self._url_edit.setText(url)
        self._web.load(QUrl(url))

    def _fill_and_copy_prompt(self) -> None:
        prompt = str(self._handoff.get("prompt_text") or "")
        if not prompt:
            return
        QApplication.clipboard().setText(prompt)
        js = f"({_FILL_CHAT_PROMPT_JS})({json.dumps(prompt, ensure_ascii=False)})"
        self._page.runJavaScript(js, self._on_fill_result)

    def _on_fill_result(self, raw) -> None:
        try:
            res = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            res = {}
        if res.get("filled"):
            self._status_lbl.setText("✓ 已自动填入网页输入框（并同步复制到剪贴板），请点击发送给 AI！")
        else:
            self._status_lbl.setText("✓ 完整整理指令已在剪贴板：请在下方对话框按 Ctrl+V 粘贴并发送！")

    def _open_handoff_folder(self) -> None:
        hdir = Path(str(self._handoff.get("handoff_dir") or get_handoff_dir()))
        if os.name == "nt":
            os.startfile(str(hdir))
        else:
            subprocess.Popen(["xdg-open", str(hdir)])
        self._status_lbl.setText(f"✓ 已打开任务包目录：{hdir.name}（可将其中 .md 与板书图直接拖入 WorkBuddy）")

    def _extract_reply(self) -> None:
        self._page.runJavaScript(_EXTRACT_ASSISTANT_REPLY_JS, self._on_reply_extracted)

    def _on_reply_extracted(self, raw) -> None:
        text = str(raw or "").strip()
        if not text:
            text = (QApplication.clipboard().text() or "").strip()
        if not text:
            self._status_lbl.setText("✗ 暂未提取到回复：请等 AI 输出完毕后再点『复制回复』或直接点击此按钮")
            return
        self.replyCaptured.emit(text, self._current_label)
        self.accept()

    def _cleanup(self) -> None:
        if getattr(self, "_cleaned", False):
            return
        self._cleaned = True
        self._web.stop()
        self._web.setPage(None)
        self._page.deleteLater()
        self._profile.deleteLater()
        self._apply_proxy_mode(direct=True)

    def done(self, r: int) -> None:
        self._cleanup()
        super().done(r)

    def closeEvent(self, event):
        self._cleanup()
        super().closeEvent(event)


class _AtelierWebView(QWebEngineView):
    """支持 Windows 资源管理器原生拖拽文件/文件夹导入，禁止意外跳转 file:// 页面。"""

    def __init__(self, parent, bridge: "DesktopBridge"):
        super().__init__(parent)
        self._bridge = bridge
        self.setAcceptDrops(True)

    @staticmethod
    def _extract_local_paths(mime) -> list[str]:
        if not mime or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, event):
        paths = self._extract_local_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.page().runJavaScript("window._setDragOverlay && window._setDragOverlay(true);")
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._extract_local_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.page().runJavaScript("window._setDragOverlay && window._setDragOverlay(false);")
        event.accept()

    def dropEvent(self, event):
        self.page().runJavaScript("window._setDragOverlay && window._setDragOverlay(false);")
        paths = self._extract_local_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        self.page().runJavaScript(
            f"(function(){{ return (window._resolveDropTarget ? window._resolveDropTarget({int(pos.x())}, {int(pos.y())}) : 'auto'); }})()",
            lambda zone: self._bridge.handleDroppedPaths(paths, str(zone or "auto")),
        )


class _KernelWorker(QThread):
    progress = Signal(int, str)
    done = Signal(str)

    def __init__(self, kind: str):
        super().__init__()
        self._kind = kind

    def run(self):
        try:
            info = download_and_install_kernel(
                self._kind,
                progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
            )
            self.done.emit(
                json.dumps(
                    {"ok": True, "message": f"{info.name} ({info.version}) 已就绪", "kind": info.kind},
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            self.done.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


class _AiProbeWorker(QThread):
    done = Signal(str)

    def __init__(self, base_url: str):
        super().__init__()
        self._base_url = base_url

    def run(self):
        try:
            models = MultimodalModelClient.probe_ollama_models(self._base_url)
            self.done.emit(json.dumps({"ok": True, "models": models}, ensure_ascii=False))
        except Exception as e:
            self.done.emit(json.dumps({"ok": False, "error": str(e), "models": []}, ensure_ascii=False))


class _AiTestWorker(QThread):
    done = Signal(str)

    def __init__(self, profile: ModelProfile):
        super().__init__()
        self._profile = profile

    def run(self):
        try:
            reply = MultimodalModelClient(self._profile).chat("请只回复：连接正常", timeout=25.0)
            self.done.emit(json.dumps({"ok": True, "reply": reply[:120]}, ensure_ascii=False))
        except Exception as e:
            self.done.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


class _AiLoadPracticesWorker(QThread):
    done = Signal(str, list, str)

    def __init__(self, client: CCTRClient | None, practices_json: list[str]):
        super().__init__()
        self._client = client
        self._practices_json = practices_json

    def run(self):
        try:
            all_qs = []
            titles = []
            for pj in self._practices_json:
                p = Practice.from_json(pj)
                titles.append(p.lesson_name or p.practice_name)
                if p.courseid == "__local_bank__":
                    all_qs.extend(load_local_bank_questions(p.practise_id))
                else:
                    if self._client is None or not self._client.key:
                        raise RuntimeError("融智云考会话已失效，请先登录或使用已完成导出任务快照")
                    all_qs.extend(self._client.query_questions(p))
            title = f"{titles[0]} 等{len(titles)}项" if len(titles) > 1 else (titles[0] if titles else "工作台题库")
            self.done.emit(title, all_qs, "")
        except Exception as e:
            self.done.emit("", [], str(e))


class _AiNoteAnalyzeWorker(QThread):
    done = Signal(str, object, str)

    def __init__(self, note: NoteItem, profile: ModelProfile):
        super().__init__()
        self._note = note
        self._profile = profile

    def run(self):
        try:
            updated = analyze_note_item(self._note, MultimodalModelClient(self._profile))
            self.done.emit(updated.id, updated, "")
        except Exception as e:
            self.done.emit(self._note.id, None, str(e))


class _AiFusionWorker(QThread):
    progress = Signal(int, str)
    done = Signal(object, str)

    def __init__(self, questions: list, notes: list[NoteItem], profile: ModelProfile, mode: str, title: str):
        super().__init__()
        self._questions = list(questions)
        self._notes = list(notes)
        self._profile = profile
        self._mode = mode
        self._title = title

    def run(self):
        try:
            if self._mode == "offline":
                self.progress.emit(50, "正在按章节与课堂笔记关键词离线智能聚类…")
                ws = offline_classify_and_fuse(self._questions, self._notes, title=self._title)
                self.progress.emit(100, "离线考点分类完成")
            else:
                ws = ai_classify_and_fuse(
                    self._questions,
                    self._notes,
                    self._profile,
                    title=self._title,
                    progress_cb=lambda pct, msg: self.progress.emit(pct, msg),
                )
            self.done.emit(ws, "")
        except Exception as e:
            self.done.emit(None, str(e))


class _AiExportWorker(QThread):
    done = Signal(str)

    def __init__(self, workspace: KnowledgeWorkspace, opts: dict):
        super().__init__()
        self._ws = workspace
        self._opts = opts

    def run(self):
        try:
            paths = export_knowledge_bundle(
                self._ws,
                outputs=list(self._opts.get("outputs") or ["fused_pdf"]),
                variant=str(self._opts.get("variant") or "withAnswersRed"),
                layout_mode=str(self._opts.get("layoutMode") or "inline"),
                smart_pagination=bool(self._opts.get("smartPagination", True)),
                include_notes=bool(self._opts.get("includeNotes", True)),
                include_questions=bool(self._opts.get("includeQuestions", True)),
                include_note_images=bool(self._opts.get("includeNoteImages", True)),
            )
            self.done.emit(
                json.dumps(
                    {
                        "ok": True,
                        "paths": paths,
                        "files": [{"path": p, "name": Path(p).name} for p in paths],
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            self.done.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))


class DesktopBridge(QObject):
    captchaReady = Signal(str)
    loginDone = Signal(str)
    filtersReady = Signal(str)
    externalBanksLoaded = Signal(str)
    stateChanged = Signal(str)
    kernelProgress = Signal(int, str)
    kernelDone = Signal(str)
    windowStateChanged = Signal(bool)
    aiStateUpdated = Signal(str)
    aiProbeDone = Signal(str)
    aiTestDone = Signal(str)
    aiProgress = Signal(int, str)
    aiFusionDone = Signal(str)
    aiExportDone = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._client: CCTRClient | None = None
        self._workers: list[QThread] = []
        self._last_state_json = ""
        self._ai_ws = KnowledgeWorkspace()
        try:
            cleanup_empty_legacy_output_dirs()
        except Exception:
            pass
        # 后台预热 OCR 模型，打开界面后验证码秒级填充
        if is_ocr_available():
            threading.Thread(target=lambda: recognize_captcha(b""), daemon=True).start()

    def stop_workers(self):
        for w in list(self._workers):
            try:
                w.quit()
                w.wait(2000)
            except Exception:
                pass

    def _keep_worker(self, w: QThread):
        self._workers.append(w)
        w.finished.connect(lambda: self._workers.remove(w) if w in self._workers else None)
        w.start()

    def _ensure_client(self) -> CCTRClient:
        if self._client is None:
            self._client = CCTRClient("", "", "")
        return self._client

    def _kernels_payload(self) -> dict:
        all_k = detect_all_kernels()
        return {
            "preferred": get_preferred_kernel(),
            "labels": [{"key": k, "label": lbl} for k, lbl in KERNEL_LABELS],
            "items": {
                k: {
                    "kind": v.kind,
                    "name": v.name,
                    "installed": v.installed,
                    "path": v.path,
                    "version": v.version,
                    "source": v.source,
                    "can_download": v.can_download,
                }
                for k, v in all_k.items()
            },
        }

    def _tasks_and_logs_payload(self) -> dict:
        tasks = get_all_tasks()
        logs = get_all_logs(limit=120)
        task_list = []
        for t in tasks:
            art = _task_artifact_path(t)
            total_q = 0
            first_title = ""
            for idx_c, cfg in enumerate(t.config):
                try:
                    p = Practice.from_json(cfg["practice"])
                    if idx_c == 0:
                        first_title = p.lesson_name or p.practice_name
                    if p.question_count:
                        total_q += p.question_count
                except Exception:
                    pass
            if len(t.config) > 1 and first_title:
                summary_title = f"{first_title} 等{len(t.config)}项"
            else:
                summary_title = first_title or f"任务 #{t.id}"
            task_list.append(
                {
                    "id": t.id,
                    "title": summary_title,
                    "username": t.username,
                    "school_id": t.school_id,
                    "status": t.status.value,
                    "export_type": t.export_type.value,
                    "create_time": t.create_time[:19].replace("T", " "),
                    "file_path": str(art) if art else "",
                    "file_name": art.name if art else "",
                    "file_size": _fmt_size(art),
                    "question_count": total_q,
                    "has_snapshot": (Path.home() / ".cctr_export" / "task_questions" / f"{t.id}.json").exists(),
                }
            )
        return {"tasks": task_list, "logs": logs}

    @Slot(result=str)
    def getInitialState(self) -> str:
        settings = load_settings()
        payload = {
            "version": "5.2.0",
            "ocrAvailable": is_ocr_available(),
            "darkMode": bool(settings.get("dark_mode", False)),
            "exportDir": str(get_export_dir()),
            "savedAccounts": get_saved_accounts(),
            "kernels": self._kernels_payload(),
            "plugins": list_registered_plugins(),
            "aiConfig": load_ai_config().to_dict(),
            "aiPresets": [{"key": k, **v} for k, v in PROVIDER_PRESETS.items()],
            "aiWebPresets": [{"key": k, "label": v[0], "url": v[1]} for k, v in WEB_ASSISTANT_PRESETS.items()],
            "aiCliAgents": probe_local_cli_agents(),
            "aiWorkspace": self._ai_ws_payload(),
            **self._tasks_and_logs_payload(),
        }
        self._last_state_json = json.dumps(self._tasks_and_logs_payload(), ensure_ascii=False)
        return json.dumps(payload, ensure_ascii=False)

    def poll_state(self, force: bool = False):
        data = self._tasks_and_logs_payload()
        raw = json.dumps(data, ensure_ascii=False)
        if force or raw != self._last_state_json:
            self._last_state_json = raw
            self.stateChanged.emit(raw)

    @Slot()
    def fetchCaptcha(self):
        w = _CaptchaWorker(self._ensure_client())
        w.done.connect(self.captchaReady.emit)
        self._keep_worker(w)

    @Slot(str, str, str, str, bool)
    def login(self, school: str, username: str, password: str, captcha: str, auto_retry_ocr: bool):
        c = self._ensure_client()
        c.school_id = school.strip()
        c.username = username.strip()
        c.password = password.strip()
        w = _LoginWorker(c, captcha.strip(), auto_retry_ocr)

        def on_done(raw: str):
            try:
                data = json.loads(raw)
                if data.get("ok"):
                    data["savedAccounts"] = remember_saved_account(c.school_id, c.username)
                    raw = json.dumps(data, ensure_ascii=False)
            except Exception:
                pass
            self.loginDone.emit(raw)

        w.done.connect(on_done)
        self._keep_worker(w)

    @Slot(str, str, result=str)
    def removeSavedAccount(self, school: str, username: str) -> str:
        return json.dumps({"savedAccounts": remove_saved_account(school, username)}, ensure_ascii=False)

    @Slot(str)
    def openPlatformBrowser(self, platform_key: str):
        # 脱离 QWebChannel 同步 IPC 调用栈异步打开窗口，防止 Chromium 浏览器内核死锁导致白屏无法加载
        key = platform_key or "chaoxing"
        QTimer.singleShot(0, lambda k=key: self._show_platform_browser(k))

    def _show_platform_browser(self, platform_key: str):
        existing = getattr(self, "_platform_dlg", None)
        if existing is not None:
            try:
                if existing.isVisible():
                    _, url = PLATFORM_PRESETS.get(platform_key, PLATFORM_PRESETS["chaoxing"])
                    existing._navigate(url)
                    existing.raise_()
                    existing.activateWindow()
                    return
            except Exception:
                pass
        parent_w = self._main_win()
        dlg = PlatformBrowserDialog(parent_w, initial_key=platform_key)
        dlg.setModal(False)
        self._platform_dlg = dlg

        def _on_finished(_res: int):
            if getattr(dlg, "captured_practices", None):
                self.externalBanksLoaded.emit(
                    json.dumps(
                        {
                            "ok": True,
                            "sourceLabel": "学习通 / 雨课堂",
                            "practices": dlg.captured_practices,
                        },
                        ensure_ascii=False,
                    )
                )
            if getattr(self, "_platform_dlg", None) is dlg:
                self._platform_dlg = None

        dlg.finished.connect(_on_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    @Slot()
    def importLocalFiles(self):
        parent_w = self._main_win()
        files, _ = QFileDialog.getOpenFileNames(
            parent_w,
            "导入题库文件（支持考试宝/磨题帮 Excel、Word、TXT、Markdown、JSON、CSV/Anki）",
            "",
            "题库文件 (*.xlsx *.docx *.txt *.md *.markdown *.json *.csv *.tsv);;所有文件 (*.*)",
        )
        if not files:
            return
        w = _ImportFilesWorker(files)

        def on_done(raw: str):
            res = json.loads(raw)
            res["sourceLabel"] = "本地导入题库"
            self.externalBanksLoaded.emit(json.dumps(res, ensure_ascii=False))

        w.done.connect(on_done)
        self._keep_worker(w)

    @Slot(str)
    def loadFilters(self, practice_json: str):
        p = Practice.from_json(practice_json)
        w = _FilterWorker(self._client, p)
        w.done.connect(self.filtersReady.emit)
        self._keep_worker(w)

    @Slot(str, result=str)
    def submitExport(self, payload_json: str) -> str:
        try:
            body = json.loads(payload_json)
            fmt = ExportType(body.get("format", "pdf"))
            variant = str(body.get("variant", "withAnswersRed"))
            practiced_only = bool(body.get("practicedOnly", False))
            smart_pagination = bool(body.get("smartPagination", True))
            chapters = [ChapterType.from_dict(c) for c in body.get("chapters", [])]
            qtypes = [QuestionType.from_dict(q) for q in body.get("questionTypes", [])]
            practices_json = body.get("practices", [])
            if not practices_json:
                return json.dumps({"ok": False, "error": "请至少选择一个练习"}, ensure_ascii=False)

            needs_cctr = any(
                Practice.from_json(pj).courseid != "__local_bank__"
                for pj in practices_json
            )
            if needs_cctr and (not self._client or not self._client.key):
                return json.dumps({"ok": False, "error": "会话已失效，请重新登录"}, ensure_ascii=False)

            config = [
                {
                    "practice": pj,
                    "chapters": [c.to_dict() for c in chapters],
                    "question_types": [q.to_dict() for q in qtypes],
                    "variant": variant,
                    "practiced_only": practiced_only,
                    "smart_pagination": smart_pagination,
                }
                for pj in practices_json
            ]
            school_id = self._client.school_id if (self._client and self._client.key) else "multi"
            username = self._client.username if (self._client and self._client.key) else "多平台工作台"
            auth_key = self._client.key if (self._client and self._client.key) else ""
            task_id = add_task(
                school_id,
                username,
                "",
                fmt,
                config,
                auth_key=auth_key,
            )
            engine.run_task(task_id)
            self.poll_state(force=True)
            return json.dumps({"ok": True, "taskId": task_id}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @Slot()
    def resetSession(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @Slot(int)
    def runTask(self, task_id: int):
        update_task_status(task_id, TaskStatus.WAITING)
        engine.run_task(task_id)
        self.poll_state(force=True)

    @Slot(int)
    def deleteTask(self, task_id: int):
        delete_task(task_id)
        self.poll_state(force=True)

    @Slot()
    def clearLogs(self):
        clear_all_logs()
        self.poll_state(force=True)

    @Slot(int)
    def openTaskFile(self, task_id: int):
        for t in get_all_tasks():
            if t.id == task_id:
                art = _task_artifact_path(t)
                if art and art.exists():
                    self._open_local(art)
                    return
        self.openTaskDir(task_id)

    @Slot(int)
    def openTaskDir(self, task_id: int):
        for t in get_all_tasks():
            if t.id == task_id:
                art = _task_artifact_path(t)
                if art and art.exists():
                    if os.name == "nt":
                        subprocess.Popen(["explorer", "/select,", str(art)])
                    else:
                        self._open_local(art.parent)
                    return
        self.openExportDir()

    @Slot()
    def openExportDir(self):
        self._open_local(get_export_dir())

    @Slot(result=str)
    def chooseExportDir(self) -> str:
        current = str(get_export_dir())
        parent_w = self.parent() if isinstance(self.parent(), QMainWindow) else None
        chosen = QFileDialog.getExistingDirectory(parent_w, "选择默认导出保存文件夹", current)
        if not chosen:
            return json.dumps({"ok": False, "exportDir": current}, ensure_ascii=False)
        saved = set_export_dir(chosen)
        return json.dumps({"ok": True, "exportDir": str(saved)}, ensure_ascii=False)

    def _open_local(self, target: Path):
        if os.name == "nt":
            os.startfile(str(target))
        else:
            subprocess.Popen(["xdg-open", str(target)])

    @Slot(str, result=str)
    def selectKernel(self, kind: str) -> str:
        set_preferred_kernel(kind)
        return json.dumps(self._kernels_payload(), ensure_ascii=False)

    @Slot(result=str)
    def detectKernels(self) -> str:
        return json.dumps(self._kernels_payload(), ensure_ascii=False)

    @Slot(str)
    def downloadKernel(self, kind: str):
        target = "chromium" if kind == "auto" else kind
        w = _KernelWorker(target)
        w.progress.connect(self.kernelProgress.emit)

        def on_done(res_json: str):
            data = json.loads(res_json)
            data["kernels"] = self._kernels_payload()
            self.kernelDone.emit(json.dumps(data, ensure_ascii=False))

        w.done.connect(on_done)
        self._keep_worker(w)

    @Slot(bool)
    def setDarkMode(self, dark: bool):
        save_settings({"dark_mode": bool(dark)})

    # ── v5 AI 多模态考点融合插件桥接方法 ───────────────────────────────────

    def _ai_ws_payload(self) -> dict:
        ws = self._ai_ws
        q_briefs = [
            {
                "id": q.id,
                "question_type": q.question_type,
                "chapter_name": q.chapter_name,
                "subject_name": q.subject_name,
                "stem_preview": html_to_plain_text(q.subject_html)[:180],
                "answer_preview": html_to_plain_text(q.answer)[:90],
            }
            for q in ws.questions
        ]
        notes_brief = [
            {
                "id": n.id,
                "title": n.title,
                "source_type": n.source_type,
                "source_path": n.source_path,
                "raw_preview": (n.raw_text or "")[:240],
                "image_data_uri": n.image_data_uri,
                "ocr_summary": n.ocr_summary,
                "key_points": n.key_points,
                "formulas": n.formulas,
                "suggested_topic": n.suggested_topic,
            }
            for n in ws.notes
        ]
        return {
            "title": ws.title,
            "source_label": ws.source_label,
            "questionCount": len(ws.questions),
            "questions": q_briefs,
            "notes": notes_brief,
            "topics": [t.to_dict() for t in ws.topics],
        }

    def _emit_ai_ws(self):
        self.aiStateUpdated.emit(json.dumps(self._ai_ws_payload(), ensure_ascii=False))

    @Slot(str, result=str)
    def saveAiPluginConfig(self, cfg_json: str) -> str:
        try:
            prof = save_ai_config(ModelProfile.from_dict(json.loads(cfg_json)))
            return json.dumps({"ok": True, "config": prof.to_dict()}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @Slot(str)
    def probeOllamaModels(self, base_url: str):
        w = _AiProbeWorker(base_url)
        w.done.connect(self.aiProbeDone.emit)
        self._keep_worker(w)

    @Slot(str)
    def testAiModel(self, cfg_json: str):
        try:
            prof = save_ai_config(ModelProfile.from_dict(json.loads(cfg_json)))
        except Exception as e:
            self.aiTestDone.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        w = _AiTestWorker(prof)
        w.done.connect(self.aiTestDone.emit)
        self._keep_worker(w)

    @Slot(int, result=str)
    def loadAiFromTask(self, task_id: int) -> str:
        try:
            title, qs = load_questions_from_task_snapshot(task_id)
            self._ai_ws.title = title or f"任务 #{task_id}"
            self._ai_ws.source_label = f"已导出任务 #{task_id}"
            self._ai_ws.questions = qs
            self._ai_ws.topics = []
            self._emit_ai_ws()
            return json.dumps({"ok": True, "title": self._ai_ws.title, "count": len(qs)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @Slot(str)
    def loadAiFromPractices(self, practices_json_list: str):
        try:
            plist = json.loads(practices_json_list)
            if not plist:
                self.aiFusionDone.emit(json.dumps({"ok": False, "error": "工作台当前尚未勾选练习"}, ensure_ascii=False))
                return
        except Exception as e:
            self.aiFusionDone.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        w = _AiLoadPracticesWorker(self._client, plist)

        def on_loaded(title: str, qs: list, err: str):
            if err or not qs:
                self.aiFusionDone.emit(json.dumps({"ok": False, "error": err or "未读取到题目"}, ensure_ascii=False))
                return
            self._ai_ws.title = title or "工作台题库"
            self._ai_ws.source_label = "工作台已选练习"
            self._ai_ws.questions = qs
            self._ai_ws.topics = []
            self._emit_ai_ws()
            self.aiFusionDone.emit(json.dumps({"ok": True, "loadedOnly": True, "message": f"已载入「{self._ai_ws.title}」共 {len(qs)} 道题"}, ensure_ascii=False))

        w.done.connect(on_loaded)
        self._keep_worker(w)

    @Slot()
    def importAiQuestionFiles(self):
        QTimer.singleShot(0, self._pick_ai_question_files)

    def _pick_ai_question_files(self):
        parent_w = self._main_win()
        files, _ = QFileDialog.getOpenFileNames(
            parent_w,
            "导入题库或 v5 知识图谱 JSON",
            "",
            "题库与知识图谱 (*.json *.xlsx *.docx *.md *.markdown *.txt *.csv *.tsv);;所有文件 (*.*)",
        )
        if not files:
            return
        loaded_qs = []
        titles = []
        errors = []
        for fp in files:
            try:
                p = Path(fp)
                if p.suffix.lower() == ".json":
                    raw = p.read_text(encoding="utf-8-sig")
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and "topics" in parsed and "questions" in parsed:
                        self._ai_ws = load_workspace_from_json(raw)
                        self._emit_ai_ws()
                        self.aiFusionDone.emit(json.dumps({"ok": True, "loadedOnly": True, "message": f"已恢复知识图谱「{self._ai_ws.title}」（{len(self._ai_ws.topics)} 考点 / {len(self._ai_ws.questions)} 题）"}, ensure_ascii=False))
                        return
                title, qs = import_questions_from_file(fp)
                titles.append(title)
                loaded_qs.extend(qs)
            except Exception as e:
                errors.append(f"{Path(fp).name}: {e}")
        if loaded_qs:
            self._ai_ws.title = titles[0] if len(titles) == 1 else f"{titles[0]} 等{len(titles)}份"
            self._ai_ws.source_label = "本地导入题库"
            self._ai_ws.questions = loaded_qs
            self._ai_ws.topics = []
            self._emit_ai_ws()
            self.aiFusionDone.emit(json.dumps({"ok": True, "loadedOnly": True, "message": f"已载入 {len(loaded_qs)} 道题目"}, ensure_ascii=False))
        elif errors:
            self.aiFusionDone.emit(json.dumps({"ok": False, "error": errors[0]}, ensure_ascii=False))

    @Slot()
    def importAiNoteFiles(self):
        QTimer.singleShot(0, self._pick_ai_note_files)

    def _pick_ai_note_files(self):
        parent_w = self._main_win()
        files, _ = QFileDialog.getOpenFileNames(
            parent_w,
            "导入课堂笔记、板书拍照或课件讲义",
            "",
            "笔记与板书 (*.png *.jpg *.jpeg *.webp *.bmp *.pdf *.pptx *.docx *.md *.markdown *.txt);;所有文件 (*.*)",
        )
        if not files:
            return
        added = 0
        errors = []
        for fp in files:
            try:
                note = import_note_file(fp)
                self._ai_ws.notes.append(note)
                added += 1
            except Exception as e:
                errors.append(f"{Path(fp).name}: {e}")
        self._emit_ai_ws()
        if added:
            self.aiFusionDone.emit(json.dumps({"ok": True, "loadedOnly": True, "message": f"已导入 {added} 份课堂笔记/板书"}, ensure_ascii=False))
        elif errors:
            self.aiFusionDone.emit(json.dumps({"ok": False, "error": errors[0]}, ensure_ascii=False))

    @Slot(str, str, result=str)
    def addAiManualNote(self, title: str, text: str) -> str:
        t = (title or "").strip() or f"手动笔记 {len(self._ai_ws.notes) + 1}"
        body = (text or "").strip()
        if not body:
            return json.dumps({"ok": False, "error": "笔记内容不能为空"}, ensure_ascii=False)
        self._ai_ws.notes.append(NoteItem(id=f"note_{uuid.uuid4().hex[:8]}", title=t, source_type="txt", raw_text=body))
        self._emit_ai_ws()
        return json.dumps({"ok": True}, ensure_ascii=False)

    @Slot(str, result=str)
    def removeAiNote(self, note_id: str) -> str:
        self._ai_ws.notes = [n for n in self._ai_ws.notes if n.id != note_id]
        for tp in self._ai_ws.topics:
            tp.note_ids = [nid for nid in tp.note_ids if nid != note_id]
        self._emit_ai_ws()
        return json.dumps({"ok": True}, ensure_ascii=False)

    @Slot(str, str)
    def analyzeSingleAiNote(self, note_id: str, cfg_json: str):
        note = next((n for n in self._ai_ws.notes if n.id == note_id), None)
        if not note:
            self.aiFusionDone.emit(json.dumps({"ok": False, "error": "未找到该条课堂笔记"}, ensure_ascii=False))
            return
        prof = save_ai_config(ModelProfile.from_dict(json.loads(cfg_json)))
        w = _AiNoteAnalyzeWorker(note, prof)

        def on_done(nid: str, updated, err: str):
            if err or updated is None:
                self.aiFusionDone.emit(json.dumps({"ok": False, "error": err or "解析失败"}, ensure_ascii=False))
                return
            self._ai_ws.notes = [updated if n.id == nid else n for n in self._ai_ws.notes]
            self._emit_ai_ws()
            self.aiFusionDone.emit(json.dumps({"ok": True, "loadedOnly": True, "message": f"已完成笔记「{updated.title}」的多模态识别"}, ensure_ascii=False))

        w.done.connect(on_done)
        self._keep_worker(w)

    @Slot(str)
    def runAiFusion(self, payload_json: str):
        try:
            body = json.loads(payload_json)
            if not self._ai_ws.questions:
                self.aiFusionDone.emit(json.dumps({"ok": False, "error": "请先导入或从归档/工作台载入一套题库"}, ensure_ascii=False))
                return
            mode = str(body.get("mode") or "ai")
            title = str(body.get("title") or self._ai_ws.title or "考点智能融合").strip()
            prof = save_ai_config(ModelProfile.from_dict(body.get("profile") or {}))
        except Exception as e:
            self.aiFusionDone.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return

        w = _AiFusionWorker(self._ai_ws.questions, self._ai_ws.notes, prof, mode, title)
        w.progress.connect(self.aiProgress.emit)

        def on_done(ws, err: str):
            if err or ws is None:
                self.aiFusionDone.emit(json.dumps({"ok": False, "error": err or "分类融合失败"}, ensure_ascii=False))
                return
            self._ai_ws = ws
            self._emit_ai_ws()
            self.aiFusionDone.emit(
                json.dumps(
                    {
                        "ok": True,
                        "message": f"完成按考点分类：共 {len(ws.topics)} 个考点 / {len(ws.questions)} 题 / {len(ws.notes)} 份笔记",
                    },
                    ensure_ascii=False,
                )
            )

        w.done.connect(on_done)
        self._keep_worker(w)

    @Slot(str, result=str)
    def syncAiTopics(self, payload_json: str) -> str:
        try:
            data = json.loads(payload_json)
            if "title" in data and str(data["title"]).strip():
                self._ai_ws.title = str(data["title"]).strip()
            if isinstance(data.get("topics"), list):
                self._ai_ws.topics = [KnowledgeTopic.from_dict(t) for t in data["topics"]]
            # 静默更新状态，避免编辑考点文本框时反向触发整卡重绘造成失焦回弹
            return json.dumps({"ok": True, "topicCount": len(self._ai_ws.topics)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @Slot()
    def importAiAnyFiles(self):
        QTimer.singleShot(0, self._pick_ai_any_files)

    def _pick_ai_any_files(self):
        parent_w = self._main_win()
        files, _ = QFileDialog.getOpenFileNames(
            parent_w,
            "导入题库、课堂笔记、板书拍照或知识图谱",
            "",
            "全部支持格式 (*.xlsx *.docx *.pdf *.pptx *.png *.jpg *.jpeg *.webp *.bmp *.md *.markdown *.txt *.json *.csv *.tsv);;所有文件 (*.*)",
        )
        if files:
            self.handleDroppedPaths(files, "auto")

    @Slot(str, result=str)
    def clearAiWorkspace(self, target: str = "all") -> str:
        t = (target or "all").strip().lower()
        if t == "notes":
            self._ai_ws.notes = []
        elif t == "topics":
            self._ai_ws.topics = []
        else:
            self._ai_ws = KnowledgeWorkspace()
        self._emit_ai_ws()
        return json.dumps({"ok": True}, ensure_ascii=False)

    @Slot(str)
    def exportAiBundle(self, payload_json: str):
        try:
            body = json.loads(payload_json)
            if isinstance(body.get("topics"), list):
                self._ai_ws.topics = [KnowledgeTopic.from_dict(t) for t in body["topics"]]
            if body.get("title"):
                self._ai_ws.title = str(body["title"]).strip()
        except Exception as e:
            self.aiExportDone.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        w = _AiExportWorker(self._ai_ws, body)
        w.done.connect(self.aiExportDone.emit)
        self._keep_worker(w)

    def handleDroppedPaths(self, paths: list[str], zone: str = "auto"):
        if not paths:
            return
        z = (zone or "auto").strip().lower()
        w = _DroppedFilesWorker(paths, z)

        def on_done(target_zone: str, res):
            if target_zone == "workspace":
                self.externalBanksLoaded.emit(json.dumps(res, ensure_ascii=False))
                return
            if not isinstance(res, dict):
                self.aiFusionDone.emit(json.dumps({"ok": False, "error": "拖拽解析失败"}, ensure_ascii=False))
                return
            msgs = []
            restored = res.get("restored_ws")
            qs = res.get("questions") or []
            q_titles = res.get("question_titles") or []
            notes = res.get("notes") or []
            errors = res.get("errors") or []

            if restored is not None:
                self._ai_ws = restored
                msgs.append(f"恢复图谱「{restored.title}」")
            if qs:
                self._ai_ws.title = q_titles[0] if len(q_titles) == 1 else f"{q_titles[0]} 等{len(q_titles)}份"
                self._ai_ws.source_label = "拖拽导入题库"
                self._ai_ws.questions = qs
                self._ai_ws.topics = []
                msgs.append(f"载入题库 {len(qs)} 题")
            if notes:
                self._ai_ws.notes.extend(notes)
                msgs.append(f"加入资料/板书 {len(notes)} 份")
            if msgs:
                self._emit_ai_ws()
                summary = "已通过拖拽" + "、".join(msgs)
                if errors:
                    summary += f"（另有 {len(errors)} 个文件跳过）"
                self.aiFusionDone.emit(json.dumps({"ok": True, "loadedOnly": True, "message": summary}, ensure_ascii=False))
            else:
                self.aiFusionDone.emit(json.dumps({"ok": False, "error": (errors[0] if errors else "未识别到支持的题库或课堂笔记格式")}, ensure_ascii=False))

        w.done.connect(on_done)
        self._keep_worker(w)

    @Slot(str, str)
    def importDroppedPathsJson(self, paths_json: str, zone: str = "auto"):
        try:
            paths = [str(p) for p in json.loads(paths_json) if str(p).strip()]
        except Exception:
            paths = []
        self.handleDroppedPaths(paths, zone)

    @Slot(str, str, result=str)
    def pasteAiClipboardImage(self, title: str, data_uri: str) -> str:
        try:
            note = import_note_from_data_uri(title, data_uri)
            self._ai_ws.notes.append(note)
            self._emit_ai_ws()
            return json.dumps({"ok": True, "message": f"已粘贴截图「{note.title}」"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @Slot(result=str)
    def detectLocalCliAgents(self) -> str:
        return json.dumps({"ok": True, "agents": probe_local_cli_agents()}, ensure_ascii=False)

    @Slot(result=str)
    def prepareWorkBuddyHandoff(self) -> str:
        try:
            custom_p = load_ai_config().custom_prompt
            pack = build_workbench_handoff_pack(self._ai_ws, custom_prompt=custom_p)
            QApplication.clipboard().setText(pack["prompt_text"])
            wb_gui = next((a["path"] for a in probe_local_cli_agents() if a.get("kind") == "gui"), "")
            if os.name == "nt":
                os.startfile(pack["handoff_dir"])
                if wb_gui and Path(wb_gui).is_file():
                    subprocess.Popen([wb_gui])
            return json.dumps({"ok": True, "launchedGui": bool(wb_gui), **pack}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @Slot(str)
    def openAiWebWorkbench(self, assistant_key: str):
        try:
            custom_p = load_ai_config().custom_prompt
            pack = build_workbench_handoff_pack(self._ai_ws, custom_prompt=custom_p)
            QApplication.clipboard().setText(pack["prompt_text"])
        except Exception as e:
            self.aiFusionDone.emit(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return
        QTimer.singleShot(0, lambda k=assistant_key or "qianwen", info=pack: self._show_ai_web_workbench(k, info))

    def _show_ai_web_workbench(self, assistant_key: str, handoff_info: dict):
        parent_w = self._main_win()
        dlg = AiWebAssistantDialog(parent_w, initial_key=assistant_key, handoff_info=handoff_info)
        dlg.setModal(False)
        self._ai_web_dlg = dlg

        def _on_captured(raw_reply: str, src_lbl: str):
            res_json = self._apply_external_reply_core(raw_reply, src_lbl)
            self.aiFusionDone.emit(res_json)

        dlg.replyCaptured.connect(_on_captured)
        dlg.finished.connect(lambda _r: setattr(self, "_ai_web_dlg", None) if getattr(self, "_ai_web_dlg", None) is dlg else None)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _apply_external_reply_core(self, raw_text: str, source_label: str = "千问/WorkBuddy 智能体融合") -> str:
        try:
            text = (raw_text or "").strip() or (QApplication.clipboard().text() or "").strip()
            ws = parse_external_assistant_reply(
                text,
                self._ai_ws.questions,
                self._ai_ws.notes,
                title=self._ai_ws.title or "AI 考点智能融合",
                source_label=source_label,
            )
            self._ai_ws = ws
            self._emit_ai_ws()
            return json.dumps(
                {
                    "ok": True,
                    "message": f"完成导入「{source_label}」整理结果：共 {len(ws.topics)} 个考点 / {len(ws.questions)} 题",
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)

    @Slot(str, result=str)
    def applyExternalAiReply(self, raw_text: str) -> str:
        return self._apply_external_reply_core(raw_text)

    @Slot(str)
    def openLocalPath(self, path: str):
        p = Path(path)
        if p.exists():
            self._open_local(p)

    @Slot(str)
    def revealLocalPath(self, path: str):
        p = Path(path)
        if p.exists():
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                self._open_local(p.parent)
        else:
            self.openExportDir()

    def _main_win(self) -> QMainWindow | None:
        p = self.parent()
        return p if isinstance(p, QMainWindow) else None

    @Slot()
    def windowMinimize(self):
        w = self._main_win()
        if w:
            w.showMinimized()

    @Slot()
    def windowToggleMaximize(self):
        w = self._main_win()
        if not w:
            return
        if w.isMaximized():
            w.showNormal()
        else:
            w.showMaximized()

    @Slot()
    def windowClose(self):
        w = self._main_win()
        if w:
            w.close()

    @Slot()
    def windowStartDrag(self):
        w = self._main_win()
        if w and w.windowHandle():
            w.windowHandle().startSystemMove()

    @Slot(str)
    def windowStartResize(self, edge: str):
        w = self._main_win()
        if not w or not w.windowHandle():
            return
        edge_map = {
            "n": Qt.Edge.TopEdge,
            "s": Qt.Edge.BottomEdge,
            "w": Qt.Edge.LeftEdge,
            "e": Qt.Edge.RightEdge,
            "nw": Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
            "ne": Qt.Edge.TopEdge | Qt.Edge.RightEdge,
            "sw": Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
            "se": Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
        }
        qt_edge = edge_map.get((edge or "").lower())
        if qt_edge is not None:
            w.windowHandle().startSystemResize(qt_edge)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CCTR Atelier · 融智云考印务工作台")
        self.setWindowIcon(_build_atelier_icon())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Window
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowSystemMenuHint
        )
        self._dwm_applied = False
        self.setMinimumSize(QSize(680, 560))
        self.resize(1340, 840)

        screen = self.screen().availableGeometry()
        self.move((screen.width() - self.width()) // 2, (screen.height() - self.height()) // 2)

        # 主线程初始化内置 QtWebEngine PDF 打印桥（供内置/兜底模式使用）
        init_qt_pdf_bridge(self)

        self._bridge = DesktopBridge(self)
        self._view = _AtelierWebView(self, self._bridge)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        settings = self._view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

        self._channel = QWebChannel(self._view.page())
        self._channel.registerObject("cctrBridge", self._bridge)
        self._view.page().setWebChannel(self._channel)

        # 通过 QFile 内联注入 qwebchannel.js，避开 file:// 加载 qrc:// 跨域拦截
        qwc_file = QFile(":/qtwebchannel/qwebchannel.js")
        if qwc_file.open(QIODevice.OpenModeFlag.ReadOnly):
            qwc_code = bytes(qwc_file.readAll()).decode("utf-8", errors="ignore")
            qwc_file.close()
            script = QWebEngineScript()
            script.setName("qwebchannel")
            script.setSourceCode(qwc_code)
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
            script.setRunsOnSubFrames(False)
            self._view.page().scripts().insert(script)

        self.setCentralWidget(self._view)

        index_html = _resolve_web_index()
        self._view.load(QUrl.fromLocalFile(str(index_html)))

        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: self._bridge.poll_state(False))
        self._timer.start(1500)

    def _apply_win11_dwm_style(self):
        if self._dwm_applied or os.name != "nt":
            return
        self._dwm_applied = True
        try:
            class _MARGINS(ctypes.Structure):
                _fields_ = [
                    ("cxLeftWidth", ctypes.c_int),
                    ("cxRightWidth", ctypes.c_int),
                    ("cyTopHeight", ctypes.c_int),
                    ("cyBottomHeight", ctypes.c_int),
                ]

            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(ctypes.c_int(2)), ctypes.sizeof(ctypes.c_int)
            )
            margins = _MARGINS(0, 0, 0, 1)
            dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(margins))
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_win11_dwm_style()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "_bridge"):
            self._bridge.windowStateChanged.emit(self.isMaximized())

    def closeEvent(self, event):
        self._bridge.stop_workers()
        engine.shutdown()
        super().closeEvent(event)
