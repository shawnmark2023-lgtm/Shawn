"""多模态 AI 考点智能分类与课堂笔记融合插件引擎（支持 OpenAI 兼容多模态与本机 Ollama）"""
from __future__ import annotations

import base64
import dataclasses
import html as html_lib
import io
import json
import re
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from PIL import Image, ImageOps

from ..core.exporter import export_questions_to_pdf, pack_to_zip, prefetch_and_inline_images, render_html_to_pdf_bytes
from ..core.formats import (
    _docx_bytes_to_text,
    export_questions_to_json,
    html_to_plain_text,
    import_questions_from_file,
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
    "ollama": {
        "label": "本机 Ollama（离线开源多模态 · qwen2.5vl / llama3.2-vision）",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5vl:7b",
    },
    "openai_qwen": {
        "label": "阿里云百炼通义千问（OpenAI 兼容 · qwen-vl-max）",
        "provider": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max-latest",
    },
    "openai_siliconflow": {
        "label": "硅基流动 SiliconFlow（OpenAI 兼容 · Qwen2.5-VL）",
        "provider": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
    },
    "openai_zhipu": {
        "label": "智谱清言 GLM（OpenAI 兼容 · glm-4v-flash）",
        "provider": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4v-flash",
    },
    "openai_official": {
        "label": "OpenAI 官方 / 兼容中转（gpt-4o-mini / gpt-4o）",
        "provider": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "openai_custom": {
        "label": "自定义 OpenAI 兼容多模态服务（OneAPI / vLLM / LM Studio）",
        "provider": "openai",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "gpt-4o-mini",
    },
}

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
_CN_CLOUD_SUFFIXES = (".aliyuncs.com", ".siliconflow.cn", ".bigmodel.cn", ".volces.com", ".deepseek.com", ".baidubce.com", ".myqcloud.com")
_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_+-]{1,}")
_SLIDE_NUM_RE = re.compile(r"slide(\d+)\.xml$", re.I)


@dataclasses.dataclass
class ModelProfile:
    preset: str = "ollama"
    provider: str = "ollama"  # "ollama" | "openai"
    base_url: str = "http://127.0.0.1:11434"
    api_key: str = ""
    model: str = "qwen2.5vl:7b"
    batch_size: int = 24
    custom_prompt: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ModelProfile:
        return cls(
            preset=str(d.get("preset") or "ollama"),
            provider=str(d.get("provider") or "ollama"),
            base_url=str(d.get("base_url") or "http://127.0.0.1:11434").strip().rstrip("/"),
            api_key=str(d.get("api_key") or "").strip(),
            model=str(d.get("model") or "qwen2.5vl:7b").strip(),
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


class MultimodalModelClient:
    """统一多模态大模型调用客户端（兼容 OpenAI 多模态接口与本机 Ollama 原生多模态接口）。"""

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


def build_fused_topic_html(
    workspace: KnowledgeWorkspace,
    *,
    variant: str = "withAnswersRed",
    smart_pagination: bool = True,
    include_notes: bool = True,
    include_questions: bool = True,
    include_note_images: bool = True,
) -> str:
    """构建按【考点目录 -> 课堂笔记精要与公式 -> 对应原题与解析】重排的高清 A4 讲义 HTML。"""
    q_by_id = {q.id: q for q in workspace.questions}
    n_by_id = {n.id: n for n in workspace.notes}
    if variant not in ("withAnswersRed", "withAnswersPlain", "withoutAnswers"):
        raise ValueError(f"不支持的答案模式：{variant}")

    blocks: list[str] = []
    # 考点速览目录卡片
    toc_rows = []
    for idx, tp in enumerate(workspace.topics, 1):
        toc_rows.append(
            f"<li><strong>{idx}. {html_lib.escape(tp.name)}</strong> "
            f"<span class='tp-badge {html_lib.escape(tp.importance)}'>{html_lib.escape(tp.importance)}</span> "
            f"<span class='tp-count'>· {len(tp.question_ids)} 题 / {len(tp.note_ids)} 笔记</span></li>"
        )
    blocks.append(f"<section class='toc-box'><div class='toc-title'>考点复习总览</div><ul>{''.join(toc_rows)}</ul></section>")

    global_q_idx = 0
    for idx, tp in enumerate(workspace.topics, 1):
        blocks.append(
            f"<section class='topic-head'><h2>{idx}. {html_lib.escape(tp.name)} "
            f"<span class='tp-badge {html_lib.escape(tp.importance)}'>{html_lib.escape(tp.importance)}</span></h2>"
        )
        if include_notes:
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
            if note_parts:
                blocks.append(f"<div class='note-card'><div class='note-head'>课堂笔记与考点精要</div>{''.join(note_parts)}</div>")
        blocks.append("</section>")

        if include_questions:
            for qid in tp.question_ids:
                q = q_by_id.get(qid)
                if not q:
                    continue
                global_q_idx += 1
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

                blocks.append(
                    f'<article><h3>第 {global_q_idx} 题 <span>（{html_lib.escape(q.question_type)} · {html_lib.escape(q.id)}）</span></h3>'
                    f'<div class="{stem_cls}">{stem_rendered}</div>'
                    f'{"".join(opt_parts)}'
                    f'{answer_block}'
                    f'{analysis_block}'
                    f'</article>'
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
    smart_pagination: bool = True,
    include_notes: bool = True,
    include_questions: bool = True,
    include_note_images: bool = True,
    output_dir: str | Path | None = None,
) -> list[str]:
    """按用户勾选组合导出（fused_pdf / anki / md / zip / json），返回实际生成的成品路径列表。"""
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

    if "fused_pdf" in want or "pdf" in want:
        html = build_fused_topic_html(
            clean_ws,
            variant=variant,
            smart_pagination=smart_pagination,
            include_notes=include_notes,
            include_questions=include_questions,
            include_note_images=include_note_images,
        )
        pdf_bytes = render_html_to_pdf_bytes(html)
        p = out_dir / f"{stem}_AI考点融合讲义.pdf"
        p.write_bytes(pdf_bytes)
        created.append(p)

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
        manifest = {"version": 5, "title": clean_ws.title, "topicCount": len(clean_ws.topics), "topics": []}
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
                    smart_pagination=smart_pagination,
                    include_notes=include_notes,
                    include_questions=include_questions,
                    include_note_images=include_note_images,
                )
                pdf_b = render_html_to_pdf_bytes(sub_html)
                arc = f"{idx:02d}_{safe_archive_segment(tp.name)}.pdf"
                zf.writestr(arc, pdf_b)
                manifest["topics"].append({"name": tp.name, "path": arc, "questionCount": len(sub_ws.questions), "sha256": sha256_bytes(pdf_b)})
            md_txt = export_knowledge_to_markdown(clean_ws, out_dir / f".tmp_{uuid.uuid4().hex[:6]}.md", variant=variant)
            zf.write(str(md_txt), "考点融合复习讲义.md")
            md_txt.unlink(missing_ok=True)
            tsv_tmp = export_knowledge_to_anki_tsv(clean_ws, out_dir / f".tmp_{uuid.uuid4().hex[:6]}.tsv")
            zf.write(str(tsv_tmp), "分考点Anki记忆卡.tsv")
            tsv_tmp.unlink(missing_ok=True)
            zf.writestr("知识图谱.json", json.dumps(clean_ws.to_dict(), ensure_ascii=False, indent=2).encode("utf-8"))
            zf.writestr("导出清单.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        created.append(zp)

    return [str(p) for p in created]
