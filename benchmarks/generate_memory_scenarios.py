"""生成 12 组 × 30 轮记忆管理基准对话。

每组 JSONL 文件包含 30 轮完整对话记录。
覆盖：单文件重读、文件轮换、摘要失效、多文件增长、无摘要、
任务切换、笔记累积、文件删除、长摘要、混合读写、跨文件引用、真实开发。
"""

import json
import random
from pathlib import Path

random.seed(42)

OUT_DIR = Path(__file__).parent / "memory"
OUT_DIR.mkdir(exist_ok=True)

ROUNDS = 30


def _rand_code(n):
    """生成 n 行随机 Python 代码。"""
    lines = []
    for i in range(n):
        lines.append(random.choice([
            f"def func_{i}():",
            f"    return {random.randint(0, 999)}",
            f"x_{i} = {random.randint(0, 999)}",
            f"class Model{i}:",
            f"    pass",
        ]))
    return "\n".join(lines)


def _write_scenario(name, rounds_data):
    """写入单个场景 JSONL 文件。"""
    path = OUT_DIR / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for rd in rounds_data:
            f.write(json.dumps(rd, ensure_ascii=False) + "\n")
    print(f"  {name}.jsonl — {len(rounds_data)} 轮")


# ── 1. single_file_reread ─────────────────────────────────────────
def gen_single_file_reread():
    """单文件重读：30 轮反复询问同一个文件的不同方面。"""
    fname = "src/core.py"
    content = """import os
import sys
from typing import List, Optional

class Config:
    DEFAULT_TIMEOUT = 30
    MAX_RETRIES = 3

    def __init__(self, path: str):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        with open(self.path) as f:
            return json.load(f)

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def validate(self) -> bool:
        required = ['host', 'port', 'database']
        return all(k in self.data for k in required)

def create_config(path: str) -> Config:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config not found: {path}")
    config = Config(path)
    if not config.validate():
        raise ValueError("Invalid config")
    return config"""

    questions = [
        f"请读取 {fname}",
        f"{fname} 里有哪些类？",
        f"Config 类有哪些方法？",
        f"Config.__init__ 做了什么？",
        f"_load 方法的返回类型是什么？",
        f"validate 方法检查哪些字段？",
        f"DEFAULT_TIMEOUT 的值是多少？",
        f"MAX_RETRIES 的值是多少？",
        f"create_config 函数的作用是什么？",
        f"create_config 会抛什么异常？",
        f"get 方法的 default 参数是什么？",
        f"Config 类继承了什么基类？",
        f"{fname} 用了哪些标准库？",
        f"{fname} 有 type hints 吗？",
        f"如果 config 文件不存在会怎样？",
        f"validate 返回 False 时 create_config 怎么处理？",
        f"Config 的 data 属性是什么类型？",
        f"_load 方法用什么方式读文件？",
        f"get 方法和 dict.get 有什么区别？",
        f"create_config 的参数类型是什么？",
        f"Config 类有几个类变量？",
        f"这个模块的 import 有哪些？",
        f"Config 能处理 YAML 格式吗？",
        f"validate 的返回类型是什么？",
        f"_load 会处理 JSON 解析错误吗？",
        f"Config 支持动态修改配置吗？",
        f"create_config 有日志输出吗？",
        f"这个文件有多少行代码？",
        f"Config 的设计理念是什么？",
        f"总结一下 {fname} 的所有功能",
    ]

    rounds = []
    for i in range(ROUNDS):
        q = questions[i]
        needs_read = i < 2 or "读取" in q or "多少行" in q
        if needs_read:
            rounds.append({
                "round": i + 1,
                "user": q,
                "tool_name": "read_file",
                "tool_args": {"path": fname},
                "tool_result": f"# {fname}\n\n{content}",
                "assistant": f"根据 `{fname}` 的代码：\n\n{content[:200]}...",
                "memory_snapshot": {"recent_files": [fname], "task_summary": f"分析 {fname}"},
            })
        else:
            rounds.append({
                "round": i + 1,
                "user": q,
                "tool_name": None,
                "tool_args": None,
                "tool_result": None,
                "assistant": f"根据之前读取的 `{fname}` 代码，回答你的问题...",
                "memory_snapshot": {"recent_files": [fname], "task_summary": f"分析 {fname}"},
            })
    return rounds


