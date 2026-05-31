"""模型驱动的压缩器。

用 LLM 调用替代简单截取，生成更有信息量的压缩结果。
用于文档摘要和历史记录压缩两个场景。
"""

from .utils import clip

DOCUMENT_COMPRESS_THRESHOLD = 500  # 小于此字符数的文档不做模型压缩
DOCUMENT_SUMMARY_LIMIT = 200
HISTORY_SUMMARY_LIMIT = 300


def compress(model_client, content, instruction, max_tokens=256, fallback=None):
    """调用模型压缩 content。失败时返回 fallback。"""
    prompt = f"{instruction}\n\n{content}"
    try:
        result = model_client.complete(prompt, max_tokens)
        result = str(result).strip()
        if result:
            return result
    except Exception:
        pass
    return fallback


def compress_document(content, model_client):
    """压缩文档内容为摘要。小文件直接返回原文。"""
    content = str(content)
    if len(content) < DOCUMENT_COMPRESS_THRESHOLD:
        return clip(content, DOCUMENT_SUMMARY_LIMIT)

    instruction = (
        "请用中文将以下文档内容压缩为一句话摘要，不超过 100 字。"
        "保留文档的核心主题和关键信息，忽略格式细节。"
    )
    fallback = clip(content, DOCUMENT_SUMMARY_LIMIT)
    return compress(model_client, content, instruction, max_tokens=128, fallback=fallback)


def compress_history(items_text, model_client):
    """压缩历史记录为摘要。"""
    instruction = (
        "请用中文将以下对话历史压缩为简洁摘要，不超过 150 字。"
        "重点关注：读写了哪些文件、执行了什么任务、遇到了什么错误、当前状态是什么。"
    )
    fallback = clip(items_text, HISTORY_SUMMARY_LIMIT)
    return compress(model_client, items_text, instruction, max_tokens=256, fallback=fallback)
