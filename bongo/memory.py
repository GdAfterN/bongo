from __future__ import annotations

from .database import StudyDatabase


SYSTEM_PROMPT = """你是 Bongo，一只住在桌面上的本地助学伙伴。
你的职责是依据用户喂给你的知识资料帮助理解和复习。
回答应准确、清晰、鼓励思考，不虚构资料中不存在的事实。
提供了本地资料片段时，优先依据这些片段回答，并在相关陈述后标注 [来源N]。
如果资料不足以回答，要明确说明不足，并将常识补充与资料内容区分开。
你没有文件、Shell、网络或其他工具能力。"""


class ConversationContext:
    def __init__(self, database: StudyDatabase, max_messages: int = 16, max_context_chars: int = 12000):
        self.database = database
        self.max_messages = max_messages
        self.max_context_chars = max_context_chars

    def build(self, conversation_id: int, user_message: str) -> tuple[list[dict], list[dict], str]:
        conversation = self.database.get_conversation(conversation_id) or {}
        history = self.database.get_messages(conversation_id, limit=self.max_messages)
        source_id = conversation.get("source_id")
        chunks = self.database.search_chunks(user_message, limit=6, source_id=source_id)
        citations = []
        context_parts = []
        used = 0
        for index, chunk in enumerate(chunks, start=1):
            remaining = self.max_context_chars - used
            if remaining <= 0:
                break
            content = chunk["content"][:remaining]
            label = f"来源{index}：{chunk['source_name']}"
            if chunk.get("heading"):
                label += f" / {chunk['heading']}"
            context_parts.append(f"[{label}]\n{content}")
            citations.append({"index": index, "source": chunk["source_name"], "heading": chunk.get("heading", "")})
            used += len(content)

        messages = []
        summary = str(conversation.get("summary", "")).strip()
        if summary:
            messages.append({"role": "user", "content": f"此前对话摘要：\n{summary}"})
        for message in history:
            if message["role"] in {"user", "assistant"}:
                messages.append({"role": message["role"], "content": message["content"]})
        request = user_message
        if context_parts:
            request += "\n\n以下是从当前对话所选资料中检索到的片段：\n\n" + "\n\n".join(context_parts)
        messages.append({"role": "user", "content": request})
        return messages, citations, SYSTEM_PROMPT