# ── 2. rotate_files ───────────────────────────────────────────────
def gen_rotate_files():
    """文件轮换：按顺序读取多个文件，测试记忆保留。"""
    files = [
        ("README.md", "# 项目说明\n\n这是一个Web应用项目。"),
        ("src/app.py", "from flask import Flask\napp = Flask(__name__)"),
        ("src/models.py", "from sqlalchemy import Column, Integer, String\nclass User:\n    id = Column(Integer)"),
        ("src/views.py", "from flask import jsonify\ndef index():\n    return jsonify({'ok': True})"),
        ("config.yaml", "host: 0.0.0.0\nport: 8080\ndb: sqlite:///app.db"),
        ("requirements.txt", "flask==2.3\nsqlalchemy==2.0\npytest==7.0"),
        ("tests/test_app.py", "def test_index():\n    assert True"),
        ("Dockerfile", "FROM python:3.11\nCOPY . /app\nRUN pip install -r requirements.txt"),
        (".env.example", "DATABASE_URL=sqlite:///app.db\nSECRET_KEY=change-me"),
        ("Makefile", "run:\n\tflask run\ntest:\n\tpytest"),
    ]
    rounds = []
    for i in range(ROUNDS):
        idx = i % len(files)
        fname, content = files[idx]
        q = f"读取并告诉我 {fname} 的内容"
        rounds.append({
            "round": i + 1,
            "user": q,
            "tool_name": "read_file",
            "tool_args": {"path": fname},
            "tool_result": f"# {fname}\n\n{content}",
            "assistant": f"`{fname}` 的内容：\n```\n{content}\n```",
            "memory_snapshot": {
                "recent_files": [f[0] for f in files[:min(i+1, 8)]],
                "task_summary": "了解项目结构",
            },
        })
    return rounds


# ── 3. stale_summary ──────────────────────────────────────────────
def gen_stale_summary():
    """摘要失效：文件内容改变但记忆中的摘要过时。"""
    fname = "src/service.py"
    v1 = "def process(data):\n    return data"
    v2 = "def process(data, timeout=30):\n    validated = validate(data)\n    return transform(validated)"
    v3 = "def process(data, timeout=30, retries=3):\n    for i in range(retries):\n        try:\n            validated = validate(data)\n            return transform(validated)\n        except TimeoutError:\n            continue\n    raise MaxRetriesExceeded"

    rounds = []
    for i in range(ROUNDS):
        cycle = i % 10
        if cycle == 0:
            q = f"读取 {fname}"
            tool_result = f"# {fname}\n\n{v1}"
            assistant = f"`{fname}` 是一个简单的数据处理函数。"
            tool_name, tool_args = "read_file", {"path": fname}
        elif cycle == 1:
            q = f"给 {fname} 的 process 加上 timeout 参数"
            tool_result = f"patched {fname}"
            assistant = "已添加 timeout 参数。"
            tool_name, tool_args = "patch_file", {"path": fname, "old_text": "def process(data):", "new_text": "def process(data, timeout=30):"}
        elif cycle == 2:
            q = f"再看看 {fname} 现在的样子"
            tool_result = f"# {fname}\n\n{v2}"
            assistant = f"`{fname}` 已更新，现在包含 timeout 参数和验证逻辑。"
            tool_name, tool_args = "read_file", {"path": fname}
        elif cycle == 3:
            q = "process 函数有几个参数？"
            tool_result = None
            assistant = "根据之前的代码，process 有 data 和 timeout 两个参数。"
            tool_name, tool_args = None, None
        elif cycle == 4:
            q = f"给 {fname} 加上重试机制"
            tool_result = f"patched {fname}"
            assistant = "已添加重试机制，最多重试 3 次。"
            tool_name, tool_args = "patch_file", {"path": fname, "old_text": "def process(data, timeout=30):", "new_text": "def process(data, timeout=30, retries=3):"}
        elif cycle == 5:
            q = f"再看看 {fname} 的最新代码"
            tool_result = f"# {fname}\n\n{v3}"
            assistant = f"`{fname}` 现在有完整的重试逻辑。"
            tool_name, tool_args = "read_file", {"path": fname}
        elif cycle == 6:
            q = "process 现在有几个参数？"
            tool_result = None
            assistant = "process 现在有 3 个参数：data、timeout、retries。"
            tool_name, tool_args = None, None
        elif cycle == 7:
            q = "重试机制的默认次数是多少？"
            tool_result = None
            assistant = "默认重试 3 次。"
            tool_name, tool_args = None, None
        elif cycle == 8:
            q = "process 会抛什么异常？"
            tool_result = None
            assistant = "当重试次数用完后会抛出 MaxRetriesExceeded 异常。"
            tool_name, tool_args = None, None
        else:
            q = "总结 process 函数的完整功能"
            tool_result = None
            assistant = "process 函数：接收数据 → 验证 → 转换 → 返回，支持超时和重试。"
            tool_name, tool_args = None, None
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": tool_name, "tool_args": tool_args,
            "tool_result": tool_result, "assistant": assistant,
            "memory_snapshot": {"recent_files": [fname], "task_summary": f"修改 {fname} 的 process 函数"},
        })
    return rounds


