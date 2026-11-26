"""可扩展插件注册中心"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class PluginDescriptor:
    plugin_id: str
    name: str
    version: str
    summary: str
    capabilities: tuple[str, ...]


_PLUGINS: list[PluginDescriptor] = [
    PluginDescriptor(
        plugin_id="ai_knowledge_fusion",
        name="多模态考点智能分类与课堂笔记融合",
        version="5.0.0",
        summary="接入通用 OpenAI 多模态接口或本机 Ollama，读取板书拍照/课件 PDF/PPT/Word 笔记并与导出题库智能按考点重组，生成可视化考点树与融合讲义。",
        capabilities=("multimodal_vision", "ollama_local", "topic_clustering", "pdf_md_anki_zip_export"),
    ),
]


def list_registered_plugins() -> list[dict]:
    return [
        {
            "plugin_id": p.plugin_id,
            "name": p.name,
            "version": p.version,
            "summary": p.summary,
            "capabilities": list(p.capabilities),
        }
        for p in _PLUGINS
    ]
