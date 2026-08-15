from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .activity import WorkSessionTool
from .providers import ConversationProvider


ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["get_current_work_session"]},
        "reason": {"type": "string"},
    },
    "required": ["action", "reason"],
}

BREAK_REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "activity": {"type": "string"},
        "suggestion": {"type": "string"},
    },
    "required": ["summary", "activity", "suggestion"],
}


class AgentAction(BaseModel):
    action: str
    reason: str = Field(min_length=1, max_length=300)


class BreakReport(BaseModel):
    summary: str = Field(min_length=1, max_length=160)
    activity: str = Field(min_length=1, max_length=300)
    suggestion: str = Field(min_length=1, max_length=160)


class WorkBreakAgent:
    """A bounded observe-act-observe loop with one read-only tool."""

    def __init__(self, provider: ConversationProvider, tool: WorkSessionTool):
        self.provider = provider
        self.tool = tool

    def run(self) -> dict[str, Any]:
        action_payload = self.provider.complete(
            [{
                "role": "user",
                "content": (
                    "用户已经连续工作至少40分钟。请选择必要工具获取事实依据，"
                    "再用于生成休息提醒。"
                ),
            }],
            (
                "你是 Bongo 的工作节奏 Agent。你只能选择已注册的只读工具，"
                "不能访问键盘内容、窗口标题、文件或网络。reason 只简述选择工具的原因，"
                "不要输出内部推理过程。"
            ),
            ACTION_SCHEMA,
        )
        action = AgentAction.model_validate(action_payload)
        if action.action != self.tool.name:
            raise ValueError(f"Agent requested an unregistered tool: {action.action}")

        observation = self.tool.execute()
        report_payload = self.provider.complete(
            [{
                "role": "user",
                "content": (
                    "工具 Observation 如下：\n"
                    f"{observation}\n\n"
                    "生成适合桌宠气泡的简短报告。summary 说明连续工作时长和总敲击量；"
                    "activity 根据应用进程分布谨慎推断工作类型；suggestion 明确建议休息。"
                ),
            }],
            (
                "只能依据工具 Observation。应用名只能支持工作类型推断，不能声称知道具体"
                "文件、网站、项目或输入内容。用中文输出，不夸大，不诊断健康问题。"
            ),
            BREAK_REPORT_SCHEMA,
        )
        report = BreakReport.model_validate(report_payload)
        return {
            "action": action.model_dump(),
            "observation": observation,
            "report": report.model_dump(),
        }