# ── 4. multi_file ─────────────────────────────────────────────────
def gen_multi_file():
    """多文件增长：读取越来越多文件，测试记忆截断。"""
    files = [
        f"src/module_{chr(97+i)}.py" for i in range(20)
    ]
    rounds = []
    for i in range(ROUNDS):
        idx = i % len(files)
        fname = files[idx]
        q = f"读取 {fname}"
        content = f"# {fname}\ndef func_{idx}():\n    return {idx}"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": "read_file", "tool_args": {"path": fname},
            "tool_result": f"# {fname}\n\n{content}",
            "assistant": f"已读取 `{fname}`，包含 func_{idx} 函数。",
            "memory_snapshot": {
                "recent_files": files[max(0, i-7):i+1][-8:],
                "task_summary": f"分析 {i+1} 个模块文件",
            },
        })
    return rounds


# ── 5. no_summary ─────────────────────────────────────────────────
def gen_no_summary():
    """无任务摘要：用户从不说明目的，测试记忆推断。"""
    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    rounds = []
    for i in range(ROUNDS):
        fname = files[i % len(files)]
        q = f"{fname}"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": "read_file", "tool_args": {"path": fname},
            "tool_result": f"# {fname}\ndef func():\n    return {i}",
            "assistant": f"已读取 `{fname}`。",
            "memory_snapshot": {"recent_files": [fname], "task_summary": ""},
        })
    return rounds


# ── 6. task_change ────────────────────────────────────────────────
def gen_task_change():
    """任务切换：中途切换任务，测试记忆适应。"""
    task1_files = ["src/auth/login.py", "src/auth/register.py", "src/auth/session.py"]
    task2_files = ["src/pay/stripe.py", "src/pay/invoice.py", "src/pay/refund.py"]
    rounds = []
    for i in range(ROUNDS):
        if i < 15:
            fname = task1_files[i % len(task1_files)]
            task = "实现用户认证模块"
            q = f"帮我看看 {fname} 的认证逻辑"
        else:
            fname = task2_files[(i - 15) % len(task2_files)]
            task = "实现支付模块"
            q = f"帮我看看 {fname} 的支付逻辑"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": "read_file", "tool_args": {"path": fname},
            "tool_result": f"# {fname}\ndef handle():\n    pass",
            "assistant": f"已读取 `{fname}`。",
            "memory_snapshot": {"recent_files": [fname], "task_summary": task},
        })
    return rounds


