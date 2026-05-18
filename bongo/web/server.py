"""Bongo Web API 服务器。

提供RESTful API接口，支持：
- 会话管理（创建、加载、删除、列表）
- 消息发送和接收
- 历史记录查询
- 流式响应支持
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import threading
import queue

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from bongo.models import OpenAICompatibleModelClient
from bongo.runtime import bongo, SessionStore
from bongo.workspace import WorkspaceContext

app = Flask(__name__)
CORS(app)  # 允许跨域请求

# 全局配置
WORKSPACE_ROOT = os.environ.get("BONGO_WORKSPACE", ".")
SESSIONS_DIR = Path(WORKSPACE_ROOT) / ".bongo" / "sessions"
MODEL_PROVIDER = os.environ.get("BONGO_PROVIDER", "openai")
MODEL_NAME = os.environ.get("BONGO_MODEL", "qwen3.5-plus-2026-02-15")
BASE_URL = os.environ.get("BONGO_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
API_KEY = os.environ.get("BONGO_API_KEY", os.environ.get("OPENAI_API_KEY", ""))

# 存储活跃的agent实例
active_agents = {}


def build_model_client():
    """构建模型客户端"""
    return OpenAICompatibleModelClient(
        model=MODEL_NAME,
        base_url=BASE_URL,
        api_key=API_KEY,
        temperature=0.2,
        timeout=300,
    )


def create_or_load_agent(session_id: Optional[str] = None):
    """创建或加载agent实例"""
    workspace = WorkspaceContext.build(WORKSPACE_ROOT)
    store = SessionStore(SESSIONS_DIR)

    if session_id:
        # 尝试加载现有会话
        try:
            agent = bongo.from_session(
                model_client=build_model_client(),
                workspace=workspace,
                session_store=store,
                session_id=session_id,
                approval_policy="auto",  # Web模式下自动批准
                max_steps=6,
                max_new_tokens=512,
            )
            return agent
        except Exception as e:
            print(f"Failed to load session {session_id}: {e}")
            session_id = None

    # 创建新会话
    agent = bongo(
        model_client=build_model_client(),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=6,
        max_new_tokens=512,
    )
    return agent


@app.route('/api/sessions', methods=['GET'])
def list_sessions():
    """获取所有会话列表"""
    try:
        sessions_dir = SESSIONS_DIR
        if not sessions_dir.exists():
            return jsonify({"sessions": []})

        sessions = []
        for session_file in sorted(sessions_dir.glob("*.json"),
                                   key=lambda p: p.stat().st_mtime,
                                   reverse=True):
            try:
                with open(session_file, 'r', encoding='utf-8') as f:
                    session_data = json.load(f)
                    sessions.append({
                        "id": session_data.get("id", session_file.stem),
                        "created_at": session_data.get("created_at", ""),
                        "workspace_root": session_data.get("workspace_root", ""),
                        "message_count": len(session_data.get("history", [])),
                        "last_message": get_last_user_message(session_data),
                    })
            except Exception as e:
                print(f"Error reading session {session_file}: {e}")
                continue

        return jsonify({"sessions": sessions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def get_last_user_message(session_data):
    """获取最后一条用户消息"""
    history = session_data.get("history", [])
    for item in reversed(history):
        if item.get("role") == "user":
            return item.get("content", "")[:100]
    return ""


@app.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    """获取指定会话的详细信息"""
    try:
        store = SessionStore(SESSIONS_DIR)
        session_data = store.load(session_id)

        # 格式化历史记录
        formatted_history = []
        for item in session_data.get("history", []):
            if item["role"] == "user":
                formatted_history.append({
                    "role": "user",
                    "content": item["content"],
                    "timestamp": item.get("created_at", "")
                })
            elif item["role"] == "assistant":
                formatted_history.append({
                    "role": "assistant",
                    "content": item["content"],
                    "timestamp": item.get("created_at", "")
                })
            elif item["role"] == "tool":
                # 工具调用可以作为系统消息或特殊格式
                formatted_history.append({
                    "role": "tool",
                    "name": item.get("name", ""),
                    "args": item.get("args", {}),
                    "content": item["content"],
                    "timestamp": item.get("created_at", "")
                })

        return jsonify({
            "session": {
                "id": session_data.get("id"),
                "created_at": session_data.get("created_at"),
                "workspace_root": session_data.get("workspace_root"),
                "history": formatted_history,
                "memory": session_data.get("memory", {})
            }
        })
    except FileNotFoundError:
        return jsonify({"error": "Session not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    """删除指定会话"""
    try:
        store = SessionStore(SESSIONS_DIR)
        session_path = store.path(session_id)
        if session_path.exists():
            session_path.unlink()
            return jsonify({"success": True})
        else:
            return jsonify({"error": "Session not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/chat', methods=['POST'])
def chat():
    """发送消息并获取回复（非流式）"""
    try:
        data = request.json
        message = data.get("message", "").strip()
        session_id = data.get("session_id")

        if not message:
            return jsonify({"error": "Message cannot be empty"}), 400

        # 创建或加载agent
        agent = create_or_load_agent(session_id)

        # 保存agent实例以便后续使用
        active_agents[agent.session["id"]] = agent

        # 执行ask
        response = agent.ask(message)

        return jsonify({
            "session_id": agent.session["id"],
            "response": response,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/chat/stream', methods=['POST'])
def chat_stream():
    """发送消息并获取流式回复"""
    try:
        data = request.json
        message = data.get("message", "").strip()
        session_id = data.get("session_id")

        if not message:
            return jsonify({"error": "Message cannot be empty"}), 400

        def generate():
            try:
                # 创建或加载agent
                agent = create_or_load_agent(session_id)
                active_agents[agent.session["id"]] = agent

                # 发送session_id
                yield f"data: {json.dumps({'type': 'session', 'session_id': agent.session['id']})}\n\n"

                # 执行ask（目前bongo是同步的，所以一次性返回）
                # TODO: 如果需要真正的流式输出，需要修改bongo的ask方法
                response = agent.ask(message)

                # 发送完整响应
                yield f"data: {json.dumps({'type': 'message', 'content': response})}\n\n"
                yield f"data: {json.dumps({'type': 'done'})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/sessions/<session_id>/memory', methods=['GET'])
def get_session_memory(session_id):
    """获取会话的工作记忆"""
    try:
        store = SessionStore(SESSIONS_DIR)
        session_data = store.load(session_id)
        memory = session_data.get("memory", {})

        return jsonify({
            "memory": {
                "working": memory.get("working", {}),
                "file_summaries": memory.get("file_summaries", {})
            },
            "relevant_notes": session_data.get("relevant_notes", [])
        })
    except FileNotFoundError:
        return jsonify({"error": "Session not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """健康检查"""
    return jsonify({
        "status": "healthy",
        "workspace": WORKSPACE_ROOT,
        "model": MODEL_NAME,
        "provider": MODEL_PROVIDER
    })


if __name__ == '__main__':
    print(f"Starting Bongo API server...")
    print(f"Workspace: {WORKSPACE_ROOT}")
    print(f"Model: {MODEL_NAME}")
    print(f"Provider: {MODEL_PROVIDER}")
    print(f"Sessions directory: {SESSIONS_DIR}")

    app.run(host='0.0.0.0', port=5000, debug=True)
