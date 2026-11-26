"""多模态 AI 考点智能分类与课堂笔记融合插件引擎（支持 OpenAI 兼容多模态与本机 Ollama）"""
from __future__ import annotations

import base64
import dataclasses
import html as html_lib
import io
import json
import os
import re
import shutil
import subprocess
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

from PIL import Image, ImageOps

from ..core.exporter import export_questions_to_pdf, pack_to_zip, prefetch_and_inline_images, render_html_to_pdf_bytes
from ..core.formats import (
    _clean_xml_text,
    _col_index_to_letters,
    _docx_bytes_to_text,
    export_questions_to_json,
    html_to_plain_text,
    import_questions_from_file,
    is_choice_question,
    normalize_choice_answer_letters,
    parse_json_questions,
)
from ..core.http_utils import ResilientHttpSession
from ..core.integrity import (
    _render_text_or_html,
    build_export_document_html,
    has_visible_rich_content,
    safe_archive_segment,
    sha256_bytes,
    validate_questions,
)
from ..core.kernels import get_export_dir, load_settings, save_settings
from ..core.models import Option, Question

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "qwen_api": {
        "label": "阿里云百炼 · 通义千问 (qwen-vl-max)",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max-latest",
    },
    "workbuddy_cli": {
        "label": "腾讯 WorkBuddy / CodeBuddy 本机 CLI（免 Key 直呼）",
        "provider": "cli",
        "base_url": "codebuddy",
        "model": "auto",
    },
    "ollama": {
        "label": "本机 Ollama（离线开源多模态 · qwen2.5vl）",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5vl:7b",
    },
    "workbuddy_api": {
        "label": "腾讯混元 / WorkBuddy 开放接口 (hunyuan-vision)",
        "provider": "openai",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "model": "hunyuan-vision",
    },
    "deepseek_api": {
        "label": "DeepSeek 官方接口 (deepseek-chat)",
        "provider": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "openai_siliconflow": {
        "label": "硅基流动 SiliconFlow (Qwen2.5-VL-72B)",
        "provider": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
    },
    "openai_custom": {
        "label": "自定义 OpenAI 兼容多模态服务 (OneAPI / vLLM)",
        "provider": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "gpt-4o-mini",
    },
}

WEB_ASSISTANT_PRESETS: dict[str, tuple[str, str]] = {
    "qianwen": ("通义千问（免 API 网页直连）", "https://www.qianwen.com/"),
    "yuanbao": ("腾讯元宝 / WorkBuddy 助手", "https://yuanbao.tencent.com/"),
    "deepseek": ("DeepSeek 网页助手", "https://chat.deepseek.com/"),
    "kimi": ("Kimi 长文本助手", "https://kimi.moonshot.cn/"),
    "doubao": ("豆包 AI 助手", "https://www.doubao.com/chat/"),
}

_PRESET_COMPAT_ALIASES = {
    "openai_qwen": "qwen_api",
    "openai_zhipu": "openai_custom",
    "openai_official": "openai_custom",
}

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
_CN_CLOUD_SUFFIXES = (".aliyuncs.com", ".siliconflow.cn", ".bigmodel.cn", ".volces.com", ".deepseek.com", ".baidubce.com", ".myqcloud.com", ".tencent.com")
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
_NOTE_ONLY_EXTS = _IMAGE_EXTS | frozenset({".pdf", ".pptx"})
_QUESTION_ONLY_EXTS = frozenset({".xlsx", ".csv", ".tsv"})
_NOTE_NAME_HINTS = ("笔记", "讲义", "板书", "课件", "总结", "重点", "提纲", "大纲", "note", "lecture", "slides")
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_+-]{1,}")
_SLIDE_NUM_RE = re.compile(r"slide(\d+)\.xml$", re.I)
_Q_REF_TOKEN_RE = re.compile(r"(?:Q\s*(\d+)(?:\s*[-~～]\s*Q?\s*(\d+))?|第\s*(\d+)\s*题|#\s*(\d+))", re.I)


@dataclasses.dataclass
class ModelProfile:
    preset: str = "qwen_api"
    provider: str = "openai"  # "ollama" | "openai" | "cli"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    model: str = "qwen-vl-max-latest"
    batch_size: int = 24
    custom_prompt: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ModelProfile:
        raw_preset = str(d.get("preset") or "qwen_api")
        preset = _PRESET_COMPAT_ALIASES.get(raw_preset, raw_preset)
        return cls(
            preset=preset,
            provider=str(d.get("provider") or ("ollama" if preset == "ollama" else ("cli" if preset == "workbuddy_cli" else "openai"))),
            base_url=str(d.get("base_url") or PROVIDER_PRESETS.get(preset, PROVIDER_PRESETS["qwen_api"])["base_url"]).strip().rstrip("/"),
            api_key=str(d.get("api_key") or "").strip(),
            model=str(d.get("model") or PROVIDER_PRESETS.get(preset, PROVIDER_PRESETS["qwen_api"])["model"]).strip(),
            batch_size=max(5, min(60, int(d.get("batch_size") or 24))),
            custom_prompt=str(d.get("custom_prompt") or "").strip(),
        )


@dataclasses.dataclass
class NoteItem:
    id: str
    title: str
    source_type: str  # "image" | "pdf" | "docx" | "pptx" | "md" | "txt"
    source_path: str = ""
    raw_text: str = ""
    image_data_uri: str = ""
    ocr_summary: str = ""
    key_points: list[str] = dataclasses.field(default_factory=list)
    formulas: list[str] = dataclasses.field(default_factory=list)
    suggested_topic: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> NoteItem:
        return cls(
            id=str(d.get("id") or uuid.uuid4().hex[:10]),
            title=str(d.get("title") or "课堂笔记"),
            source_type=str(d.get("source_type") or "txt"),
            source_path=str(d.get("source_path") or ""),
            raw_text=str(d.get("raw_text") or ""),
            image_data_uri=str(d.get("image_data_uri") or ""),
            ocr_summary=str(d.get("ocr_summary") or ""),
            key_points=[str(x) for x in (d.get("key_points") or []) if str(x).strip()],
            formulas=[str(x) for x in (d.get("formulas") or []) if str(x).strip()],
            suggested_topic=str(d.get("suggested_topic") or ""),
        )


@dataclasses.dataclass
class KnowledgeTopic:
    id: str
    name: str
    importance: str = "重要掌握"  # "核心高频" | "重要掌握" | "基础了解"
    summary_md: str = ""
    formulas: list[str] = dataclasses.field(default_factory=list)
    note_ids: list[str] = dataclasses.field(default_factory=list)
    question_ids: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> KnowledgeTopic:
        imp = str(d.get("importance") or "重要掌握")
        if imp not in ("核心高频", "重要掌握", "基础了解"):
            imp = "重要掌握"
        return cls(
            id=str(d.get("id") or f"tp_{uuid.uuid4().hex[:8]}"),
            name=str(d.get("name") or "未命名考点").strip() or "未命名考点",
            importance=imp,
            summary_md=str(d.get("summary_md") or ""),
            formulas=[str(x).strip() for x in (d.get("formulas") or []) if str(x).strip()],
            note_ids=[str(x) for x in (d.get("note_ids") or []) if str(x).strip()],
            question_ids=[str(x) for x in (d.get("question_ids") or []) if str(x).strip()],
        )


@dataclasses.dataclass
class KnowledgeWorkspace:
    title: str = "考点融合知识库"
    source_label: str = ""
    questions: list[Question] = dataclasses.field(default_factory=list)
    notes: list[NoteItem] = dataclasses.field(default_factory=list)
    topics: list[KnowledgeTopic] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": 5,
            "title": self.title,
            "source_label": self.source_label,
            "questions": [dataclasses.asdict(q) for q in self.questions],
            "notes": [n.to_dict() for n in self.notes],
            "topics": [t.to_dict() for t in self.topics],
        }


def load_workspace_from_json(raw_json: str) -> KnowledgeWorkspace:
    data = json.loads(raw_json)
    qs: list[Question] = []
    for item in data.get("questions") or []:
        if not isinstance(item, dict):
            continue
        opts = [
            Option(is_true=bool(o.get("is_true")), html=str(o.get("html") or ""))
            for o in (item.get("options") or [])
            if isinstance(o, dict)
        ]
        qs.append(
            Question(
                id=str(item.get("id") or ""),
                question_type=str(item.get("question_type") or "单选题"),
                subject_html=str(item.get("subject_html") or ""),
                answer=str(item.get("answer") or ""),
                has_options=bool(item.get("has_options")),
                options=opts,
                subject_name=str(item.get("subject_name") or ""),
                chapter_name=str(item.get("chapter_name") or ""),
                analysis_html=str(item.get("analysis_html") or ""),
                is_choice=bool(item.get("is_choice")),
                chapter_id=str(item.get("chapter_id") or ""),
                question_type_id=str(item.get("question_type_id") or ""),
            )
        )
    notes = [NoteItem.from_dict(n) for n in (data.get("notes") or []) if isinstance(n, dict)]
    topics = [KnowledgeTopic.from_dict(t) for t in (data.get("topics") or []) if isinstance(t, dict)]
    return KnowledgeWorkspace(
        title=str(data.get("title") or "考点融合知识库"),
        source_label=str(data.get("source_label") or ""),
        questions=qs,
        notes=notes,
        topics=topics,
    )


def load_ai_config() -> ModelProfile:
    raw = load_settings().get("ai_plugin")
    if isinstance(raw, dict):
        return ModelProfile.from_dict(raw)
    return ModelProfile()


def save_ai_config(profile: ModelProfile) -> ModelProfile:
    save_settings({"ai_plugin": profile.to_dict()})
    return profile


def _is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _LOCAL_HOSTS or host.startswith(("127.", "192.168.", "10."))


def _prefer_direct_for_url(url: str) -> bool:
    if _is_local_url(url):
        return True
    host = (urlparse(url).hostname or "").lower()
    return any(host.endswith(suf) or host == suf.lstrip(".") for suf in _CN_CLOUD_SUFFIXES)


def _decode_text_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _pptx_bytes_to_text(raw: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        slides = [n for n in zf.namelist() if _SLIDE_NUM_RE.search(n)]
        slides.sort(key=lambda n: int(_SLIDE_NUM_RE.search(n).group(1)))
        for idx, sname in enumerate(slides, 1):
            root = ET.fromstring(zf.read(sname))
            texts: list[str] = []
            for elem in root.iter():
                if elem.tag.endswith("}t") and elem.text and elem.text.strip():
                    texts.append(elem.text.strip())
            if texts:
                parts.append(f"[幻灯片 {idx}]\n" + "\n".join(texts))
    return "\n\n".join(parts)


def _pdf_bytes_to_text(raw: bytes) -> str:
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(raw))
    pages: list[str] = []
    for idx, page in enumerate(reader.pages, 1):
        txt = (page.extract_text() or "").strip()
        if txt:
            pages.append(f"[第 {idx} 页]\n{txt}")
    return "\n\n".join(pages)


def _encode_note_image_data_uri(raw: bytes, max_edge: int = 1600) -> str:
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[3])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        if max(im.size) > max_edge:
            im.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=88, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"