# ── 7. note_heavy ─────────────────────────────────────────────────
def gen_note_heavy():
    """笔记累积：每轮都添加笔记，测试笔记截断。"""
    topics = [
        "Python 类型注解", "Flask 路由", "SQLAlchemy ORM", "Pytest fixture",
        "Docker 多阶段构建", "Git rebase", "REST API 设计", "JWT 认证",
        "Redis 缓存", "Celery 任务队列", "Nginx 反向代理", "PostgreSQL 索引",
    ]
    rounds = []
    for i in range(ROUNDS):
        topic = topics[i % len(topics)]
        q = f"帮我记录一下关于 {topic} 的学习笔记"
        note_content = f"## {topic}\n\n- 关键概念{i+1}\n- 最佳实践{i+1}\n- 常见陷阱{i+1}"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": "add_note", "tool_args": {"content": note_content, "title": topic},
            "tool_result": f"笔记已保存：{topic}",
            "assistant": f"已记录 `{topic}` 的学习笔记，包含关键概念、最佳实践和常见陷阱。",
            "memory_snapshot": {
                "recent_files": [],
                "task_summary": f"记录学习笔记（{i+1} 篇）",
                "notes_count": i + 1,
            },
        })
    return rounds


# ── 8. file_deleted ───────────────────────────────────────────────
def gen_file_deleted():
    """文件删除：先读后删，测试记忆一致性。"""
    files = [f"src/temp_{i}.py" for i in range(10)]
    rounds = []
    for i in range(ROUNDS):
        cycle = i % 3
        fidx = i // 3 % len(files)
        fname = files[fidx]
        if cycle == 0:
            q = f"读取 {fname}"
            tool_result = f"# {fname}\ndef temp():\n    return {fidx}"
            tool_name, tool_args = "read_file", {"path": fname}
            assistant = f"已读取 `{fname}`。"
        elif cycle == 1:
            q = f"删除 {fname}"
            tool_result = f"deleted {fname}"
            tool_name, tool_args = "run_shell", {"command": f"rm {fname}", "timeout": 10}
            assistant = f"已删除 `{fname}`。"
        else:
            q = f"刚才删了什么文件？"
            tool_result = None
            tool_name, tool_args = None, None
            assistant = f"刚才删除了 `{fname}`。"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": tool_name, "tool_args": tool_args,
            "tool_result": tool_result, "assistant": assistant,
            "memory_snapshot": {
                "recent_files": [files[j] for j in range(max(0, fidx-2), fidx+1)],
                "task_summary": f"清理临时文件",
            },
        })
    return rounds


# ── 9. large_codebase ─────────────────────────────────────────────
def gen_large_codebase():
    """大型代码库：20+ 文件的项目。"""
    all_files = []
    for pkg in ["auth", "api", "models", "utils", "tests"]:
        for mod in ["core", "helpers", "config", "exceptions"]:
            all_files.append(f"src/{pkg}/{mod}.py")
    rounds = []
    for i in range(ROUNDS):
        fname = all_files[i % len(all_files)]
        q = f"查看 {fname}"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": "read_file", "tool_args": {"path": fname},
            "tool_result": f"# {fname}\n\nclass Handler:\n    def run(self):\n        pass",
            "assistant": f"已读取 `{fname}`，包含 Handler 类。",
            "memory_snapshot": {
                "recent_files": all_files[max(0, i-7):i+1][-8:],
                "task_summary": f"了解项目代码（已查看 {i+1} 个文件）",
            },
        })
    return rounds


