"""Tests for v5 Multimodal AI Knowledge Fusion Plugin."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pypdf
from PIL import Image, ImageDraw

from cctr_export.core.exporter import export_questions_to_pdf
from cctr_export.core.models import Option, Question
from cctr_export.plugins import list_registered_plugins
from cctr_export.plugins import ai_knowledge as ak


def _sample_questions() -> list[Question]:
    return [
        Question(
            id="k:1",
            question_type="单选题",
            subject_html="<p>傅里叶变换把信号从时域变换到什么域？</p>",
            answer="A",
            has_options=True,
            options=[
                Option(is_true=True, html="频域"),
                Option(is_true=False, html="空域"),
            ],
            subject_name="信号与系统",
            chapter_name="傅里叶变换",
            analysis_html="<p>核心定义：时域信号对应频谱分布</p>",
            is_choice=True,
        ),
        Question(
            id="k:2",
            question_type="计算题",
            subject_html="<p>利用奈奎斯特采样定理确定带宽 B=4kHz 信号的最小采样频率。</p>",
            answer="<p>f_s >= 2B = 8kHz</p>",
            has_options=False,
            options=[],
            subject_name="信号与系统",
            chapter_name="未分类章节",
            analysis_html="奈奎斯特准则核心公式",
            is_choice=False,
        ),
        Question(
            id="k:3",
            question_type="简答题",
            subject_html="<p>简述香农公式中信道容量与信噪比的关系。</p>",
            answer="C = B log2(1 + S/N)",
            has_options=False,
            options=[],
            subject_name="信号与系统",
            chapter_name="未分类章节",
            analysis_html="",
            is_choice=False,
        ),
    ]


def test_plugin_registry_and_multimodal_note_importers(tmp_path: Path):
    plugins = list_registered_plugins()
    assert plugins and plugins[0]["plugin_id"] == "ai_knowledge_fusion"

    # 1. 板书大图 (>1600px) 自动等比缩放编码 JPEG Data URI
    img_path = tmp_path / "board_notes.png"
    im = Image.new("RGB", (2400, 1200), color=(250, 249, 246))
    d = ImageDraw.Draw(im)
    d.text((60, 60), "Nyquist fs >= 2B", fill=(20, 20, 19))
    im.save(img_path, format="PNG")
    n_img = ak.import_note_file(img_path)
    assert n_img.source_type == "image"
    assert n_img.image_data_uri.startswith("data:image/jpeg;base64,")

    # 2. PPTX 课件幻灯片提取
    pptx_path = tmp_path / "lecture_slides.pptx"
    slide1 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>采样定理与奈奎斯特频率</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
    )
    with zipfile.ZipFile(pptx_path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml", slide1.encode("utf-8"))
    n_pptx = ak.import_note_file(pptx_path)
    assert n_pptx.source_type == "pptx"
    assert "采样定理" in n_pptx.raw_text

    # 3. PDF 讲义抽取 + Markdown 笔记
    qs = _sample_questions()
    pdf_file = export_questions_to_pdf(qs[:1], tmp_path / "handout", title="傅里叶讲义", filename="fourier.pdf")
    n_pdf = ak.import_note_file(pdf_file)
    assert n_pdf.source_type == "pdf"
    assert "傅里叶" in n_pdf.raw_text


def test_multimodal_ollama_and_openai_classification_preserves_all_questions(monkeypatch, tmp_path: Path):
    calls: list[tuple[str, dict]] = []

    class _FakeResp:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload, ensure_ascii=False)

        def json(self):
            return self._payload

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url, *args, **kwargs):
            calls.append(("GET", {"url": url}))
            return _FakeResp(200, {"models": [{"name": "qwen2.5vl:7b", "size": 5 * 1024**3}]})

        def post(self, url, json=None, *args, **kwargs):
            calls.append(("POST", {"url": url, "json": json}))
            msg_text = str(json["messages"][0]["content"])
            # 1) 图片识图分析响应
            if "images" in json["messages"][0] or isinstance(json["messages"][0]["content"], list):
                content = '```json\n{"suggested_topic": "采样与信道容量", "summary_md": "板书：fs>=2B 与香农公式", "key_points": ["最小采样率"], "formulas": ["fs >= 2B"]}\n```'
            # 2) 考点大纲生成响应
            elif "考点知识大纲" in msg_text:
                content = '{"topics": [{"name": "傅里叶频谱", "importance": "核心高频", "summary_md": "时域到频域变换", "formulas": ["F(w)"], "note_ids": []}, {"name": "采样与信道容量", "importance": "重要掌握", "summary_md": "融合板书与公式", "formulas": ["C=Blog2(1+S/N)"], "note_ids": ["img1"]}]}'
            # 3) 批量分类响应（故意只映射 k:1 与 k:2，测试未映射的 k:3 自动兜底不丢失）
            else:
                content = '{"assignments": {"k:1": "傅里叶频谱", "k:2": "采样与信道容量"}}'

            if url.endswith("/api/chat"):
                return _FakeResp(200, {"message": {"content": content}})
            return _FakeResp(200, {"choices": [{"message": {"content": content}}]})

        def close(self):
            pass

    monkeypatch.setattr(ak, "ResilientHttpSession", _FakeSession)

    # 验证 Ollama 模型探测
    models = ak.MultimodalModelClient.probe_ollama_models("http://127.0.0.1:11434")
    assert models == [{"name": "qwen2.5vl:7b", "vision": True, "sizeGb": 5.0}]

    img_buf = io.BytesIO()
    Image.new("RGB", (120, 80), color=(240, 240, 240)).save(img_buf, format="JPEG")
    import base64
    b64_uri = "data:image/jpeg;base64," + base64.b64encode(img_buf.getvalue()).decode("ascii")
    note = ak.NoteItem(id="img1", title="课堂板书1", source_type="image", image_data_uri=b64_uri)

    # 先测 Ollama 多模态链路
    ollama_prof = ak.ModelProfile(provider="ollama", base_url="http://127.0.0.1:11434", model="qwen2.5vl:7b")
    ws_ollama = ak.ai_classify_and_fuse(_sample_questions(), [note], ollama_prof, title="通信考点集")
    all_qids = [qid for tp in ws_ollama.topics for qid in tp.question_ids]
    assert set(all_qids) == {"k:1", "k:2", "k:3"}  # k:3 自动关键词兜底保留，一题不丢
    assert any("images" in c[1]["json"]["messages"][0] for c in calls if c[0] == "POST")

    # 再测 OpenAI 兼容多模态链路（图片以 image_url 发送）
    calls.clear()
    note_openai = ak.NoteItem(id="img2", title="课堂板书2", source_type="image", image_data_uri=b64_uri)
    oai_prof = ak.ModelProfile(provider="openai", base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", api_key="sk-test", model="qwen-vl-max-latest")
    ws_oai = ak.ai_classify_and_fuse(_sample_questions(), [note_openai], oai_prof, title="阿里云百炼融合")
    assert len(ws_oai.topics) >= 1
    assert any(isinstance(c[1]["json"]["messages"][0]["content"], list) for c in calls if c[0] == "POST")


def test_offline_fusion_and_customizable_bundle_exports_and_reload(tmp_path: Path):
    qs = _sample_questions()
    img_buf = io.BytesIO()
    Image.new("RGB", (320, 180), color=(248, 247, 243)).save(img_buf, format="JPEG")
    import base64
    note_img = ak.NoteItem(
        id="n_img",
        title="黑板奈奎斯特公式",
        source_type="image",
        image_data_uri="data:image/jpeg;base64," + base64.b64encode(img_buf.getvalue()).decode("ascii"),
        ocr_summary="奈奎斯特最小采样率与信道带宽两倍成正比",
        formulas=["fs >= 2B"],
        suggested_topic="奈奎斯特采样",
    )
    ws = ak.offline_classify_and_fuse(qs, [note_img], title="信号与系统考前融合")
    assert len(ws.topics) >= 2

    out_dir = tmp_path / "fused_out"
    paths = ak.export_knowledge_bundle(
        ws,
        outputs=["fused_pdf", "anki", "md", "zip", "json"],
        variant="withAnswersRed",
        include_notes=True,
        include_questions=True,
        include_note_images=True,
        output_dir=out_dir,
    )
    assert len(paths) == 5
    by_ext = {Path(p).suffix.lower(): Path(p) for p in paths}

    # 1) 验证融合 PDF 包含笔记精要与原题
    reader = pypdf.PdfReader(str(by_ext[".pdf"]))
    pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "奈奎斯特" in pdf_text and "傅里叶" in pdf_text

    # 2) 验证分考点 Anki 卡片
    tsv_text = by_ext[".tsv"].read_text(encoding="utf-8")
    assert "#separator:tab" in tsv_text and "笔记精要卡片" in tsv_text

    # 3) 验证分考点 Markdown 讲义
    md_text = by_ext[".md"].read_text(encoding="utf-8")
    assert "课堂笔记与考点精要" in md_text and "fs >= 2B" in md_text

    # 4) 验证分考点 ZIP 与 SHA-256 清单
    with zipfile.ZipFile(str(by_ext[".zip"]), "r") as zf:
        names = zf.namelist()
        assert "导出清单.json" in names and "知识图谱.json" in names
        manifest = json.loads(zf.read("导出清单.json").decode("utf-8"))
        for entry in manifest["topics"]:
            assert hashlib.sha256(zf.read(entry["path"])).hexdigest() == entry["sha256"]

    # 5) 验证知识图谱 JSON 可无损回读继续编辑
    reloaded = ak.load_workspace_from_json(by_ext[".json"].read_text(encoding="utf-8"))
    assert reloaded.title == ws.title
    assert len(reloaded.questions) == 3 and len(reloaded.notes) == 1