def import_note_file(path: str | Path) -> NoteItem:
    """导入板书拍照图片、PDF/PPTX/Word 课件讲义或 Markdown/TXT 课堂笔记。"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"未找到笔记文件：{p}")
    ext = p.suffix.lower()
    nid = f"note_{uuid.uuid4().hex[:10]}"
    raw = p.read_bytes()

    if ext in _IMAGE_EXTS:
        data_uri = _encode_note_image_data_uri(raw)
        return NoteItem(
            id=nid,
            title=p.stem,
            source_type="image",
            source_path=str(p),
            image_data_uri=data_uri,
        )
    if ext == ".pdf":
        text = _pdf_bytes_to_text(raw)
        if not text.strip():
            raise ValueError(f"「{p.name}」为纯扫描图片 PDF，可截图为 PNG/JPG 后直接以板书图片导入识别")
        return NoteItem(id=nid, title=p.stem, source_type="pdf", source_path=str(p), raw_text=text)
    if ext == ".pptx":
        text = _pptx_bytes_to_text(raw)
        if not text.strip():
            raise ValueError(f"「{p.name}」未提取到幻灯片文字，若为全图 PPT 请另存为图片导入")
        return NoteItem(id=nid, title=p.stem, source_type="pptx", source_path=str(p), raw_text=text)
    if ext == ".docx":
        text = _docx_bytes_to_text(raw)
        return NoteItem(id=nid, title=p.stem, source_type="docx", source_path=str(p), raw_text=text)
    if ext in (".md", ".markdown", ".txt", ".text"):
        return NoteItem(
            id=nid,
            title=p.stem,
            source_type=ext.lstrip(".")[:4],
            source_path=str(p),
            raw_text=_decode_text_bytes(raw),
        )
    raise ValueError(f"不支持的课堂笔记格式：{ext}（支持 PNG/JPG/WEBP、PDF、PPTX、DOCX、MD、TXT）")


def load_questions_from_task_snapshot(task_id: int) -> tuple[str, list[Question]]:
    snap = Path.home() / ".cctr_export" / "task_questions" / f"{task_id}.json"
    if not snap.exists():
        raise FileNotFoundError(f"任务 #{task_id} 尚无题库快照（请先导出该任务或直接导入对应题库文件）")
    return parse_json_questions(snap.read_text(encoding="utf-8"), f"任务 #{task_id}")


def load_questions_any_file(path: str | Path) -> tuple[str, list[Question]]:
    return import_questions_from_file(path)


def import_note_from_data_uri(title: str, data_uri: str) -> NoteItem:
    """将剪贴板粘贴的截图 Data URI 标准化压缩后转为课堂笔记/板书图片项。"""
    raw_uri = (data_uri or "").strip()
    if "," not in raw_uri:
        raise ValueError("剪贴板中未包含有效的图片数据")
    b64_part = raw_uri.split(",", 1)[1]
    raw_bytes = base64.b64decode(b64_part)
    clean_uri = _encode_note_image_data_uri(raw_bytes)
    clean_title = (title or "").strip() or f"剪贴板板书_{uuid.uuid4().hex[:4]}"
    return NoteItem(
        id=f"note_{uuid.uuid4().hex[:10]}",
        title=clean_title,
        source_type="image",
        source_path="clipboard",
        image_data_uri=clean_uri,
    )


def _expand_dropped_paths(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for raw in paths or []:
        p = Path(str(raw or "").strip())
        if not p.exists():
            continue
        candidates = [p] if p.is_file() else [sub for sub in sorted(p.iterdir()) if sub.is_file()]
        for item in candidates:
            key = str(item.resolve()).lower()
            if key not in seen:
                seen.add(key)
                out.append(item)
    return out


def classify_and_import_dropped_paths(paths: list[str], *, zone_hint: str = "auto") -> dict:
    """智能识别拖入的文件或文件夹，自动分流为【题库】、【课堂笔记/板书】或【可续编辑知识图谱】."""
    zone = (zone_hint or "auto").strip().lower()
    expanded = _expand_dropped_paths(paths)
    restored_ws: KnowledgeWorkspace | None = None
    questions: list[Question] = []
    question_titles: list[str] = []
    notes: list[NoteItem] = []
    errors: list[str] = []

    for p in expanded:
        ext = p.suffix.lower()
        try:
            if ext == ".json" and zone != "notes":
                raw_txt = _decode_text_bytes(p.read_bytes())
                parsed = json.loads(raw_txt)
                if isinstance(parsed, dict) and "topics" in parsed and "questions" in parsed:
                    restored_ws = load_workspace_from_json(raw_txt)
                    continue
                t, qs = import_questions_from_file(p)
                if qs:
                    question_titles.append(t)
                    questions.extend(qs)
                else:
                    notes.append(import_note_file(p))
                continue

            if ext in _NOTE_ONLY_EXTS:
                notes.append(import_note_file(p))
                continue

            if ext in _QUESTION_ONLY_EXTS and zone != "notes":
                t, qs = import_questions_from_file(p)
                question_titles.append(t)
                questions.extend(qs)
                continue

            if zone == "notes":
                notes.append(import_note_file(p))
                continue

            if zone in ("questions", "workspace"):
                t, qs = import_questions_from_file(p)
                question_titles.append(t)
                questions.extend(qs)
                continue

            # zone == "auto" 且扩展名为 .docx/.md/.txt：结合文件名与解析题目数自动判定
            name_low = p.stem.lower()
            if any(k in name_low for k in _NOTE_NAME_HINTS):
                notes.append(import_note_file(p))
                continue
            try:
                t, qs = import_questions_from_file(p)
            except Exception:
                qs = []
                t = p.stem
            if len(qs) >= 2:
                question_titles.append(t)
                questions.extend(qs)
            else:
                notes.append(import_note_file(p))
        except Exception as e:
            errors.append(f"{p.name}: {e}")

    return {
        "restored_ws": restored_ws,
        "questions": questions,
        "question_titles": question_titles,
        "notes": notes,
        "errors": errors,
    }


def _extract_json_from_llm(text: str) -> dict | list:
    cleaned = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.I)
    if fence:
        cleaned = fence.group(1).strip()
    for start_ch, end_ch in (("{", "}"), ("[", "]")):
        i = cleaned.find(start_ch)
        j = cleaned.rfind(end_ch)
        if i != -1 and j > i:
            sub = cleaned[i : j + 1]
            try:
                return json.loads(sub)
            except json.JSONDecodeError:
                fixed = re.sub(r",\s*([}\]])", r"\1", sub)
                try:
                    return json.loads(fixed)
                except json.JSONDecodeError:
                    continue
    return json.loads(cleaned)


def _candidate_cli_dirs() -> list[Path]:
    dirs: list[Path] = []
    home = Path.home()
    for env_key in ("APPDATA", "LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        val = os.environ.get(env_key, "").strip()
        if not val:
            continue
        base = Path(val)
        dirs.extend(
            [
                base / "npm",
                base / "Programs" / "WorkBuddy",
                base / "Programs" / "CodeBuddy",
                base / "WorkBuddy",
                base / "CodeBuddy",
                base / "Qwen",
            ]
        )
    dirs.extend([home / ".local" / "bin", home / ".codebuddy" / "bin", home / ".workbuddy" / "bin"])
    return [d for d in dirs if d.is_dir()]


def _resolve_cli_command(preferred: str = "") -> str:
    names = []
    pref = (preferred or "").strip()
    if pref:
        if Path(pref).is_file():
            return str(Path(pref))
        names.append(pref)
    for fallback in ("workbuddy", "codebuddy", "qwen"):
        if fallback not in names:
            names.append(fallback)
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit
        for d in _candidate_cli_dirs():
            for ext in (".cmd", ".exe", ".bat", ""):
                cand = d / f"{name}{ext}"
                if cand.is_file():
                    return str(cand)
    return ""


def probe_local_cli_agents() -> list[dict]:
    """扫描本机已安装的 WorkBuddy / CodeBuddy / Qwen CLI 及 WorkBuddy 桌面客户端。"""
    found: list[dict] = []
    specs = [
        ("workbuddy", "WorkBuddy CLI（腾讯免 Key 智能体）", "workbuddy_cli"),
        ("codebuddy", "CodeBuddy CLI（腾讯智能体管道）", "workbuddy_cli"),
        ("qwen", "Qwen Code / 通义本地 CLI", "qwen_cli"),
    ]
    seen_paths: set[str] = set()
    for cmd_name, label, preset in specs:
        resolved = _resolve_cli_command(cmd_name)
        if resolved and resolved.lower() not in seen_paths:
            seen_paths.add(resolved.lower())
            found.append({"cmd": cmd_name, "path": resolved, "label": label, "preset": preset, "kind": "cli"})
    # 检测 WorkBuddy 桌面端 GUI 是否存在
    for d in _candidate_cli_dirs():
        exe = d / "WorkBuddy.exe"
        if exe.is_file() and str(exe).lower() not in seen_paths:
            seen_paths.add(str(exe).lower())
            found.append({"cmd": "WorkBuddy.exe", "path": str(exe), "label": "WorkBuddy 桌面客户端", "preset": "workbuddy_desktop", "kind": "gui"})
    return found


def get_handoff_dir() -> Path:
    d = Path.home() / ".cctr_export" / "ai_handoff"
    d.mkdir(parents=True, exist_ok=True)
    return d


class MultimodalModelClient:
    """统一多模态大模型调用客户端（兼容 OpenAI 多模态接口、本机 Ollama 与 WorkBuddy/CodeBuddy 本地 CLI）。"""

    def __init__(self, profile: ModelProfile):
        self.profile = profile

    @staticmethod
    def probe_ollama_models(base_url: str = "http://127.0.0.1:11434") -> list[dict]:
        base = (base_url or "http://127.0.0.1:11434").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        sess = ResilientHttpSession(timeout=4.0, prefer_direct=True)
        try:
            resp = sess.get(f"{base}/api/tags", timeout=4.0, retries=0)
            if resp.status_code != 200:
                raise RuntimeError(f"Ollama 服务返回 HTTP {resp.status_code}")
            data = resp.json()
            models = []
            for m in data.get("models") or []:
                name = str(m.get("name") or "").strip()
                if not name:
                    continue
                is_vision = any(tag in name.lower() for tag in ("vl", "vision", "llava", "minicpm", "moondream", "gemma3", "pixtral", "bakllava"))
                size_gb = round((m.get("size") or 0) / (1024**3), 1)
                models.append({"name": name, "vision": is_vision, "sizeGb": size_gb})
            return models
        finally:
            sess.close()

    def chat(self, prompt: str, *, image_data_uri: str = "", timeout: float = 120.0) -> str:
        if self.profile.provider == "cli":
            return self._chat_cli(prompt, image_data_uri=image_data_uri, timeout=timeout)

        base = (self.profile.base_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("请先配置模型 API 地址 (Base URL)")
        model = (self.profile.model or "").strip()
        if not model:
            raise ValueError("请先填写或选择模型名称")

        prefer_direct = _prefer_direct_for_url(base)
        sess = ResilientHttpSession(timeout=timeout, prefer_direct=prefer_direct)
        try:
            if self.profile.provider == "ollama":
                api_root = base[:-3] if base.endswith("/v1") else base
                msg: dict[str, object] = {"role": "user", "content": prompt}
                if image_data_uri and "," in image_data_uri:
                    msg["images"] = [image_data_uri.split(",", 1)[1]]
                body = {
                    "model": model,
                    "messages": [msg],
                    "stream": False,
                    "options": {"temperature": 0.2},
                }
                headers = {"Content-Type": "application/json"}
                if self.profile.api_key:
                    headers["Authorization"] = f"Bearer {self.profile.api_key}"
                resp = sess.post(f"{api_root}/api/chat", json=body, headers=headers, timeout=timeout, retries=1)
                if resp.status_code == 404:
                    return self._chat_openai(sess, f"{api_root}/v1", model, prompt, image_data_uri, timeout)
                if resp.status_code >= 400:
                    raise RuntimeError(f"Ollama 调用失败 ({resp.status_code}): {resp.text[:300]}")
                data = resp.json()
                return str(((data.get("message") or {}).get("content")) or "").strip()

            return self._chat_openai(sess, base, model, prompt, image_data_uri, timeout)
        finally:
            sess.close()

    def _chat_cli(self, prompt: str, *, image_data_uri: str = "", timeout: float = 180.0) -> str:
        cmd = _resolve_cli_command(self.profile.base_url)
        if not cmd:
            raise RuntimeError(
                "未检测到本机 WorkBuddy / CodeBuddy / Qwen CLI 命令行工具；"
                "可切换为上方「🤝 免 API 接入千问 / WorkBuddy」打开内置助手窗口直接一键处理，无需 API Key。"
            )
        work_dir = get_handoff_dir()
        full_prompt = prompt
        temp_files: list[Path] = []
        try:
            if image_data_uri and "," in image_data_uri:
                img_path = work_dir / f"_cli_img_{uuid.uuid4().hex[:8]}.jpg"
                img_path.write_bytes(base64.b64decode(image_data_uri.split(",", 1)[1]))
                temp_files.append(img_path)
                full_prompt = f"请先读取并识图该课堂板书图片文件：{img_path}\n\n{full_prompt}"
            if len(full_prompt) > 5500:
                task_md = work_dir / f"_cli_task_{uuid.uuid4().hex[:8]}.md"
                task_md.write_text(full_prompt, encoding="utf-8")
                temp_files.append(task_md)
                cli_arg = f"请阅读文件 {task_md} 中的完整指令与数据，并严格按其中要求输出结果。"
            else:
                cli_arg = full_prompt

            argv = [cmd, "-p", cli_arg]
            if self.profile.model and self.profile.model.lower() != "auto":
                argv.extend(["--model", self.profile.model])
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            proc = subprocess.run(
                argv,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=flags,
            )
            if proc.returncode != 0:
                err_msg = (proc.stderr or proc.stdout or f"退出码 {proc.returncode}").strip()[:320]
                raise RuntimeError(f"本地 CLI ({Path(cmd).name}) 执行失败：{err_msg}")
            out = (proc.stdout or "").strip()
            if not out:
                raise RuntimeError(f"本地 CLI ({Path(cmd).name}) 未返回输出内容")
            return out
        finally:
            for tf in temp_files:
                try:
                    tf.unlink(missing_ok=True)
                except OSError:
                    pass

    def _chat_openai(
        self,
        sess: ResilientHttpSession,
        base: str,
        model: str,
        prompt: str,
        image_data_uri: str,
        timeout: float,
    ) -> str:
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        if not self.profile.api_key and not _is_local_url(base):
            raise ValueError("请先填写 API Key（若使用本机免费离线模型，请切换上方预设为「本机 Ollama」）")
        if image_data_uri:
            content: object = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_uri}},
            ]
        else:
            content = prompt
        headers = {"Content-Type": "application/json"}
        if self.profile.api_key:
            headers["Authorization"] = f"Bearer {self.profile.api_key}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.2,
        }
        resp = sess.post(url, json=payload, headers=headers, timeout=timeout, retries=1)
        if resp.status_code >= 400:
            raise RuntimeError(f"模型接口返回错误 ({resp.status_code}): {resp.text[:360]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("模型未返回 choices 结果")
        msg = choices[0].get("message") or {}
        raw_c = msg.get("content")
        if isinstance(raw_c, list):
            return "\n".join(str(p.get("text") or "") for p in raw_c if isinstance(p, dict)).strip()
        return str(raw_c or "").strip()


def analyze_note_item(note: NoteItem, client: MultimodalModelClient) -> NoteItem:
    """使用多模态模型识别课堂板书图片或提炼长文笔记的核心考点与公式。"""
    extra_hint = f"\n用户附加要求：{client.profile.custom_prompt}" if client.profile.custom_prompt else ""
    if note.source_type == "image" and note.image_data_uri:
        prompt = (
            f"请仔细识别这张课堂板书/手写笔记图片（标题：{note.title}）中的全部文字、数学/专业公式、图示要点与重点标注。"
            f"{extra_hint}\n请严格只输出一个 JSON 对象，格式如下：\n"
            '{"suggested_topic": "建议归属的核心考点名称（10字以内）", '
            '"summary_md": "结构化整理后的完整笔记精要（Markdown，公式保留清晰写法）", '
            '"key_points": ["要点1", "要点2"], "formulas": ["公式或定义1"]}'
        )
        raw = client.chat(prompt, image_data_uri=note.image_data_uri, timeout=120.0)
    else:
        body_text = (note.raw_text or "").strip()
        if not body_text:
            return note
        excerpt = body_text[:12000]
        prompt = (
            f"请提炼以下课堂笔记/讲义片段（来源：{note.title}）中的核心考点、关键公式与高频易错点。{extra_hint}\n"
            "请严格只输出一个 JSON 对象：\n"
            '{"suggested_topic": "核心考点名称", "summary_md": "结构化 Markdown 笔记精要", '
            '"key_points": ["要点1", "要点2"], "formulas": ["公式1"]}\n\n'
            f"【笔记正文】\n{excerpt}"
        )
        raw = client.chat(prompt, timeout=90.0)

    try:
        obj = _extract_json_from_llm(raw)
        if isinstance(obj, dict):
            note.suggested_topic = str(obj.get("suggested_topic") or note.suggested_topic or note.title).strip()
            note.ocr_summary = str(obj.get("summary_md") or raw).strip()
            note.key_points = [str(x).strip() for x in (obj.get("key_points") or []) if str(x).strip()]
            note.formulas = [str(x).strip() for x in (obj.get("formulas") or []) if str(x).strip()]
            return note
    except Exception:
        pass
    note.ocr_summary = raw
    note.suggested_topic = note.suggested_topic or note.title
    return note


def _question_brief(q: Question) -> str:
    stem = html_to_plain_text(q.subject_html)[:220].replace("\n", " ")
    ans = html_to_plain_text(q.answer)[:100].replace("\n", " ")
    opts = ""
    if q.options:
        opts = " | 选项: " + "; ".join(html_to_plain_text(o.html)[:45] for o in q.options[:4])
    return f"ID={q.id} | 章节={q.chapter_name} | 题型={q.question_type} | 题干={stem}{opts} | 答案={ans}"


def _tokenize_zh(text: str) -> set[str]:
    tokens = set()
    for m in _TOKEN_RE.findall(text or ""):
        ml = m.lower()
        tokens.add(ml)
        if len(ml) >= 4 and re.match(r"^[\u4e00-\u9fff]+$", ml):
            for i in range(len(ml) - 1):
                tokens.add(ml[i : i + 2])
    return tokens


def offline_classify_and_fuse(questions: list[Question], notes: list[NoteItem], title: str = "考点智能分类") -> KnowledgeWorkspace:
    """免 API 零成本本地关键词/章节语义融合分类（同时作为大模型未分配题目的兜底引擎）。"""
    items = validate_questions(questions)
    note_tokens = {
        n.id: _tokenize_zh(f"{n.title} {n.suggested_topic} {n.ocr_summary} {n.raw_text[:2000]}")
        for n in notes
    }

    groups: dict[str, list[Question]] = defaultdict(list)
    for q in items:
        chap = (q.chapter_name or "").strip()
        if chap and chap != "未分类章节":
            tname = chap
        else:
            q_tok = _tokenize_zh(html_to_plain_text(q.subject_html) + " " + q.question_type)
            best_note: NoteItem | None = None
            best_score = 0
            for n in notes:
                sc = len(q_tok & note_tokens.get(n.id, set()))
                if sc > best_score:
                    best_score = sc
                    best_note = n
            tname = (best_note.suggested_topic or best_note.title) if (best_note and best_score >= 2) else (q.question_type or "综合核心考点")
        groups[tname].append(q)

    topics: list[KnowledgeTopic] = []
    for idx, (tname, q_list) in enumerate(groups.items(), 1):
        t_tok = _tokenize_zh(tname + " " + " ".join(html_to_plain_text(q.subject_html)[:100] for q in q_list[:10]))
        matched_notes = []
        for n in notes:
            nt = note_tokens.get(n.id, set())
            if tname in n.title or n.title in tname or tname == n.suggested_topic or len(t_tok & nt) >= 2:
                matched_notes.append(n)

        summary_lines = [f"本考点共汇集 **{len(q_list)}** 道题目。"]
        formulas: list[str] = []
        for mn in matched_notes:
            snippet = (mn.ocr_summary or mn.raw_text or "").strip()[:500]
            if snippet:
                summary_lines.append(f"**课堂笔记《{mn.title}》精要：**\n{snippet}")
            formulas.extend(mn.formulas)

        imp = "核心高频" if len(q_list) >= max(8, len(items) // 4) else ("重要掌握" if len(q_list) >= 3 else "基础了解")
        topics.append(
            KnowledgeTopic(
                id=f"tp_{idx}",
                name=tname,
                importance=imp,
                summary_md="\n\n".join(summary_lines),
                formulas=list(dict.fromkeys(formulas))[:8],
                note_ids=[n.id for n in matched_notes],
                question_ids=[q.id for q in q_list],
            )
        )

    return KnowledgeWorkspace(title=title, source_label="本地章节与笔记关键词融合", questions=items, notes=notes, topics=topics)


def ai_classify_and_fuse(
    questions: list[Question],
    notes: list[NoteItem],
    profile: ModelProfile,
    *,
    title: str = "AI 考点智能融合",
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> KnowledgeWorkspace:
    """使用多模态大模型先识图读笔记，再按考点大纲对全量题目智能分类与知识点融合。"""
    items = validate_questions(questions)
    client = MultimodalModelClient(profile)

    def _report(pct: int, msg: str):
        if progress_cb:
            progress_cb(pct, msg)

    # 1. 多模态识别/提炼未分析的笔记
    processed_notes: list[NoteItem] = []
    for idx, note in enumerate(notes):
        if note.ocr_summary or (note.source_type != "image" and not note.raw_text.strip()):
            processed_notes.append(note)
            continue
        _report(10 + int(25 * idx / max(1, len(notes))), f"正在用多模态模型解析课堂笔记：{note.title}…")
        processed_notes.append(analyze_note_item(note, client))

    notes_digest = "\n".join(
        f"- 笔记ID={n.id} | 标题={n.title} | 建议考点={n.suggested_topic} | 精要={(n.ocr_summary or n.raw_text)[:360]} | 公式={'; '.join(n.formulas[:4])}"
        for n in processed_notes
    ) or "（暂无导入外部课堂笔记，请根据题目内容自身知识体系按核心考点归纳分类）"

    # 2. 构建统一考点大纲
    _report(38, "正在综合课堂笔记与题库结构生成考点知识大纲…")
    sample_qs = "\n".join(_question_brief(q) for q in items[:30])
    extra_hint = f"\n用户附加要求：{profile.custom_prompt}" if profile.custom_prompt else ""
    outline_prompt = (
        f"你是一位擅长考前复习整理的名师，请结合【课堂笔记摘要】与【题库抽样】，设计一套清晰、互相不重叠的考试【考点知识大纲】（建议 4~14 个考点）。{extra_hint}\n"
        "请严格只输出一个 JSON 对象：\n"
        '{"topics": [{"name": "考点名称（简洁明确）", "importance": "核心高频|重要掌握|基础了解", '
        '"summary_md": "融合对应课堂笔记与考题规律的考点核心精要总结（Markdown）", '
        '"formulas": ["关键公式或核心结论1"], "note_ids": ["匹配笔记ID"]}]} \n\n'
        f"【课堂笔记摘要】\n{notes_digest}\n\n【题库抽样（共 {len(items)} 题）】\n{sample_qs}"
    )
    outline_raw = client.chat(outline_prompt, timeout=120.0)
    outline_obj = _extract_json_from_llm(outline_raw)
    raw_topics = outline_obj.get("topics") if isinstance(outline_obj, dict) else outline_obj
    if not isinstance(raw_topics, list) or not raw_topics:
        raise RuntimeError("模型未能生成有效考点大纲，请检查模型名称或切换文本/视觉能力更强的模型")

    topics: list[KnowledgeTopic] = []
    seen_names: set[str] = set()
    valid_note_ids = {n.id for n in processed_notes}
    for idx, rt in enumerate(raw_topics, 1):
        if not isinstance(rt, dict):
            continue
        t = KnowledgeTopic.from_dict(
            {
                "id": f"tp_{idx}",
                "name": rt.get("name") or f"考点 {idx}",
                "importance": rt.get("importance") or "重要掌握",
                "summary_md": rt.get("summary_md") or "",
                "formulas": rt.get("formulas") or [],
                "note_ids": [nid for nid in (rt.get("note_ids") or []) if nid in valid_note_ids],
                "question_ids": [],
            }
        )
        if t.name not in seen_names:
            seen_names.add(t.name)
            topics.append(t)

    topic_names = [t.name for t in topics]
    topic_by_name = {t.name: t for t in topics}

    # 3. 批量智能分类全量题目（严格保证不丢一题）
    batch_size = max(5, profile.batch_size)
    assigned_map: dict[str, str] = {}
    total_batches = (len(items) + batch_size - 1) // batch_size

    for b_idx in range(total_batches):
        chunk = items[b_idx * batch_size : (b_idx + 1) * batch_size]
        pct = 45 + int(45 * b_idx / max(1, total_batches))
        _report(pct, f"正在按考点智能分类第 {b_idx + 1}/{total_batches} 批题目（{len(chunk)} 题）…")
        q_lines = "\n".join(_question_brief(q) for q in chunk)
        cls_prompt = (
            "请将下面每一道题精确分配到给定的【候选考点列表】中最匹配的一项。\n"
            f"候选考点列表：{json.dumps(topic_names, ensure_ascii=False)}\n"
            '请严格只输出 JSON：{"assignments": {"题目ID": "候选考点名称"}}\n\n'
            f"【待分类题目】\n{q_lines}"
        )
        cls_raw = client.chat(cls_prompt, timeout=120.0)
        try:
            cls_obj = _extract_json_from_llm(cls_raw)
            assigns = cls_obj.get("assignments") if isinstance(cls_obj, dict) else {}
            if isinstance(assigns, dict):
                for qid, tname in assigns.items():
                    qid_s = str(qid).strip()
                    tname_s = str(tname).strip()
                    if tname_s in topic_by_name:
                        assigned_map[qid_s] = tname_s
        except Exception:
            pass

    # 兜底：任何未被精确映射的题目按关键词匹配至最相近考点，决不静默丢弃
    topic_tokens = {
        t.name: _tokenize_zh(f"{t.name} {t.summary_md} {' '.join(t.formulas)}")
        for t in topics
    }
    for q in items:
        target_name = assigned_map.get(q.id)
        if not target_name or target_name not in topic_by_name:
            q_tok = _tokenize_zh(f"{q.chapter_name} {html_to_plain_text(q.subject_html)}")
            target_name = max(topic_names, key=lambda tn: len(q_tok & topic_tokens[tn]))
        topic_by_name[target_name].question_ids.append(q.id)

    non_empty_topics = [t for t in topics if t.question_ids or t.note_ids]
    _report(96, f"已完成 {len(items)} 道题目与 {len(processed_notes)} 份笔记的多模态考点融合！")
    return KnowledgeWorkspace(
        title=title,
        source_label=f"{profile.provider}:{profile.model}",
        questions=items,
        notes=processed_notes,
        topics=non_empty_topics or topics,
    )


def build_workbench_handoff_pack(workspace: KnowledgeWorkspace, custom_prompt: str = "") -> dict:
    """生成可直接粘贴给通义千问 / 腾讯元宝 / WorkBuddy / DeepSeek 的紧凑考点融合提示词及本地任务目录。"""
    items = validate_questions(workspace.questions)
    if not items:
        raise ValueError("请先载入题库后再生成 AI 整理指令")

    handoff_dir = get_handoff_dir()
    image_paths: list[str] = []
    note_lines: list[str] = []
    for idx, n in enumerate(workspace.notes, 1):
        img_note = ""
        if n.image_data_uri and "," in n.image_data_uri:
            fname = f"note_{idx:02d}_{safe_archive_segment(n.title)}.jpg"
            fp = handoff_dir / fname
            fp.write_bytes(base64.b64decode(n.image_data_uri.split(",", 1)[1]))
            image_paths.append(str(fp))
            img_note = f"（板书图片已导出至：{fname}）"
        body = (n.ocr_summary or n.raw_text or "").strip()[:1500]
        note_lines.append(f"### 笔记 {idx}：《{n.title}》{img_note}\n{body or '（见对应板书照片）'}")

    q_lines: list[str] = []
    for idx, q in enumerate(items, 1):
        stem = html_to_plain_text(q.subject_html)[:160].replace("\n", " ")
        ans = html_to_plain_text(q.answer)[:60].replace("\n", " ")
        chap = (q.chapter_name or "未分类").strip()
        q_lines.append(f"[Q{idx}] ({q.question_type}|{chap}) {stem} [答:{ans}]")

    extra = f"\n用户附加整理偏好：{custom_prompt.strip()}" if (custom_prompt or "").strip() else ""
    prompt_text = (
        f"你是一位擅长考前复习梳理的名师。请把下面【课堂笔记】与【共 {len(items)} 道考题（编号 Q1~Q{len(items)}）】"
        f"融合整理为按【核心考点】归纳的「先知识点精讲、后紧接对应真题」复习结构。{extra}\n"
        "请尽量输出一个 ```json ... ``` 代码块，格式如下（questions 数组里填题号如 \"Q1\"、\"Q2-Q8\"，尽量覆盖全部题目）：\n"
        "```json\n"
        '{"topics": [\n'
        '  {"name": "考点名称", "importance": "核心高频|重要掌握|基础了解",\n'
        '   "summary_md": "融合课堂笔记的考点精要讲解（Markdown）",\n'
        '   "formulas": ["关键公式或口诀"],\n'
        '   "questions": ["Q1", "Q2-Q5"]}\n'
        "]}\n"
        "```\n\n"
        "## 一、课堂笔记资料\n"
        + ("\n\n".join(note_lines) if note_lines else "（本次未导入额外笔记，请直接提炼各考点核心原理、易错辨析与记忆口诀）")
        + "\n\n## 二、题库全量摘要\n"
        + "\n".join(q_lines)
    )

    bundle_md = handoff_dir / "待整理题库与课堂笔记.md"
    bundle_md.write_text(prompt_text, encoding="utf-8")
    inst_txt = handoff_dir / "WorkBuddy一键指令.txt"
    inst_txt.write_text(
        "请打开 WorkBuddy / 通义千问，直接将本目录下的「待整理题库与课堂笔记.md」（以及 note_*.jpg 板书图，如有）"
        "拖入对话，或粘贴「待整理题库与课堂笔记.md」正文后把 AI 生成结果复制回本软件的「提取 / 粘贴回填 AI 结果」即可自动生成考点树与多格式讲义。",
        encoding="utf-8",
    )
    return {
        "handoff_dir": str(handoff_dir),
        "bundle_md": str(bundle_md),
        "image_paths": image_paths,
        "prompt_text": prompt_text,
        "question_count": len(items),
        "note_count": len(workspace.notes),
    }


def _expand_question_refs(refs: object, items: list[Question]) -> list[str]:
    """解析 'Q1' / 'Q1-Q5' / '1~4' / '#3' / '第3题' / 原题 ID，返回规范 Question.id 列表。"""
    id_set = {q.id for q in items}
    idx_to_id = {i: q.id for i, q in enumerate(items, 1)}
    tokens = refs if isinstance(refs, list) else re.split(r"[,，;；\s]+", str(refs or ""))
    out: list[str] = []

    def _add_idx(n: int):
        qid = idx_to_id.get(n)
        if qid and qid not in out:
            out.append(qid)

    for raw in tokens:
        s = str(raw or "").strip()
        if not s:
            continue
        if s in id_set and s not in out:
            out.append(s)
            continue
        if s.isdigit():
            _add_idx(int(s))
            continue
        matched = False
        for m in _Q_REF_TOKEN_RE.finditer(s):
            matched = True
            if m.group(1) and m.group(2):
                a, b = int(m.group(1)), int(m.group(2))
                lo, hi = min(a, b), max(a, b)
                for n in range(lo, hi + 1):
                    _add_idx(n)
            else:
                num = m.group(1) or m.group(3) or m.group(4)
                if num:
                    _add_idx(int(num))
        if not matched and "-" in s:
            parts = [p.strip() for p in s.split("-", 1)]
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                lo, hi = min(int(parts[0]), int(parts[1])), max(int(parts[0]), int(parts[1]))
                for n in range(lo, hi + 1):
                    _add_idx(n)
    return out


def parse_external_assistant_reply(
    raw_text: str,
    questions: list[Question],
    notes: list[NoteItem],
    *,
    title: str = "AI 考点智能融合",
    source_label: str = "千问/WorkBuddy 智能体融合",
) -> KnowledgeWorkspace:
    """容错解析通义千问 / 腾讯元宝 / WorkBuddy / DeepSeek 的 JSON 或 Markdown 回复，严格保证不丢一题。"""
    items = validate_questions(questions)
    if not items:
        raise ValueError("当前尚未载入题库，无法映射 AI 分类结果")
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("未获取到 AI 回复内容，请先复制或生成 AI 的分类回复")

    topics: list[KnowledgeTopic] = []
    parsed_json = None
    try:
        parsed_json = _extract_json_from_llm(text)
    except Exception:
        parsed_json = None

    raw_topics = None
    if isinstance(parsed_json, dict) and isinstance(parsed_json.get("topics"), list):
        raw_topics = parsed_json["topics"]
    elif isinstance(parsed_json, list):
        raw_topics = parsed_json

    if isinstance(raw_topics, list) and raw_topics:
        for idx, rt in enumerate(raw_topics, 1):
            if not isinstance(rt, dict):
                continue
            qids = _expand_question_refs(rt.get("questions") or rt.get("question_ids") or [], items)
            t = KnowledgeTopic.from_dict(
                {
                    "id": f"tp_{idx}",
                    "name": rt.get("name") or f"考点 {idx}",
                    "importance": rt.get("importance") or "重要掌握",
                    "summary_md": rt.get("summary_md") or rt.get("summary") or "",
                    "formulas": rt.get("formulas") or [],
                    "note_ids": [],
                    "question_ids": qids,
                }
            )
            topics.append(t)
    else:
        sections = re.split(r"(?m)^#{2,3}\s+", text)
        for sec in sections:
            sec = sec.strip()
            if not sec or "\n" not in sec:
                continue
            head, body = sec.split("\n", 1)
            tname = re.sub(r"^(?:\d+[.、)）]\s*|考点[一二三四五六七八九十\d]+[：:.]?\s*)", "", head).strip()
            tname = re.sub(r"\[(?:核心高频|重要掌握|基础了解)\]", "", tname).strip()
            if not tname or len(tname) > 48:
                continue
            qids = _expand_question_refs(re.findall(r"Q\d+(?:\s*[-~]\s*Q?\d+)?|第\s*\d+\s*题|#\d+", body), items)
            imp = "核心高频" if "高频" in head else ("基础了解" if "基础" in head else "重要掌握")
            topics.append(
                KnowledgeTopic(
                    id=f"tp_{len(topics) + 1}",
                    name=tname,
                    importance=imp,
                    summary_md=body.strip()[:1200],
                    formulas=[],
                    note_ids=[],
                    question_ids=qids,
                )
            )

    if not topics:
        raise ValueError("未能从回复中识别出考点结构（建议点击助手弹窗的『① 填入指令』生成规范格式后重试）")

    seen_qids: set[str] = set()
    for t in topics:
        dedup = []
        for qid in t.question_ids:
            if qid not in seen_qids:
                seen_qids.add(qid)
                dedup.append(qid)
        t.question_ids = dedup

    topic_names = [t.name for t in topics]
    topic_by_name = {t.name: t for t in topics}
    topic_tokens = {t.name: _tokenize_zh(f"{t.name} {t.summary_md} {' '.join(t.formulas)}") for t in topics}
    for q in items:
        if q.id in seen_qids:
            continue
        q_tok = _tokenize_zh(f"{q.chapter_name} {html_to_plain_text(q.subject_html)}")
        best_name = max(topic_names, key=lambda tn: len(q_tok & topic_tokens[tn]))
        topic_by_name[best_name].question_ids.append(q.id)

    note_tokens = {n.id: _tokenize_zh(f"{n.title} {n.suggested_topic} {n.ocr_summary} {n.raw_text[:1000]}") for n in notes}
    for t in topics:
        tt = topic_tokens[t.name]
        t.note_ids = [
            n.id
            for n in notes
            if t.name in n.title or n.title in t.name or t.name == n.suggested_topic or len(tt & note_tokens[n.id]) >= 2
        ]

    non_empty = [t for t in topics if t.question_ids or t.note_ids]
    return KnowledgeWorkspace(
        title=title,
        source_label=source_label,
        questions=items,
        notes=notes,
        topics=non_empty or topics,
    )


def _render_single_question_html(q: Question, global_q_idx: int, variant: str) -> str:
    stem_rendered, stem_rich = _render_text_or_html(q.subject_html)
    stem_cls = "content rich" if stem_rich else "content"

    opt_parts: list[str] = []
    if q.has_options and q.options:
        for o_idx, opt in enumerate(q.options):
            opt_rendered, opt_rich = _render_text_or_html(opt.html)
            is_correct = bool(opt.is_true and variant == "withAnswersRed")
            label = f'<span class="option-label">{chr(65 + o_idx)}.</span>' if q.is_choice else ""
            cls = "option correct" if is_correct else "option"
            body_cls = "option-body" + (" labeled" if label else "") + (" rich" if opt_rich else "")
            opt_parts.append(f'<div class="{cls}">{label}<div class="{body_cls}">{opt_rendered}</div></div>')

    answer_block = ""
    if variant != "withoutAnswers":
        ans_rendered, ans_rich = _render_text_or_html(q.answer)
        ans_cls = "content rich" if ans_rich else "content"
        answer_block = f'<div class="label">答案</div><div class="{ans_cls}">{ans_rendered}</div>'

    analysis_block = ""
    if variant != "withoutAnswers" and has_visible_rich_content(q.analysis_html or ""):
        ana_rendered, ana_rich = _render_text_or_html(q.analysis_html)
        ana_cls = "content rich" if ana_rich else "content"
        analysis_block = f'<div class="label">解析</div><div class="{ana_cls}">{ana_rendered}</div>'

    return (
        f'<article><h3>第 {global_q_idx} 题 <span>（{html_lib.escape(q.question_type)} · {html_lib.escape(q.id)}）</span></h3>'
        f'<div class="{stem_cls}">{stem_rendered}</div>'
        f'{"".join(opt_parts)}'
        f'{answer_block}'
        f'{analysis_block}'
        f'</article>'
    )


def _render_topic_note_card_html(
    tp: KnowledgeTopic,
    n_by_id: dict[str, NoteItem],
    *,
    include_notes: bool,
    include_note_images: bool,
) -> str:
    if not include_notes:
        return ""
    note_parts: list[str] = []
    if tp.summary_md.strip():
        note_parts.append(f"<div class='note-summary'>{html_lib.escape(tp.summary_md.strip())}</div>")
    if tp.formulas:
        f_items = "".join(f"<li>{html_lib.escape(f)}</li>" for f in tp.formulas)
        note_parts.append(f"<div class='formula-box'><strong>核心公式与结论：</strong><ul>{f_items}</ul></div>")
    if include_note_images:
        img_tags = []
        for nid in tp.note_ids:
            n = n_by_id.get(nid)
            if n and n.image_data_uri:
                img_tags.append(
                    f"<figure class='board-fig'><img src='{html_lib.escape(n.image_data_uri, quote=True)}' alt='{html_lib.escape(n.title)}' />"
                    f"<figcaption>{html_lib.escape(n.title)}</figcaption></figure>"
                )
        if img_tags:
            note_parts.append(f"<div class='board-grid'>{''.join(img_tags)}</div>")
    if not note_parts:
        return ""
    return f"<div class='note-card'><div class='note-head'>课堂笔记与考点精要</div>{''.join(note_parts)}</div>"


def build_fused_topic_html(
    workspace: KnowledgeWorkspace,
    *,
    variant: str = "withAnswersRed",
    layout_mode: str = "inline",
    smart_pagination: bool = True,
    include_notes: bool = True,
    include_questions: bool = True,
    include_note_images: bool = True,
) -> str:
    """构建按【考点目录 -> 课堂笔记精要与公式 -> 对应原题与解析】重排的高清 A4 讲义 HTML（支持讲练一体/先练后对/讲练分册）。"""
    q_by_id = {q.id: q for q in workspace.questions}
    n_by_id = {n.id: n for n in workspace.notes}
    if variant not in ("withAnswersRed", "withAnswersPlain", "withoutAnswers"):
        raise ValueError(f"不支持的答案模式：{variant}")
    layout = layout_mode if layout_mode in ("inline", "compact_answers_end", "split") else "inline"

    blocks: list[str] = []
    toc_rows = []
    for idx, tp in enumerate(workspace.topics, 1):
        toc_rows.append(
            f"<li><strong>{idx}. {html_lib.escape(tp.name)}</strong> "
            f"<span class='tp-badge {html_lib.escape(tp.importance)}'>{html_lib.escape(tp.importance)}</span> "
            f"<span class='tp-count'>· {len(tp.question_ids)} 题 / {len(tp.note_ids)} 笔记</span></li>"
        )
    blocks.append(f"<section class='toc-box'><div class='toc-title'>考点复习总览</div><ul>{''.join(toc_rows)}</ul></section>")

    global_q_idx = 0
    if layout == "split":
        if include_notes:
            blocks.append("<div class='part-banner'>第一部分 · 全考点课堂笔记精讲串讲</div>")
            for idx, tp in enumerate(workspace.topics, 1):
                blocks.append(
                    f"<section class='topic-head'><h2>{idx}. {html_lib.escape(tp.name)} "
                    f"<span class='tp-badge {html_lib.escape(tp.importance)}'>{html_lib.escape(tp.importance)}</span></h2>"
                    + _render_topic_note_card_html(tp, n_by_id, include_notes=True, include_note_images=include_note_images)
                    + "</section>"
                )
        if include_questions:
            blocks.append("<div class='part-banner page-break'>第二部分 · 分考点真题专项训练与解析</div>")
            for idx, tp in enumerate(workspace.topics, 1):
                blocks.append(
                    f"<section class='topic-head'><h2>{idx}. {html_lib.escape(tp.name)} · 配套真题 "
                    f"<span class='tp-count'>({len(tp.question_ids)} 题)</span></h2></section>"
                )
                for qid in tp.question_ids:
                    q = q_by_id.get(qid)
                    if not q:
                        continue
                    global_q_idx += 1
                    blocks.append(_render_single_question_html(q, global_q_idx, variant))
    else:
        for idx, tp in enumerate(workspace.topics, 1):
            blocks.append(
                f"<section class='topic-head'><h2>{idx}. {html_lib.escape(tp.name)} "
                f"<span class='tp-badge {html_lib.escape(tp.importance)}'>{html_lib.escape(tp.importance)}</span></h2>"
                + _render_topic_note_card_html(tp, n_by_id, include_notes=include_notes, include_note_images=include_note_images)
                + "</section>"
            )
            if not include_questions:
                continue
            q_var = "withoutAnswers" if layout == "compact_answers_end" else variant
            ans_rows: list[str] = []
            for qid in tp.question_ids:
                q = q_by_id.get(qid)
                if not q:
                    continue
                global_q_idx += 1
                blocks.append(_render_single_question_html(q, global_q_idx, q_var))
                if layout == "compact_answers_end" and variant != "withoutAnswers":
                    ans_txt = html_lib.escape(html_to_plain_text(q.answer))
                    ana_txt = html_lib.escape(html_to_plain_text(q.analysis_html))
                    ans_rows.append(
                        f"<li><strong>第 {global_q_idx} 题：</strong><span class='ans-pill'>{ans_txt}</span>"
                        + (f" <span class='ans-ana'>— {ana_txt}</span>" if ana_txt else "")
                        + "</li>"
                    )
            if layout == "compact_answers_end" and ans_rows:
                blocks.append(
                    f"<div class='ans-check-box'><div class='ans-check-head'>本考点参考答案与解析核对</div><ul>{''.join(ans_rows)}</ul></div>"
                )

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>'
        '@page { size: A4; margin: 18mm 18mm; }'
        '@media screen { html, body { width: 174mm; max-width: 174mm; margin: 0 auto; padding: 0; } }'
        '* { box-sizing: border-box; }'
        'body { margin: 0; font-family: "Microsoft YaHei UI", "Noto Sans SC", sans-serif; font-size: 10.5pt; line-height: 1.72; color: #141413; overflow-wrap: anywhere; background: #fff; }'
        'h1 { font-size: 15pt; font-weight: 700; margin: 0 0 12pt; padding-bottom: 8pt; border-bottom: 1.5px solid #e5e7eb; }'
        'h2 { font-size: 12pt; font-weight: 700; margin: 14pt 0 8pt; color: #1e293b; background: #f8fafc; border-left: 3.5px solid #1e6b4e; padding: 5pt 10pt; border-radius: 4px; break-after: avoid; }'
        'h3 { font-size: 10.5pt; font-weight: 700; margin: 10pt 0 4pt; break-after: avoid; }'
        'h3 span { color: #64748b; font-weight: normal; font-size: 8.5pt; }'
        '.part-banner { margin: 16pt 0 10pt; padding: 7pt 12pt; background: #141413; color: #fff; font-weight: 700; font-size: 11.5pt; border-radius: 4px; break-after: avoid; }'
        '.page-break { page-break-before: always; break-before: page; }'
        '.toc-box { background: #faf9f6; border: 1px solid #e2dfd7; border-radius: 6px; padding: 10pt 12pt; margin-bottom: 14pt; }'
        '.toc-title { font-weight: 700; font-size: 10.5pt; margin-bottom: 6pt; }'
        '.toc-box ul { margin: 0; padding-left: 16pt; } .toc-box li { margin: 2pt 0; font-size: 9.5pt; }'
        '.tp-badge { display: inline-block; padding: 0 5pt; border-radius: 3px; font-size: 8pt; font-weight: 600; background: #eae7e0; color: #141413; margin-left: 4pt; }'
        '.tp-badge.核心高频 { background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; }'
        '.tp-badge.重要掌握 { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }'
        '.tp-count { font-size: 8.5pt; color: #64748b; }'
        '.note-card { background: #faf9f6; border: 1px solid #e2dfd7; border-radius: 6px; padding: 9pt 11pt; margin: 6pt 0 12pt; break-inside: avoid; }'
        '.note-head { font-size: 9pt; font-weight: 700; color: #1e6b4e; margin-bottom: 4pt; letter-spacing: 0.03em; }'
        '.note-summary { white-space: pre-wrap; font-size: 9.8pt; color: #272624; line-height: 1.65; }'
        '.formula-box { margin-top: 6pt; padding: 6pt 8pt; background: #fff; border: 1px dashed #cbd5e1; border-radius: 4px; font-size: 9.5pt; }'
        '.formula-box ul { margin: 4pt 0 0; padding-left: 15pt; }'
        '.ans-check-box { margin: 10pt 0 14pt; padding: 8pt 11pt; background: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; }'
        '.ans-check-head { font-size: 9.5pt; font-weight: 700; color: #047857; margin-bottom: 4pt; }'
        '.ans-check-box ul { margin: 0; padding-left: 15pt; font-size: 9.2pt; } .ans-pill { color: #b91c1c; font-weight: 700; } .ans-ana { color: #475569; }'
        '.board-grid { display: flex; flex-wrap: wrap; gap: 8pt; margin-top: 8pt; }'
        '.board-fig { margin: 0; width: 48%; max-width: 48%; text-align: center; } .board-fig img { max-width: 100%; max-height: 180pt; border: 1px solid #e2dfd7; border-radius: 4px; }'
        '.board-fig figcaption { font-size: 8pt; color: #64748b; margin-top: 2pt; }'
        '.content, .option { white-space: pre-wrap; font-size: 10.5pt; line-height: 1.72; }'
        '.rich, .rich *:not(sub):not(sup), .content *:not(sub):not(sup), .option-body, .option-body *:not(sub):not(sup) { font-family: inherit !important; font-size: 10.5pt !important; line-height: 1.72 !important; }'
        'sub, sup { font-size: 7.5pt !important; line-height: 0 !important; }'
        '.rich { white-space: normal; } .rich p { margin: 0 0 4pt; }'
        '.option { margin: 3.5pt 0 3.5pt 0.8em; padding: 2pt 6pt; border-radius: 4px; }'
        '.option.correct { color: #b91c1c; font-weight: 600; background: #fef2f2; border-left: 2.5px solid #ef4444; }'
        '.option-label { display: inline-block; min-width: 1.8em; font-weight: 600; } .option-body { display: inline; } .option-body.rich p:first-child { display: inline; }'
        'img, svg { max-width: 100%; height: auto; vertical-align: middle; }'
        'table { border-collapse: collapse; max-width: 100%; margin: 6pt 0; font-size: 9.5pt; } td, th { border: 1px solid #cbd5e1; padding: 4px 7px; font-size: 9.5pt !important; }'
        '.label { display: block; width: fit-content; margin: 7pt 0 3pt; padding: 1pt 6pt; border-radius: 4px; background: #ecfdf5; border: 1px solid #a7f3d0; font-weight: 700; font-size: 9pt; color: #047857; break-after: avoid; }'
        'article { margin-bottom: 12pt; padding-bottom: 8pt; border-bottom: 1px dashed #e2e8f0; } article:last-child { border-bottom: none; }'
        '</style></head>'
        f'<body data-smart-pagination="{"1" if smart_pagination else "0"}"><h1>{html_lib.escape(workspace.title)}（考点融合版）</h1>{"".join(blocks)}</body></html>'
    )


def _docx_para(
    text: str,
    *,
    bold: bool = False,
    color: str = "141413",
    size_half_pt: int = 21,
    spacing_after: int = 80,
    shade_fill: str = "",
    left_border: str = "",
    indent_left: int = 0,
    page_break_before: bool = False,
) -> str:
    clean = _clean_xml_text(text)
    ppr_parts = [f'<w:spacing w:after="{spacing_after}" w:line="320" w:lineRule="auto"/>']
    if page_break_before:
        ppr_parts.append("<w:pageBreakBefore/>")
    if shade_fill:
        ppr_parts.append(f'<w:shd w:val="clear" w:color="auto" w:fill="{shade_fill}"/>')
    if left_border:
        ppr_parts.append(f'<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="{left_border}"/></w:pBdr>')
    if indent_left:
        ppr_parts.append(f'<w:ind w:left="{indent_left}"/>')
    rpr = f'<w:rPr><w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:hAnsi="Microsoft YaHei"/><w:color w:val="{color}"/><w:sz w:val="{size_half_pt}"/><w:szCs w:val="{size_half_pt}"/>' + ("<w:b/>" if bold else "") + "</w:rPr>"
    lines = clean.split("\n")
    runs = []
    for i, ln in enumerate(lines):
        runs.append(f'<w:r>{rpr}<w:t xml:space="preserve">{xml_escape(ln)}</w:t></w:r>')
        if i < len(lines) - 1:
            runs.append("<w:r><w:br/></w:r>")
    return f'<w:p><w:pPr>{"".join(ppr_parts)}</w:pPr>{"".join(runs)}</w:p>'


def export_knowledge_to_docx(
    workspace: KnowledgeWorkspace,
    docx_path: str | Path,
    *,
    variant: str = "withAnswersRed",
    layout_mode: str = "inline",
    include_notes: bool = True,
    include_questions: bool = True,
) -> Path:
    """生成纯正 Word (.docx) 考点融合讲义：支持【知识点精讲后紧接真题】与朱批红标。"""
    q_by_id = {q.id: q for q in workspace.questions}
    layout = layout_mode if layout_mode in ("inline", "compact_answers_end", "split") else "inline"
    body: list[str] = [
        _docx_para(f"{workspace.title} · 考点笔记融合讲义", bold=True, size_half_pt=32, spacing_after=100),
        _docx_para(
            f"共 {len(workspace.topics)} 个核心考点 · {len(workspace.questions)} 道题目 · {len(workspace.notes)} 份课堂笔记（来源：{workspace.source_label or '本地工作台'}）",
            color="57544E",
            size_half_pt=19,
            spacing_after=160,
        ),
    ]

    def _emit_notes(tp: KnowledgeTopic):
        if not include_notes:
            return
        if tp.summary_md.strip():
            body.append(_docx_para(f"【课堂笔记与考点精要】\n{tp.summary_md.strip()}", size_half_pt=20, shade_fill="F7F5F0", left_border="1E6B4E", spacing_after=100))
        if tp.formulas:
            body.append(_docx_para("核心公式与结论：\n" + "\n".join(f"• {f}" for f in tp.formulas), bold=True, color="1E6B4E", size_half_pt=20, shade_fill="FAF9F6", spacing_after=120))

    def _emit_q(q: Question, idx_num: int, q_var: str):
        body.append(_docx_para(f"第 {idx_num} 题（{q.question_type} · {q.id}）", bold=True, size_half_pt=21, spacing_after=40))
        body.append(_docx_para(html_to_plain_text(q.subject_html), size_half_pt=21, spacing_after=60))
        if q.has_options and q.options:
            for o_idx, opt in enumerate(q.options):
                lbl = f"{chr(65 + o_idx)}. " if q.is_choice else "• "
                is_red = bool(opt.is_true and q_var == "withAnswersRed")
                body.append(
                    _docx_para(
                        f"{lbl}{html_to_plain_text(opt.html)}",
                        bold=is_red,
                        color="B91C1C" if is_red else "141413",
                        size_half_pt=20,
                        indent_left=320,
                        spacing_after=40,
                    )
                )
        if q_var != "withoutAnswers":
            body.append(_docx_para(f"【答案】{html_to_plain_text(q.answer)}", bold=True, color="047857", size_half_pt=20, spacing_after=40))
            ana = html_to_plain_text(q.analysis_html)
            if ana:
                body.append(_docx_para(f"【解析】{ana}", color="475569", size_half_pt=19, spacing_after=120))

    q_num = 0
    if layout == "split":
        if include_notes:
            body.append(_docx_para("第一部分 · 全考点课堂笔记精讲", bold=True, color="FFFFFF", size_half_pt=26, shade_fill="141413", spacing_after=140))
            for idx, tp in enumerate(workspace.topics, 1):
                body.append(_docx_para(f"{idx}. {tp.name} [{tp.importance}]", bold=True, size_half_pt=25, shade_fill="EFF5F2", left_border="1E6B4E", spacing_after=100))
                _emit_notes(tp)
        if include_questions:
            body.append(_docx_para("第二部分 · 分考点真题训练与解析", bold=True, color="FFFFFF", size_half_pt=26, shade_fill="141413", spacing_after=140, page_break_before=True))
            for idx, tp in enumerate(workspace.topics, 1):
                body.append(_docx_para(f"{idx}. {tp.name} · 配套真题（{len(tp.question_ids)} 题）", bold=True, size_half_pt=24, shade_fill="F4F3EF", left_border="141413", spacing_after=90))
                for qid in tp.question_ids:
                    q = q_by_id.get(qid)
                    if not q:
                        continue
                    q_num += 1
                    _emit_q(q, q_num, variant)
    else:
        for idx, tp in enumerate(workspace.topics, 1):
            body.append(_docx_para(f"{idx}. {tp.name} [{tp.importance}]", bold=True, size_half_pt=25, shade_fill="EFF5F2", left_border="1E6B4E", spacing_after=100))
            _emit_notes(tp)
            if not include_questions:
                continue
            q_var = "withoutAnswers" if layout == "compact_answers_end" else variant
            compact_rows: list[str] = []
            for qid in tp.question_ids:
                q = q_by_id.get(qid)
                if not q:
                    continue
                q_num += 1
                _emit_q(q, q_num, q_var)
                if layout == "compact_answers_end" and variant != "withoutAnswers":
                    ana = html_to_plain_text(q.analysis_html)
                    compact_rows.append(f"第 {q_num} 题：{html_to_plain_text(q.answer)}" + (f"（{ana}）" if ana else ""))
            if compact_rows:
                body.append(_docx_para("【本考点答案核对】\n" + "\n".join(compact_rows), color="047857", size_half_pt=19, shade_fill="F8FAFC", spacing_after=140))

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body)}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1020" w:right="1020" w:bottom="1020" w:left="1020"/></w:sectPr></w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )
    out = Path(docx_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types.encode("utf-8"))
        zf.writestr("_rels/.rels", rels.encode("utf-8"))
        zf.writestr("word/document.xml", doc_xml.encode("utf-8"))
    return out


def _xlsx_sheet_xml(rows: list[list[str]], col_widths: list[int]) -> str:
    row_xml_parts: list[str] = []
    for r_idx, row in enumerate(rows, 1):
        cells_xml: list[str] = []
        for c_idx, val in enumerate(row):
            clean = _clean_xml_text(val)
            if not clean:
                continue
            ref = f"{_col_index_to_letters(c_idx)}{r_idx}"
            cells_xml.append(
                f'<c r="{ref}" t="inlineStr"' + (' s="1"' if r_idx == 1 else "") + f'><is><t xml:space="preserve">{xml_escape(clean)}</t></is></c>'
            )
        row_xml_parts.append(f'<row r="{r_idx}">{"".join(cells_xml)}</row>')
    cols_xml = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, w in enumerate(col_widths, 1))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<cols>{cols_xml}</cols><sheetData>{"".join(row_xml_parts)}</sheetData></worksheet>'
    )


def export_knowledge_to_xlsx(workspace: KnowledgeWorkspace, xlsx_path: str | Path, *, variant: str = "withAnswersRed") -> Path:
    """导出双工作表 Excel（Sheet1: 考点精要总览 · Sheet2: 分考点配套刷题表）."""
    q_by_id = {q.id: q for q in workspace.questions}
    summary_rows: list[list[str]] = [["考点序号", "考点名称", "考频分级", "配套题数", "课堂笔记与考点精要", "核心公式与结论"]]
    q_rows: list[list[str]] = [["归属考点", "考频分级", "题号", "题型", "题干", "选项A", "选项B", "选项C", "选项D", "选项E", "选项F", "答案", "解析"]]

    global_idx = 0
    for t_idx, tp in enumerate(workspace.topics, 1):
        summary_rows.append([str(t_idx), tp.name, tp.importance, str(len(tp.question_ids)), tp.summary_md, " | ".join(tp.formulas)])
        for qid in tp.question_ids:
            q = q_by_id.get(qid)
            if not q:
                continue
            global_idx += 1
            opts = q.options or []
            opt_cells = [html_to_plain_text(opts[i].html) if i < len(opts) else "" for i in range(6)]
            if variant == "withoutAnswers":
                ans_cell, ana_cell = "", ""
            else:
                ans_cell = normalize_choice_answer_letters(q.answer) if (q.is_choice or is_choice_question(q.question_type)) else html_to_plain_text(q.answer)
                if not ans_cell and opts:
                    ans_cell = "".join(chr(65 + i) for i, o in enumerate(opts) if o.is_true)
                ana_cell = html_to_plain_text(q.analysis_html)
            q_rows.append([tp.name, tp.importance, str(global_idx), q.question_type, html_to_plain_text(q.subject_html), *opt_cells, ans_cell or html_to_plain_text(q.answer), ana_cell])

    s1_xml = _xlsx_sheet_xml(summary_rows, [10, 24, 12, 10, 52, 36])
    s2_xml = _xlsx_sheet_xml(q_rows, [22, 12, 8, 12, 46, 20, 20, 20, 20, 16, 16, 14, 34])
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="考点精要总览" sheetId="1" r:id="rId1"/><sheet name="分考点配套题库" sheetId="2" r:id="rId2"/></sheets></workbook>'
    )
    wb_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFF4F3EF"/></patternFill></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="1" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs>'
        '</styleSheet>'
    )
    out = Path(xlsx_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types.encode("utf-8"))
        zf.writestr("_rels/.rels", root_rels.encode("utf-8"))
        zf.writestr("xl/workbook.xml", wb_xml.encode("utf-8"))
        zf.writestr("xl/_rels/workbook.rels", wb_rels.encode("utf-8"))
        zf.writestr("xl/styles.xml", styles_xml.encode("utf-8"))
        zf.writestr("xl/worksheets/sheet1.xml", s1_xml.encode("utf-8"))
        zf.writestr("xl/worksheets/sheet2.xml", s2_xml.encode("utf-8"))
    return out


def export_knowledge_to_interactive_html(
    workspace: KnowledgeWorkspace,
    html_path: str | Path,
    *,
    include_notes: bool = True,
    include_note_images: bool = True,
) -> Path:
    """导出离线可交互单文件 HTML：每个知识点精讲后紧接配套交互刷题卡，支持【背题精讲】与【自测判分】实时切换。"""
    q_by_id = {q.id: q for q in workspace.questions}
    n_by_id = {n.id: n for n in workspace.notes}
    payload_topics = []
    for tp in workspace.topics:
        note_imgs = []
        if include_note_images:
            for nid in tp.note_ids:
                n = n_by_id.get(nid)
                if n and n.image_data_uri:
                    note_imgs.append({"title": n.title, "uri": n.image_data_uri})
        q_items = []
        for qid in tp.question_ids:
            q = q_by_id.get(qid)
            if not q:
                continue
            q_items.append(
                {
                    "id": q.id,
                    "type": q.question_type,
                    "is_choice": q.is_choice,
                    "stem": html_to_plain_text(q.subject_html),
                    "options": [
                        {"label": chr(65 + i) if q.is_choice else str(i + 1), "text": html_to_plain_text(o.html), "is_true": bool(o.is_true)}
                        for i, o in enumerate(q.options or [])
                    ],
                    "answer": html_to_plain_text(q.answer),
                    "analysis": html_to_plain_text(q.analysis_html),
                }
            )
        payload_topics.append(
            {
                "id": tp.id,
                "name": tp.name,
                "importance": tp.importance,
                "summary": tp.summary_md if include_notes else "",
                "formulas": tp.formulas if include_notes else [],
                "images": note_imgs,
                "questions": q_items,
            }
        )
    safe_json = json.dumps({"title": workspace.title, "topics": payload_topics}, ensure_ascii=False).replace("</", "<\\/")
    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_lib.escape(workspace.title)} · 考点交互刷题讲义</title>
<style>
:root{{--bg:#f4f3ef;--paper:#fff;--ink:#141413;--muted:#6e6b65;--line:#e2dfd7;--jade:#1e6b4e;--red:#b91c1c;}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei UI",system-ui,sans-serif;}}
.bar{{position:sticky;top:0;z-index:20;background:var(--paper);border-bottom:1px solid var(--line);padding:12px 22px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;}}
.bar h1{{margin:0;font-size:16px;}}.tabs{{display:flex;gap:8px;align-items:center;}}.btn{{border:1px solid var(--line);background:#faf9f6;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;}}
.btn.active{{background:var(--ink);color:#fff;border-color:var(--ink);}}.wrap{{max-width:1160px;margin:18px auto;padding:0 16px;display:grid;grid-template-columns:250px 1fr;gap:18px;}}
.toc{{position:sticky;top:72px;align-self:start;background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:12px;}}
.toc a{{display:block;padding:6px 8px;border-radius:5px;color:var(--ink);text-decoration:none;font-size:13px;margin-bottom:4px;}}.toc a:hover{{background:#f4f3ef;}}
.card{{background:var(--paper);border:1px solid var(--line);border-radius:8px;padding:18px;margin-bottom:18px;}}.note{{background:#faf9f6;border-left:3px solid var(--jade);padding:12px 14px;border-radius:6px;margin:10px 0 16px;white-space:pre-wrap;line-height:1.65;}}
.q{{border-top:1px dashed var(--line);padding:12px 0;}}.opt{{display:block;width:100%;text-align:left;padding:7px 10px;margin:5px 0;border:1px solid var(--line);background:#fff;border-radius:6px;cursor:pointer;font-size:13.5px;}}
.opt.ok{{border-color:var(--jade);background:#ecfdf5;color:var(--jade);font-weight:600;}}.opt.bad{{border-color:var(--red);background:#fef2f2;color:var(--red);}}
.ans{{margin-top:8px;padding:8px 10px;background:#f8fafc;border-radius:6px;font-size:13px;display:none;}}.ans.show{{display:block;}}
@media(max-width:820px){{.wrap{{grid-template-columns:1fr;}}.toc{{position:static;}}}}
</style></head><body>
<div class="bar"><h1>{html_lib.escape(workspace.title)} · 知识点+配套真题</h1><div class="tabs"><span id="stat" style="font-size:12.5px;color:var(--muted);"></span>
<button class="btn active" id="mStudy" onclick="setMode('study')">📖 背题精讲模式</button><button class="btn" id="mQuiz" onclick="setMode('quiz')">✍️ 自测刷题模式</button></div></div>
<div class="wrap"><nav class="toc" id="toc"></nav><main id="main"></main></div>
<script>const DATA={safe_json};let MODE='study';const USER_PICK={{}};
function esc(s){{return String(s||'').replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));}}
function setMode(m){{MODE=m;document.getElementById('mStudy').classList.toggle('active',m==='study');document.getElementById('mQuiz').classList.toggle('active',m==='quiz');render();}}
function pickOpt(qid,idx){{USER_PICK[qid]=idx;render();}}
function render(){{
  const toc=document.getElementById('toc'),main=document.getElementById('main');
  let tot=0,right=0,done=0;
  toc.innerHTML='<div style="font-weight:700;font-size:13px;margin-bottom:8px;">考点目录</div>'+DATA.topics.map((t,i)=>`<a href="#t_${{i}}">${{i+1}}. ${{esc(t.name)}} (${{t.questions.length}}题)</a>`).join('');
  main.innerHTML=DATA.topics.map((t,ti)=>`<section class="card" id="t_${{ti}}"><h2 style="margin:0 0 8px;font-size:16px;">${{ti+1}}. ${{esc(t.name)}} <small style="color:var(--jade);">[${{esc(t.importance)}}]</small></h2>`
    +(t.summary||t.formulas.length?`<div class="note">${{esc(t.summary)}}${{t.formulas.length?'<br><b>核心公式：</b>'+t.formulas.map(esc).join(' · '):''}}</div>`:'')
    +(t.images||[]).map(im=>`<figure style="display:inline-block;margin:4px 8px 8px 0;"><img src="${{esc(im.uri)}}" style="max-width:280px;border-radius:6px;border:1px solid var(--line);"><figcaption style="font-size:11px;color:var(--muted);">${{esc(im.title)}}</figcaption></figure>`).join('')
    +t.questions.map((q,qi)=>{{
      tot++;const picked=USER_PICK[q.id];if(picked!==undefined){{done++;if(q.options[picked]&&q.options[picked].is_true)right++;}}
      const showAns=MODE==='study'||picked!==undefined;
      return `<div class="q"><div><b>#${{qi+1}} [${{esc(q.type)}}]</b> ${{esc(q.stem)}}</div>`
        +q.options.map((o,oi)=>{{
          let cls='opt';if(showAns&&o.is_true)cls+=' ok';else if(MODE==='quiz'&&picked===oi&&!o.is_true)cls+=' bad';
          return `<button class="${{cls}}" onclick="pickOpt('${{esc(q.id)}}',${{oi}})">${{esc(o.label)}}. ${{esc(o.text)}}</button>`;
        }}).join('')
        +`<div class="ans ${{showAns?'show':''}}"><b>答案：</b>${{esc(q.answer)}}${{q.analysis?'<br><b>解析：</b>'+esc(q.analysis):''}}</div></div>`;
    }}).join('')+'</section>').join('');
  document.getElementById('stat').textContent=MODE==='quiz'?`已作答 ${{done}}/${{tot}} · 正确 ${{right}} 题`:`共 ${{DATA.topics.length}} 考点 / ${{tot}} 题`;
}}
render();</script></body></html>"""
    out = Path(html_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return out


def export_knowledge_to_markdown(workspace: KnowledgeWorkspace, md_path: str | Path, *, variant: str = "withAnswersRed") -> Path:
    q_by_id = {q.id: q for q in workspace.questions}
    lines = [f"# {workspace.title} · 考点笔记融合讲义\n", f"> 共 {len(workspace.topics)} 个考点 · {len(workspace.questions)} 道题目 · {len(workspace.notes)} 份课堂笔记\n"]
    for idx, tp in enumerate(workspace.topics, 1):
        lines.append(f"\n## {idx}. {tp.name} [{tp.importance}]\n")
        if tp.summary_md.strip():
            lines.append(f"### 课堂笔记与考点精要\n{tp.summary_md.strip()}\n")
        if tp.formulas:
            lines.append("### 核心公式与结论\n" + "\n".join(f"- `{f}`" for f in tp.formulas) + "\n")
        for q_idx, qid in enumerate(tp.question_ids, 1):
            q = q_by_id.get(qid)
            if not q:
                continue
            lines.append(f"### 例题 {q_idx}（{q.question_type} · {q.id}）")
            lines.append(html_to_plain_text(q.subject_html) + "\n")
            if q.has_options and q.options:
                for o_idx, opt in enumerate(q.options):
                    lbl = f"{chr(65 + o_idx)}. " if q.is_choice else "- "
                    mark = " ✅" if (variant == "withAnswersRed" and opt.is_true) else ""
                    lines.append(f"{lbl}{html_to_plain_text(opt.html)}{mark}")
                lines.append("")
            if variant != "withoutAnswers":
                lines.append(f"**答案**：{html_to_plain_text(q.answer)}")
                ana = html_to_plain_text(q.analysis_html)
                if ana:
                    lines.append(f"**解析**：{ana}")
                lines.append("")
    out = Path(md_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def export_knowledge_to_anki_tsv(workspace: KnowledgeWorkspace, tsv_path: str | Path) -> Path:
    def _cell(s: str) -> str:
        return s.replace("\t", " ").replace("\r\n", "<br>").replace("\n", "<br>")

    q_by_id = {q.id: q for q in workspace.questions}
    rows = [
        "#separator:tab",
        "#html:true",
        "#tags column:4",
    ]
    for tp in workspace.topics:
        tag = f"考点::{safe_archive_segment(tp.name).replace(' ', '_')} {tp.importance}"
        if tp.summary_md.strip() or tp.formulas:
            front = f"<b>[考点精要] {html_lib.escape(tp.name)}</b>"
            back_parts = []
            if tp.summary_md.strip():
                back_parts.append(html_lib.escape(tp.summary_md.strip()))
            if tp.formulas:
                back_parts.append("<br><b>核心公式：</b><br>" + "<br>".join(html_lib.escape(f) for f in tp.formulas))
            rows.append(f"{_cell(front)}\t{_cell(''.join(back_parts))}\t笔记精要卡片\t{_cell(tag)}")

        for qid in tp.question_ids:
            q = q_by_id.get(qid)
            if not q:
                continue
            stem = html_to_plain_text(q.subject_html)
            opts = "<br>".join(f"{chr(65 + i)}. {html_to_plain_text(o.html)}" for i, o in enumerate(q.options)) if q.options else ""
            front = f"<b>[{html_lib.escape(tp.name)} · {html_lib.escape(q.question_type)}]</b><br>{html_lib.escape(stem)}" + (f"<br><br>{opts}" if opts else "")
            ans = html_to_plain_text(q.answer)
            ana = html_to_plain_text(q.analysis_html)
            back = f"<b>答案：</b>{html_lib.escape(ans)}" + (f"<br><br><b>解析：</b>{html_lib.escape(ana)}" if ana else "")
            rows.append(f"{_cell(front)}\t{_cell(back)}\t{html_lib.escape(q.question_type)}\t{_cell(tag)}")

    out = Path(tsv_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(rows), encoding="utf-8")
    return out


def export_knowledge_bundle(
    workspace: KnowledgeWorkspace,
    *,
    outputs: list[str],
    variant: str = "withAnswersRed",
    layout_mode: str = "inline",
    smart_pagination: bool = True,
    include_notes: bool = True,
    include_questions: bool = True,
    include_note_images: bool = True,
    output_dir: str | Path | None = None,
) -> list[str]:
    """按用户勾选组合导出（fused_pdf / docx / html / xlsx / md / anki / zip / json），返回实际生成的成品路径列表。"""
    if not workspace.topics:
        raise ValueError("当前知识库还没有考点分组，请先执行智能分类或离线按章节整理")
    inlined_qs = prefetch_and_inline_images(validate_questions(workspace.questions))
    clean_ws = KnowledgeWorkspace(
        title=workspace.title,
        source_label=workspace.source_label,
        questions=inlined_qs,
        notes=workspace.notes,
        topics=workspace.topics,
    )
    out_dir = Path(output_dir) if output_dir else get_export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_archive_segment(clean_ws.title or "AI考点融合")
    created: list[Path] = []
    want = {str(x).lower() for x in (outputs or ["fused_pdf"])}
    layout = layout_mode if layout_mode in ("inline", "compact_answers_end", "split") else "inline"

    if "fused_pdf" in want or "pdf" in want:
        html = build_fused_topic_html(
            clean_ws,
            variant=variant,
            layout_mode=layout,
            smart_pagination=smart_pagination,
            include_notes=include_notes,
            include_questions=include_questions,
            include_note_images=include_note_images,
        )
        pdf_bytes = render_html_to_pdf_bytes(html)
        p = out_dir / f"{stem}_AI考点融合讲义.pdf"
        p.write_bytes(pdf_bytes)
        created.append(p)

    if "docx" in want or "word" in want:
        created.append(
            export_knowledge_to_docx(
                clean_ws,
                out_dir / f"{stem}_AI考点融合讲义.docx",
                variant=variant,
                layout_mode=layout,
                include_notes=include_notes,
                include_questions=include_questions,
            )
        )

    if "html" in want or "quiz_html" in want:
        created.append(
            export_knowledge_to_interactive_html(
                clean_ws,
                out_dir / f"{stem}_交互刷题讲义.html",
                include_notes=include_notes,
                include_note_images=include_note_images,
            )
        )

    if "xlsx" in want or "excel" in want:
        created.append(export_knowledge_to_xlsx(clean_ws, out_dir / f"{stem}_分考点刷题表.xlsx", variant=variant))

    if "anki" in want:
        created.append(export_knowledge_to_anki_tsv(clean_ws, out_dir / f"{stem}_分考点Anki.tsv"))

    if "md" in want:
        created.append(export_knowledge_to_markdown(clean_ws, out_dir / f"{stem}_分考点讲义.md", variant=variant))

    if "json" in want:
        jp = out_dir / f"{stem}_知识图谱.json"
        jp.write_text(json.dumps(clean_ws.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(jp)

    if "zip" in want:
        zp = out_dir / f"{stem}_分考点专题归档.zip"
        q_by_id = {q.id: q for q in clean_ws.questions}
        manifest = {"version": 5, "title": clean_ws.title, "layoutMode": layout, "topicCount": len(clean_ws.topics), "topics": []}
        tmp_tag = uuid.uuid4().hex[:6]
        with zipfile.ZipFile(str(zp), "w", zipfile.ZIP_DEFLATED) as zf:
            for idx, tp in enumerate(clean_ws.topics, 1):
                sub_ws = KnowledgeWorkspace(
                    title=tp.name,
                    source_label=clean_ws.source_label,
                    questions=[q_by_id[qid] for qid in tp.question_ids if qid in q_by_id],
                    notes=clean_ws.notes,
                    topics=[tp],
                )
                sub_html = build_fused_topic_html(
                    sub_ws,
                    variant=variant,
                    layout_mode=layout,
                    smart_pagination=smart_pagination,
                    include_notes=include_notes,
                    include_questions=include_questions,
                    include_note_images=include_note_images,
                )
                pdf_b = render_html_to_pdf_bytes(sub_html)
                arc = f"{idx:02d}_{safe_archive_segment(tp.name)}.pdf"
                zf.writestr(arc, pdf_b)
                manifest["topics"].append({"name": tp.name, "path": arc, "questionCount": len(sub_ws.questions), "sha256": sha256_bytes(pdf_b)})
            tmp_docx = export_knowledge_to_docx(
                clean_ws,
                out_dir / f".tmp_{tmp_tag}.docx",
                variant=variant,
                layout_mode=layout,
                include_notes=include_notes,
                include_questions=include_questions,
            )
            zf.write(str(tmp_docx), "考点融合讲义.docx")
            tmp_docx.unlink(missing_ok=True)
            tmp_html = export_knowledge_to_interactive_html(
                clean_ws,
                out_dir / f".tmp_{tmp_tag}.html",
                include_notes=include_notes,
                include_note_images=include_note_images,
            )
            zf.write(str(tmp_html), "离线交互刷题讲义.html")
            tmp_html.unlink(missing_ok=True)
            md_txt = export_knowledge_to_markdown(clean_ws, out_dir / f".tmp_{tmp_tag}.md", variant=variant)
            zf.write(str(md_txt), "考点融合复习讲义.md")
            md_txt.unlink(missing_ok=True)
            tsv_tmp = export_knowledge_to_anki_tsv(clean_ws, out_dir / f".tmp_{tmp_tag}.tsv")
            zf.write(str(tsv_tmp), "分考点Anki记忆卡.tsv")
            tsv_tmp.unlink(missing_ok=True)
            zf.writestr("知识图谱.json", json.dumps(clean_ws.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
            zf.writestr("导出清单.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        created.append(zp)

    return [str(p) for p in created]