# ── 10. incremental_build ─────────────────────────────────────────
def gen_incremental_build():
    """增量构建：逐步构建一个完整功能。"""
    feature = "user_profile"
    steps = [
        ("创建模型", "write_file", f"src/models/{feature}.py",
         f"from django.db import models\n\nclass UserProfile(models.Model):\n    user = models.OneToOneField('User')\n    bio = models.TextField(blank=True)\n    avatar = models.URLField(blank=True)",
         f"wrote src/models/{feature}.py"),
        ("创建序列化器", "write_file", f"src/serializers/{feature}.py",
         f"from rest_framework import serializers\nfrom .models import UserProfile\n\nclass UserProfileSerializer(serializers.ModelSerializer):\n    class Meta:\n        model = UserProfile\n        fields = '__all__'",
         f"wrote src/serializers/{feature}.py"),
        ("创建视图", "write_file", f"src/views/{feature}.py",
         f"from rest_framework import viewsets\nfrom .serializers import UserProfileSerializer\n\nclass UserProfileViewSet(viewsets.ModelViewSet):\n    serializer_class = UserProfileSerializer",
         f"wrote src/views/{feature}.py"),
        ("创建路由", "write_file", f"src/urls/{feature}.py",
         f"from django.urls import path, include\nfrom .views import UserProfileViewSet\n\nurlpatterns = [\n    path('profile/', UserProfileViewSet.as_view()),\n]",
         f"wrote src/urls/{feature}.py"),
        ("创建测试", "write_file", f"tests/test_{feature}.py",
         f"import pytest\n\ndef test_create_profile():\n    assert True\n\ndef test_update_profile():\n    assert True",
         f"wrote tests/test_{feature}.py"),
        ("运行测试", "run_shell", "pytest tests/test_user_profile.py",
         None, "exit_code: 0\n2 passed"),
        ("查看模型", "read_file", f"src/models/{feature}.py",
         None, f"from django.db import models\n\nclass UserProfile(models.Model):\n    user = models.OneToOneField('User')\n    bio = models.TextField(blank=True)\n    avatar = models.URLField(blank=True)"),
        ("搜索引用", "search", "UserProfile",
         None, f"src/models/{feature}.py:3: class UserProfile\nsrc/serializers/{feature}.py:2: from .models import UserProfile\nsrc/views/{feature}.py:2: from .serializers import UserProfileSerializer"),
        ("添加字段", "patch_file", f"src/models/{feature}.py",
         "    avatar = models.URLField(blank=True)", "    avatar = models.URLField(blank=True)\n    location = models.CharField(max_length=100, blank=True)\n    website = models.URLField(blank=True)",
         f"patched src/models/{feature}.py"),
        ("重新测试", "run_shell", "pytest tests/test_user_profile.py",
         None, "exit_code: 0\n2 passed"),
    ]
    rounds = []
    for i in range(ROUNDS):
        step = steps[i % len(steps)]
        desc, tool, target = step[0], step[1], step[2]
        content_or_args = step[3]
        result = step[4]

        if tool == "write_file":
            user_msg = f"{desc}：{target}"
            tool_args = {"path": target, "content": content_or_args}
            tool_result = result
        elif tool == "read_file":
            user_msg = f"查看 {target}"
            tool_args = {"path": target}
            tool_result = result
        elif tool == "patch_file":
            user_msg = f"给 {target} 添加新字段"
            tool_args = {"path": target, "old_text": content_or_args, "new_text": result}
            tool_result = f"patched {target}"
        elif tool == "search":
            user_msg = f"搜索 {target} 的所有引用"
            tool_args = {"pattern": target, "path": "."}
            tool_result = result
        else:
            user_msg = f"{desc}"
            tool_args = {"command": target, "timeout": 30}
            tool_result = result

        rounds.append({
            "round": i + 1, "user": user_msg,
            "tool_name": tool, "tool_args": tool_args,
            "tool_result": tool_result,
            "assistant": f"已完成：{desc}。",
            "memory_snapshot": {
                "recent_files": [f"src/models/{feature}.py", f"src/serializers/{feature}.py",
                                 f"src/views/{feature}.py", f"src/urls/{feature}.py",
                                 f"tests/test_{feature}.py"],
                "task_summary": f"构建 {feature} 功能模块（{desc}）",
            },
        })
    return rounds


# ── 11. cross_file_ref ────────────────────────────────────────────
def gen_cross_file_ref():
    """跨文件引用：测试多文件间的依赖关系记忆。"""
    modules = [
        ("src/database.py", "class Database:\n    def connect(self): pass\n    def query(self, sql): pass"),
        ("src/cache.py", "class Cache:\n    def get(self, key): pass\n    def set(self, key, val): pass"),
        ("src/auth.py", "from .database import Database\nclass Auth:\n    def login(self, user): pass"),
        ("src/api.py", "from .auth import Auth\nfrom .cache import Cache\nclass API:\n    def handle(self, req): pass"),
        ("src/app.py", "from .api import API\nfrom .database import Database\ndef main():\n    db = Database()\n    api = API()"),
    ]
    rounds = []
    for i in range(ROUNDS):
        idx = i % len(modules)
        fname, content = modules[idx]
        q = f"读取 {fname} 并告诉我它的依赖"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": "read_file", "tool_args": {"path": fname},
            "tool_result": f"# {fname}\n\n{content}",
            "assistant": f"已读取 `{fname}`，分析其依赖关系...",
            "memory_snapshot": {
                "recent_files": [m[0] for m in modules[:idx+1]],
                "task_summary": "分析项目依赖关系",
            },
        })
    return rounds


# ── 12. realistic_dev ─────────────────────────────────────────────
def gen_realistic_dev():
    """真实开发：模拟完整开发会话。"""
    actions = [
        ("read_file", "src/app.py", {"path": "src/app.py"}, "# app.py\nfrom flask import Flask\napp = Flask(__name__)"),
        ("search", "TODO", {"pattern": "TODO", "path": "."}, "src/app.py:5: TODO: add routes\nsrc/models.py:10: TODO: add migrations"),
        ("read_file", "src/models.py", {"path": "src/models.py"}, "# models.py\nclass User:\n    id = 0\n    name = ''"),
        ("patch_file", "src/models.py", {"path": "src/models.py", "old_text": "id = 0", "new_text": "id = Column(Integer, primary_key=True)"}, "patched src/models.py"),
        ("write_file", "src/routes.py", {"path": "src/routes.py", "content": "from flask import Blueprint\nbp = Blueprint('main', __name__)\n\n@bp.route('/')\ndef index():\n    return 'ok'"}, "wrote src/routes.py"),
        ("run_shell", "pytest", {"command": "pytest", "timeout": 30}, "exit_code: 0\n5 passed"),
        ("read_file", "src/routes.py", {"path": "src/routes.py"}, "from flask import Blueprint\nbp = Blueprint('main', __name__)\n\n@bp.route('/')\ndef index():\n    return 'ok'"),
        ("search", "import", {"pattern": "import", "path": "src/"}, "src/app.py:1: from flask import Flask\nsrc/routes.py:1: from flask import Blueprint"),
        ("patch_file", "src/app.py", {"path": "src/app.py", "old_text": "app = Flask(__name__)", "new_text": "app = Flask(__name__)\nfrom .routes import bp\napp.register_blueprint(bp)"}, "patched src/app.py"),
        ("run_shell", "test again", {"command": "pytest", "timeout": 30}, "exit_code: 0\n8 passed"),
    ]
    rounds = []
    for i in range(ROUNDS):
        action = actions[i % len(actions)]
        tool_name, desc, tool_args, tool_result = action
        q = f"第 {i+1} 步：{desc}"
        rounds.append({
            "round": i + 1, "user": q,
            "tool_name": tool_name, "tool_args": tool_args,
            "tool_result": tool_result,
            "assistant": f"已完成第 {i+1} 步：{desc}。",
            "memory_snapshot": {
                "recent_files": ["src/app.py", "src/models.py", "src/routes.py"],
                "task_summary": "开发 Flask 应用",
            },
        })
    return rounds


# ── 主函数 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("生成 12 组 × 30 轮记忆管理基准对话")
    print("=" * 60)

    generators = [
        ("single_file_reread", gen_single_file_reread),
        ("rotate_files", gen_rotate_files),
        ("stale_summary", gen_stale_summary),
        ("multi_file", gen_multi_file),
        ("no_summary", gen_no_summary),
        ("task_change", gen_task_change),
        ("note_heavy", gen_note_heavy),
        ("file_deleted", gen_file_deleted),
        ("large_codebase", gen_large_codebase),
        ("incremental_build", gen_incremental_build),
        ("cross_file_ref", gen_cross_file_ref),
        ("realistic_dev", gen_realistic_dev),
    ]

    for name, gen_fn in generators:
        rounds = gen_fn()
        _write_scenario(name, rounds)

    print("\n" + "=" * 60)
    print(f"生成完成！共 {len(generators)} 个场景 × {ROUNDS} 轮 = {len(generators) * ROUNDS} 轮对话")
    print(f"输出目录: {OUT_DIR}")
    print("=" * 60)
