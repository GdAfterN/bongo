"""用户画像模块。

每个用户有独立的 profile 文件，记录技能水平、易错点、学习进度。
bongo 作为学习伙伴，通过 profile "记住"用户。

数据结构：
- profile.json: 用户基本信息、技能、错误记录
- notes/{username}.md: 学习笔记（一个用户一个 md 文件）
- trusted_paths: 信任的文件路径列表（只记录路径）
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PROFILES_DIR = Path.home() / ".bongo" / "profiles"
DEFAULT_NOTES_DIR = Path.home() / ".bongo" / "notes"
DEFAULT_MISTAKES_DIR = Path.home() / ".bongo" / "mistakes"
CURRENT_USER_FILE = Path.home() / ".bongo" / "current_user"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def default_profile(username="default"):
    return {
        "username": username,
        "created_at": _now_iso(),
        "level": "beginner",
        "skills": {},
        "mistakes": [],
        "learnings": [],
        "notes": [],
        "trusted_paths": [],
        "daily_log": {},
        "streak": 0,
        "last_active_date": None,
        "last_summary_date": None,
        # 用户定位（创建时设定）
        "user_positioning": "",     # 如 "一个 Java 领域的求职者"、"前端初学者"
        # 画像三要素
        "frequent_topics": [],      # 常聊话题（从 /ask 中提取）
        "weak_areas": [],           # 薄弱领域（从 /practice 错题中提取）
        "user_preference": {        # 用户偏好（从使用行为统计）
            "ask_count": 0,         # /ask 使用次数
            "practice_count": 0,    # /practice 使用次数
            "topics_explored": {},  # 话题 -> 出现次数
            "preference_type": "",  # 推断的偏好类型
        },
        # session 记录
        "sessions": [],             # 最近 session 摘要列表
    }


class UserProfile:
    def __init__(self, username="default", profiles_dir=None, notes_dir=None, mistakes_dir=None):
        self.profiles_dir = Path(profiles_dir or DEFAULT_PROFILES_DIR)
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir = Path(notes_dir or DEFAULT_NOTES_DIR)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.mistakes_dir = Path(mistakes_dir or DEFAULT_MISTAKES_DIR)
        self.mistakes_dir.mkdir(parents=True, exist_ok=True)
        self.username = username
        self.path = self.profiles_dir / f"{username}.json"
        self.notes_file = self.notes_dir / f"{username}.md"
        self.notes_index_file = self.notes_dir / f"{username}_index.md"
        self.mistakes_file = self.mistakes_dir / f"{username}.md"
        self.index_file = self.mistakes_dir / f"{username}_index.md"
        self.data = self._load()
        self._ensure_indexes()

    def _load(self):
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        profile = default_profile(self.username)
        self._save(profile)
        return profile

    def _save(self, data=None):
        self.path.write_text(
            json.dumps(data or self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def save(self):
        self._save()

    def _ensure_indexes(self):
        """确保笔记和错题的索引文件存在且包含 offset 信息。不存在则重建。"""
        # 笔记索引
        if self.notes_file.exists():
            if not self.notes_index_file.exists():
                self._rebuild_notes_index()
            else:
                # 检查索引是否有 offset
                entries = self._read_notes_index()
                if entries and any(e.get("offset") is None for e in entries):
                    self._rebuild_notes_index()

        # 错题索引
        if self.mistakes_file.exists():
            if not self.index_file.exists():
                self._rebuild_mistakes_index()
            else:
                entries = self.get_mistakes_index()
                if entries and any(e.get("offset") is None for e in entries):
                    self._rebuild_mistakes_index()

    def record_task(self, task, skills=None, mistakes=None, learnings=None, files=None, difficulty=3):
        skills = skills or []
        mistakes = mistakes or []
        learnings = learnings or []
        files = files or []

        for mistake in mistakes:
            entry = {
                "type": mistake.get("type", "general"),
                "desc": mistake.get("desc", ""),
                "fix": mistake.get("fix", ""),
                "task": task,
                "date": _now_iso(),
            }
            self.data["mistakes"].append(entry)

        for learning in learnings:
            entry = {
                "text": learning,
                "task": task,
                "date": _now_iso(),
            }
            self.data["learnings"].append(entry)

        today = _today_str()
        if today not in self.data["daily_log"]:
            self.data["daily_log"][today] = {"tasks": 0, "mistakes_count": 0}
        daily = self.data["daily_log"][today]
        daily["tasks"] += 1
        daily["mistakes_count"] += len(mistakes)

        last = self.data.get("last_active_date")
        if last == today:
            pass
        elif last and (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days == 1:
            self.data["streak"] = self.data.get("streak", 0) + 1
        else:
            self.data["streak"] = 1
        self.data["last_active_date"] = today

        self.save()

    def get_mistakes(self, limit=20, group_by_type=True):
        mistakes = self.data.get("mistakes", [])
        if not group_by_type:
            return mistakes[-limit:]

        groups = {}
        for m in mistakes:
            t = m.get("type", "general")
            if t not in groups:
                groups[t] = []
            groups[t].append(m)

        result = []
        for mtype, items in sorted(groups.items(), key=lambda x: -len(x[1])):
            result.append({
                "type": mtype,
                "count": len(items),
                "latest": items[-1],
                "recent": items[-3:],
            })
        return result

    def get_skills(self):
        """返回用户知识画像摘要（供其他 agent 理解用户）。"""
        return {
            "frequent_topics": self.data.get("frequent_topics", []),
            "weak_areas": self.data.get("weak_areas", []),
            "preference_type": self.data.get("user_preference", {}).get("preference_type", ""),
        }

    def add_note(self, content, file_path=None, title=None):
        """添加学习笔记到 md 文件，同时更新索引。"""
        timestamp = _now_iso()
        note_title = title or (content[:50] + "..." if len(content) > 50 else content)

        # 转义内容中的 ## 前缀，防止被解析为新笔记标题
        safe_lines = []
        for line in content.split("\n"):
            if line.startswith("## "):
                safe_lines.append(line[3:])
            else:
                safe_lines.append(line)
        safe_content = "\n".join(safe_lines)

        # 构建笔记条目
        note_entry = f"\n## {note_title}\n"
        note_entry += f"- 时间: {timestamp}\n"
        if file_path:
            note_entry += f"- 关联文件: {file_path}\n"
            if file_path not in self.data.get("trusted_paths", []):
                self.data.setdefault("trusted_paths", []).append(file_path)
                self.save()
        note_entry += f"\n{safe_content}\n"

        # 写入 md 文件，记录字节偏移
        if not self.notes_file.exists():
            self.notes_file.write_text(f"# {self.username} 的学习笔记\n", encoding="utf-8")

        with open(self.notes_file, "a+b") as f:
            f.seek(0, 2)  # seek to end
            offset = f.tell()
            entry_bytes = note_entry.encode("utf-8")
            f.write(entry_bytes)
            length = len(entry_bytes)

        # 追加索引行
        index_line = f"- {note_title} | {timestamp} | offset:{offset}, len:{length}\n"
        if not self.notes_index_file.exists():
            self.notes_index_file.write_text(f"# 笔记索引\n\n", encoding="utf-8")
        with open(self.notes_index_file, "a", encoding="utf-8") as f:
            f.write(index_line)
            f.flush()

        # 验证索引写入成功，失败则重建
        try:
            idx_content = self.notes_index_file.read_text(encoding="utf-8")
            if note_title not in idx_content:
                self._rebuild_notes_index()
        except Exception:
            self._rebuild_notes_index()

        return {
            "title": note_title,
            "content": content,
            "file_path": file_path or "",
            "timestamp": timestamp,
        }

    def delete_note(self, title):
        """根据标题从笔记文件中删除一条笔记，并重建索引。"""
        import re as _re
        if not self.notes_file.exists():
            return False
        content = self.notes_file.read_text(encoding="utf-8")
        sections = _re.split(r'\n(?=## )', content)
        new_sections = [s for s in sections if title not in s.split("\n")[0]]
        if len(new_sections) == len(sections):
            return False
        self.notes_file.write_text("\n".join(new_sections), encoding="utf-8")
        self._rebuild_notes_index()
        return True

    def _rebuild_notes_index(self):
        """从笔记详情文件重建索引。"""
        if not self.notes_file.exists():
            if self.notes_index_file.exists():
                self.notes_index_file.unlink()
            return

        import re as _re
        with open(self.notes_file, "rb") as f:
            raw_bytes = f.read()

        content = raw_bytes.decode("utf-8")
        # 找到每个 ## 标题的字节偏移
        header_offsets = []
        for m in _re.finditer(r'^## ', content, _re.MULTILINE):
            char_pos = m.start()
            byte_offset = len(content[:char_pos].encode("utf-8"))
            header_offsets.append(byte_offset)

        if not header_offsets:
            self.notes_index_file.write_text("# 笔记索引\n\n", encoding="utf-8")
            return

        index_lines = ["# 笔记索引\n\n"]
        for i, offset in enumerate(header_offsets):
            end = header_offsets[i + 1] if i + 1 < len(header_offsets) else len(raw_bytes)
            length = end - offset
            entry_text = raw_bytes[offset:end].decode("utf-8")
            title = ""
            timestamp = ""
            for line in entry_text.split("\n"):
                if line.startswith("## "):
                    title = line[3:].strip()
                elif line.startswith("- 时间:"):
                    timestamp = line[5:].strip()
            index_lines.append(f"- {title} | {timestamp} | offset:{offset}, len:{length}\n")

        self.notes_index_file.write_text("".join(index_lines), encoding="utf-8")

    def get_notes(self, limit=5):
        """从索引读取最近的笔记列表。有 offset 时用 seek 读取，否则降级全文解析。"""
        if not self.notes_file.exists():
            return []

        # 尝试从索引读取
        index_entries = self._read_notes_index()
        if index_entries:
            entries = index_entries[-limit:]
            notes = []
            for entry in entries:
                if entry.get("offset") is not None and entry.get("length") is not None:
                    raw = self._read_entry_at(self.notes_file, entry["offset"], entry["length"])
                    note = self._parse_note_entry(raw)
                    notes.append(note)
                else:
                    # 降级：全文解析
                    return self._get_notes_full(limit)
            return notes

        # 无索引，降级全文解析
        return self._get_notes_full(limit)

    def _read_notes_index(self):
        """读取笔记索引文件。"""
        if not self.notes_index_file.exists():
            return []
        entries = []
        for line in self.notes_index_file.read_text(encoding="utf-8").split("\n"):
            if not line.startswith("- "):
                continue
            try:
                body = line[2:]
                parts = body.split(" | ")
                if len(parts) < 2:
                    continue
                title = parts[0].strip()
                timestamp = parts[1].strip()
                offset = None
                length = None
                for p in parts[2:]:
                    p = p.strip()
                    if p.startswith("offset:"):
                        rest = p[7:]
                        if ", len:" in rest:
                            o, l = rest.split(", len:", 1)
                            offset = int(o)
                            length = int(l)
                entries.append({"title": title, "timestamp": timestamp, "offset": offset, "length": length})
            except (ValueError, IndexError):
                continue
        return entries

    def _parse_note_entry(self, raw):
        """从原始 entry 文本解析笔记字段。"""
        note = {"title": "", "content": "", "file_path": "", "timestamp": ""}
        lines = raw.strip().split("\n")
        for line in lines:
            if line.startswith("## "):
                note["title"] = line[3:].strip()
            elif line.startswith("- 时间:"):
                note["timestamp"] = line[5:].strip()
            elif line.startswith("- 关联文件:"):
                note["file_path"] = line[6:].strip()
            elif line.strip() and not line.startswith("- 时间:") and not line.startswith("- 关联文件:"):
                if note["content"]:
                    note["content"] += "\n" + line
                else:
                    note["content"] = line
        return note

    def _get_notes_full(self, limit):
        """降级：全文解析笔记文件。"""
        content = self.notes_file.read_text(encoding="utf-8")
        notes = []
        current_note = None
        for line in content.split("\n"):
            if line.startswith("## "):
                if current_note:
                    notes.append(current_note)
                current_note = {"title": line[3:].strip(), "content": "", "file_path": "", "timestamp": ""}
            elif current_note and line.startswith("- 时间:"):
                current_note["timestamp"] = line[5:].strip()
            elif current_note and line.startswith("- 关联文件:"):
                current_note["file_path"] = line[6:].strip()
            elif current_note and line.strip() and not line.startswith("- 时间:") and not line.startswith("- 关联文件:"):
                if current_note["content"]:
                    current_note["content"] += "\n" + line
                else:
                    current_note["content"] = line
        if current_note:
            notes.append(current_note)
        return notes[-limit:]

    def add_mistake(self, question, user_answer, score, feedback, correct_answer="", source=""):
        """添加错题到错题本 md 文件，并自动追加索引（含字节偏移）。"""
        timestamp = _now_iso()
        title = question[:50] + "..." if len(question) > 50 else question

        # 构建条目
        entry = f"\n## {title}\n"
        entry += f"- 时间: {timestamp}\n"
        if source:
            entry += f"- 来源: {source}\n"
        entry += f"- 得分: {score}\n"
        entry += f"- 错误次数: 1\n"
        entry += f"\n**题目：** {question}\n"
        entry += f"\n**你的回答：** {user_answer}\n"
        if correct_answer:
            entry += f"\n**正确答案：** {correct_answer}\n"
        entry += f"\n**反馈：** {feedback}\n"

        # 写入详情文件，记录字节偏移
        if not self.mistakes_file.exists():
            self.mistakes_file.write_text(f"# {self.username} 的错题本\n", encoding="utf-8")

        with open(self.mistakes_file, "a+b") as f:
            f.seek(0, 2)
            offset = f.tell()
            entry_bytes = entry.encode("utf-8")
            f.write(entry_bytes)
            length = len(entry_bytes)

        # 追加索引行（含 offset）
        tags = self._extract_tags(question, feedback)
        tag_str = ",".join(tags) if tags else ""
        summary = question[:30] + "..." if len(question) > 30 else question
        index_line = (
            f"- [{timestamp[:10]}] 得分:{score} | 来源:{source} | 次数:1 | "
            f"标签:{tag_str} → {summary} | offset:{offset}, len:{length}\n"
        )

        if not self.index_file.exists():
            self.index_file.write_text(f"# 错题索引\n\n", encoding="utf-8")

        with open(self.index_file, "a", encoding="utf-8") as f:
            f.write(index_line)

        return {"title": title, "score": score, "timestamp": timestamp, "tags": tags}

    def delete_mistake(self, summary):
        """根据摘要从详情文件和索引文件中删除一条错题，并重建索引。"""
        import re as _re
        # 从详情文件删除匹配的 ## 段落
        if self.mistakes_file.exists():
            content = self.mistakes_file.read_text(encoding="utf-8")
            sections = _re.split(r'\n(?=## )', content)
            kept = [s for s in sections if summary not in s.split("\n")[0]]
            self.mistakes_file.write_text("\n".join(kept), encoding="utf-8")

        # 重建索引
        self._rebuild_mistakes_index()

    def _rebuild_mistakes_index(self):
        """从错题详情文件重建索引。"""
        if not self.mistakes_file.exists():
            if self.index_file.exists():
                self.index_file.unlink()
            return

        import re as _re
        with open(self.mistakes_file, "rb") as f:
            raw_bytes = f.read()

        content = raw_bytes.decode("utf-8")
        header_offsets = []
        for m in _re.finditer(r'^## ', content, _re.MULTILINE):
            char_pos = m.start()
            byte_offset = len(content[:char_pos].encode("utf-8"))
            header_offsets.append(byte_offset)

        if not header_offsets:
            self.index_file.write_text("# 错题索引\n\n", encoding="utf-8")
            return

        index_lines = ["# 错题索引\n\n"]
        for i, offset in enumerate(header_offsets):
            end = header_offsets[i + 1] if i + 1 < len(header_offsets) else len(raw_bytes)
            length = end - offset
            entry_text = raw_bytes[offset:end].decode("utf-8")
            title = ""
            timestamp = ""
            score = 0
            source = ""
            count = 1
            question = ""
            for line in entry_text.split("\n"):
                if line.startswith("## "):
                    title = line[3:].strip()
                elif line.startswith("- 时间:"):
                    timestamp = line[5:].strip()
                elif line.startswith("- 来源:"):
                    source = line[5:].strip()
                elif line.startswith("- 得分:"):
                    try:
                        score = int(line[5:].strip())
                    except ValueError:
                        pass
                elif line.startswith("- 错误次数:"):
                    try:
                        count = int(line[6:].strip())
                    except ValueError:
                        pass
                elif line.startswith("**题目：**"):
                    question = line[7:].strip()

            tags = self._extract_tags(question, "")
            tag_str = ",".join(tags) if tags else ""
            summary = question[:30] + "..." if len(question) > 30 else question
            index_lines.append(
                f"- [{timestamp[:10]}] 得分:{score} | 来源:{source} | 次数:{count} | "
                f"标签:{tag_str} → {summary} | offset:{offset}, len:{length}\n"
            )

        self.index_file.write_text("".join(index_lines), encoding="utf-8")

    def update_mistake_count(self, summary, new_count):
        """更新索引文件中匹配题目的错误次数。"""
        if not self.index_file.exists():
            return
        content = self.index_file.read_text(encoding="utf-8")
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if summary in line and line.startswith("- ["):
                import re as _re
                lines[i] = _re.sub(r'次数:\d+', f'次数:{new_count}', line)
                break
        self.index_file.write_text("\n".join(lines), encoding="utf-8")

    @staticmethod
    def _read_entry_at(file_path, offset, length):
        """用 seek 读取文件中指定偏移处的 entry。"""
        with open(file_path, "rb") as f:
            f.seek(offset)
            return f.read(length).decode("utf-8")

    def _extract_tags(self, question, feedback):
        """从题目和反馈中提取关键词作为标签。"""
        import re
        text = f"{question} {feedback}"
        # 提取中文词和英文词
        cn_words = re.findall(r'[一-鿿]{2,6}', text)
        en_words = re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,}', text)
        # 去重，保留顺序
        seen = set()
        tags = []
        for w in cn_words + en_words:
            w_lower = w.lower()
            if w_lower not in seen:
                seen.add(w_lower)
                tags.append(w)
            if len(tags) >= 5:
                break
        return tags

    def get_mistakes_index(self):
        """读取错题索引文件，返回列表（含 offset/length）。"""
        if not self.index_file.exists():
            return []

        content = self.index_file.read_text(encoding="utf-8")
        index = []
        for line in content.split("\n"):
            if not line.startswith("- ["):
                continue
            try:
                bracket_end = line.index("]")
                timestamp = line[3:bracket_end]
                rest = line[bracket_end + 2:]

                parts = rest.split(" | ")
                score = 0
                source = ""
                tags = []
                summary = ""
                count = 1
                offset = None
                length = None

                for part in parts:
                    part = part.strip()
                    if part.startswith("得分:"):
                        score = int(part[3:])
                    elif part.startswith("来源:"):
                        source = part[3:]
                    elif part.startswith("次数:"):
                        count = int(part[3:])
                    elif part.startswith("标签:"):
                        tag_part = part[3:]
                        if " → " in tag_part:
                            tag_str, summary = tag_part.split(" → ", 1)
                            tags = [t.strip() for t in tag_str.split(",") if t.strip()]
                        else:
                            tags = [t.strip() for t in tag_part.split(",") if t.strip()]
                    elif part.startswith("offset:"):
                        rest2 = part[7:]
                        if ", len:" in rest2:
                            o, l = rest2.split(", len:", 1)
                            offset = int(o)
                            length = int(l)
                    elif " → " in part:
                        summary = part.split(" → ", 1)[1]

                entry = {
                    "timestamp": timestamp,
                    "score": score,
                    "source": source,
                    "tags": tags,
                    "count": count,
                    "summary": summary,
                }
                if offset is not None:
                    entry["offset"] = offset
                    entry["length"] = length
                index.append(entry)
            except (ValueError, IndexError):
                continue

        return index

    def get_mistakes_from_file(self, limit=20, days=None):
        """从错题本读取错题。优先用索引的 offset 做 O(1) 读取。"""
        if not self.mistakes_file.exists():
            return []

        index_entries = self.get_mistakes_index()
        if index_entries and all(e.get("offset") is not None for e in index_entries):
            # 用索引读取
            mistakes = []
            for entry in index_entries:
                raw = self._read_entry_at(self.mistakes_file, entry["offset"], entry["length"])
                mistake = self._parse_mistake_entry(raw)
                if mistake:
                    mistakes.append(mistake)
        else:
            # 降级：全文解析
            mistakes = self._get_mistakes_full()

        if days is not None:
            cutoff = (datetime.now() - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")
            mistakes = [m for m in mistakes if m.get("timestamp", "")[:10] >= cutoff]

        mistakes.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        return mistakes[:limit]

    def _get_mistakes_full(self):
        """降级：全文解析错题文件。"""
        content = self.mistakes_file.read_text(encoding="utf-8")
        mistakes = []
        current = None
        for line in content.split("\n"):
            if line.startswith("## "):
                if current:
                    mistakes.append(current)
                current = {
                    "title": line[3:].strip(), "timestamp": "", "source": "",
                    "score": 0, "count": 1, "question": "", "user_answer": "",
                    "correct_answer": "", "feedback": "",
                }
            elif current and line.startswith("- 时间:"):
                current["timestamp"] = line[5:].strip()
            elif current and line.startswith("- 来源:"):
                current["source"] = line[5:].strip()
            elif current and line.startswith("- 得分:"):
                try:
                    current["score"] = int(line[5:].strip())
                except ValueError:
                    pass
            elif current and line.startswith("- 错误次数:"):
                try:
                    current["count"] = int(line[6:].strip())
                except ValueError:
                    pass
            elif current and line.startswith("**题目：**"):
                current["question"] = line[7:].strip()
            elif current and line.startswith("**你的回答：**"):
                current["user_answer"] = line[8:].strip()
            elif current and line.startswith("**正确答案：**"):
                current["correct_answer"] = line[8:].strip()
            elif current and line.startswith("**反馈：**"):
                current["feedback"] = line[6:].strip()
        if current:
            mistakes.append(current)
        return mistakes

    def _parse_mistake_entry(self, raw):
        """从原始 entry 文本解析错题字段。"""
        mistake = {
            "title": "", "timestamp": "", "source": "",
            "score": 0, "count": 1, "question": "", "user_answer": "",
            "correct_answer": "", "feedback": "",
        }
        for line in raw.strip().split("\n"):
            if line.startswith("## "):
                mistake["title"] = line[3:].strip()
            elif line.startswith("- 时间:"):
                mistake["timestamp"] = line[5:].strip()
            elif line.startswith("- 来源:"):
                mistake["source"] = line[5:].strip()
            elif line.startswith("- 得分:"):
                try:
                    mistake["score"] = int(line[5:].strip())
                except ValueError:
                    pass
            elif line.startswith("- 错误次数:"):
                try:
                    mistake["count"] = int(line[6:].strip())
                except ValueError:
                    pass
            elif line.startswith("**题目：**"):
                mistake["question"] = line[7:].strip()
            elif line.startswith("**你的回答：**"):
                mistake["user_answer"] = line[8:].strip()
            elif line.startswith("**正确答案：**"):
                mistake["correct_answer"] = line[8:].strip()
            elif line.startswith("**反馈：**"):
                mistake["feedback"] = line[6:].strip()
        return mistake if mistake["title"] else None

    def get_trusted_paths(self):
        return self.data.get("trusted_paths", [])

    def get_notes_by_path(self, file_path):
        """获取与指定路径关联的笔记。"""
        notes = self.get_notes(limit=100)
        return [n for n in notes if n.get("file_path") == file_path]

    def get_recent_notes_for_practice(self, limit=10):
        """获取最近的笔记用于练习。"""
        return self.get_notes(limit=limit)

    def get_context_summary(self):
        """生成用于注入 prompt 的紧凑摘要（控制在 800 字符内）。"""
        level_raw = self.data.get("level", "beginner")
        level_cn = self.LEVEL_NAMES.get(level_raw, level_raw)
        lines = [f"用户: {self.username} | 水平: {level_cn} | 连续学习: {self.data.get('streak', 0)} 天"]

        frequent_topics = self.data.get("frequent_topics", [])
        if frequent_topics:
            lines.append(f"常聊话题: {', '.join(frequent_topics[:5])}")

        weak_areas = self.data.get("weak_areas", [])
        if weak_areas:
            lines.append(f"薄弱领域: {', '.join(weak_areas[:3])}")

        mistakes = self.data.get("mistakes", [])
        if mistakes:
            groups = {}
            for m in mistakes[-10:]:
                t = m.get("type", "general")
                groups[t] = groups.get(t, 0) + 1
            top_mistakes = sorted(groups.items(), key=lambda x: -x[1])[:3]
            mistake_str = ", ".join(f"{t}({c}次)" for t, c in top_mistakes)
            lines.append(f"常见错误: {mistake_str}")

        notes = self.data.get("notes", [])[-3:]
        if notes:
            lines.append("最近笔记:")
            for n in notes:
                lines.append(f"  - {n.get('title', '')[:40]}")

        return "\n".join(lines)

    def get_all_notes_for_retrieval(self):
        """返回所有笔记，格式兼容 memory.retrieval_candidates 的输入。"""
        result = []
        for n in self.data.get("notes", []):
            result.append({
                "text": n.get("content", "")[:500],
                "tags": [n.get("file_path", "")],
                "source": n.get("file_path", ""),
                "created_at": n.get("timestamp", ""),
            })
        for l in self.data.get("learnings", []):
            text = l.get("text", "") if isinstance(l, dict) else str(l)
            result.append({
                "text": text[:500],
                "tags": ["learning"],
                "source": "learnings",
                "created_at": l.get("date", "") if isinstance(l, dict) else "",
            })
        return result

    def get_progress(self, days=7):
        daily = self.data.get("daily_log", {})
        today = datetime.now()
        result = []
        for i in range(days):
            d = (today - __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
            entry = daily.get(d, {"tasks": 0, "mistakes_count": 0})
            result.append({"date": d, **entry})
        return result

    LEVEL_NAMES = {"beginner": "初学者", "intermediate": "进阶", "advanced": "熟练", "expert": "专家"}

    def get_profile_summary(self):
        mistakes = self.data.get("mistakes", [])
        recent_mistakes = mistakes[-5:] if mistakes else []

        level_raw = self.data.get("level", "beginner")
        level_cn = self.LEVEL_NAMES.get(level_raw, level_raw)
        total_tasks = sum(d.get("tasks", 0) for d in self.data.get("daily_log", {}).values())

        positioning = self.data.get("user_positioning", "")
        frequent_topics = self.data.get("frequent_topics", [])
        weak_areas = self.data.get("weak_areas", [])
        pref = self.data.get("user_preference", {})
        preference_type = pref.get("preference_type", "")

        lines = [f"用户: {self.username}"]
        if positioning:
            lines.append(f"定位: {positioning}")
        lines.append(f"水平: {level_cn}")
        lines.append(f"连续学习: {self.data.get('streak', 0)} 天")
        lines.append(f"累计任务: {total_tasks}")

        if frequent_topics:
            lines.append(f"常聊话题: {', '.join(frequent_topics[:5])}")

        if weak_areas:
            lines.append(f"薄弱领域: {', '.join(weak_areas[:5])}")

        if preference_type:
            lines.append(f"用户偏好: {preference_type}")

        if recent_mistakes:
            lines.append("近期错误:")
            for m in recent_mistakes:
                lines.append(f"  - [{m.get('type', '通用')}] {m.get('desc', '')}")

        return "\n".join(lines)

    def get_daily_summary(self):
        daily = self.data.get("daily_log", {})
        today = _today_str()
        yesterday = (datetime.now() - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")

        today_data = daily.get(today, {"tasks": 0, "mistakes_count": 0})
        yesterday_data = daily.get(yesterday, {"tasks": 0, "mistakes_count": 0})

        mistakes = self.data.get("mistakes", [])
        week_ago = (datetime.now() - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")
        recent_mistakes = [m for m in mistakes if m.get("date", "") >= week_ago]

        mistake_types = {}
        for m in recent_mistakes:
            t = m.get("type", "general")
            mistake_types[t] = mistake_types.get(t, 0) + 1

        lines = [f"--- 每日总结 ({today}) ---"]
        lines.append(f"连续学习: {self.data.get('streak', 0)} 天")
        lines.append(f"昨日: {yesterday_data['tasks']} 个任务, {yesterday_data['mistakes_count']} 个错误")
        lines.append(f"今日: {today_data['tasks']} 个任务")

        if mistake_types:
            lines.append("本周常见错误:")
            for mtype, count in sorted(mistake_types.items(), key=lambda x: -x[1]):
                lines.append(f"  - {mtype}: {count} 次")

        frequent_topics = self.data.get("frequent_topics", [])
        if frequent_topics:
            lines.append("常聊话题:")
            for topic in frequent_topics[:5]:
                lines.append(f"  - {topic}")

        return "\n".join(lines)

    def should_show_daily_summary(self):
        today = _today_str()
        return self.data.get("last_summary_date") != today

    def mark_summary_shown(self):
        self.data["last_summary_date"] = _today_str()
        self.save()

    def update_learning_context(self, learning_style=None, weak_areas=None, frequent_topics=None):
        """更新学习偏好、薄弱领域、常问话题。"""
        if learning_style is not None:
            self.data["learning_style"] = learning_style
        if weak_areas is not None:
            self.data["weak_areas"] = list(set(weak_areas))
        if frequent_topics is not None:
            self.data["frequent_topics"] = list(set(frequent_topics))
        self.save()

    def set_positioning(self, positioning):
        """设置用户定位。"""
        self.data["user_positioning"] = positioning.strip()
        self.save()

    def record_ask_topics(self, topics):
        """从 /ask 会话中记录常聊话题。"""
        freq = self.data.get("frequent_topics", [])
        for topic in topics:
            topic = topic.strip()
            if not topic:
                continue
            if topic not in freq:
                freq.append(topic)
        # 只保留最近 20 个话题
        self.data["frequent_topics"] = freq[-20:]
        # 更新用户偏好
        pref = self.data.setdefault("user_preference", {
            "ask_count": 0, "practice_count": 0, "topics_explored": {}, "preference_type": "",
        })
        pref["ask_count"] = pref.get("ask_count", 0) + 1
        for topic in topics:
            topic = topic.strip()
            if topic:
                pref.setdefault("topics_explored", {})[topic] = pref.get("topics_explored", {}).get(topic, 0) + 1
        self._update_preference_type()
        self.save()

    def record_practice_weak_area(self, topic, score):
        """/practice 答错时记录薄弱领域。"""
        weak = self.data.get("weak_areas", [])
        if topic and topic not in weak:
            weak.append(topic)
        self.data["weak_areas"] = weak[-15:]
        # 更新用户偏好
        pref = self.data.setdefault("user_preference", {
            "ask_count": 0, "practice_count": 0, "topics_explored": {}, "preference_type": "",
        })
        pref["practice_count"] = pref.get("practice_count", 0) + 1
        self._update_preference_type()
        self.save()

    def _update_preference_type(self):
        """根据使用行为推断用户偏好类型。"""
        pref = self.data.get("user_preference", {})
        ask = pref.get("ask_count", 0)
        practice = pref.get("practice_count", 0)
        topics = pref.get("topics_explored", {})

        if ask + practice == 0:
            pref["preference_type"] = ""
            return

        # 探索型 vs 深耕型：话题分散度
        if topics:
            topic_counts = list(topics.values())
            avg = sum(topic_counts) / len(topic_counts)
            max_count = max(topic_counts)
            # 如果最高频话题远高于平均，是深耕型
            if max_count > avg * 2 and len(topics) <= 5:
                explore_type = "深耕型"
            elif len(topics) > 8:
                explore_type = "探索型"
            else:
                explore_type = "均衡型"
        else:
            explore_type = ""

        # 新知型 vs 温习型：ask vs practice 比率
        total = ask + practice
        if practice / total > 0.6:
            review_type = "温习型"
        elif ask / total > 0.6:
            review_type = "新知型"
        else:
            review_type = "均衡型"

        pref["preference_type"] = f"{explore_type}、{review_type}" if explore_type else review_type

    def record_session(self, session_id, title, created_at, history_count, work_dir, topics=None, summary=""):
        """记录一个 session 摘要。"""
        sessions = self.data.setdefault("sessions", [])
        entry = {
            "id": session_id,
            "title": title[:100],
            "created_at": created_at,
            "history_count": history_count,
            "work_dir": work_dir,
            "topics": topics or [],
            "summary": summary[:300],
        }
        # 去重（按 id）
        sessions = [s for s in sessions if s.get("id") != session_id]
        sessions.append(entry)
        # 按时间排序，保留最近 30 个
        sessions.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        self.data["sessions"] = sessions[:30]
        self.save()

    def _infer_weak_areas(self):
        """从错题中自动推断薄弱领域。"""
        mistakes = self.data.get("mistakes", [])
        type_counts = {}
        for m in mistakes:
            t = m.get("type", "general")
            if t != "general":
                type_counts[t] = type_counts.get(t, 0) + 1
        # 取错误次数最多的前 5 个类型
        sorted_types = sorted(type_counts.items(), key=lambda x: -x[1])[:5]
        return [t for t, _ in sorted_types]

    def _infer_frequent_topics(self):
        """从技能使用记录中推断常问话题。"""
        skills = self.data.get("skills", {})
        # 按使用次数排序，取前 8 个
        sorted_skills = sorted(skills.items(), key=lambda x: -x[1].get("count", 0))[:8]
        return [name for name, _ in sorted_skills]

    def export_as_skill(self, output_dir=None, session_store=None, compress_old_sessions=True):
        """将用户画像、笔记、错题、session 导出为可复用的 skill 目录。

        输出结构：
        {output_dir}/
          SKILL.md          — skill 元数据 + 用户画像三要素
          references/
            notes.md        — 笔记导出
            mistakes.md     — 错题导出
            sessions/       — session 导出
              session_*.md  — 最近 10 个完整 session
              older_summary.md — 更早 session 的压缩摘要
        """
        from pathlib import Path as _Path

        if output_dir is None:
            output_dir = _Path.home() / ".bongo" / "skills" / self.username
        output_dir = _Path(output_dir)
        refs_dir = output_dir / "references"
        sessions_dir = refs_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # 画像三要素
        positioning = self.data.get("user_positioning", "")
        frequent_topics = self.data.get("frequent_topics", [])
        weak_areas = self.data.get("weak_areas", [])
        pref = self.data.get("user_preference", {})
        preference_type = pref.get("preference_type", "")

        level_raw = self.data.get("level", "beginner")
        level_cn = self.LEVEL_NAMES.get(level_raw, level_raw)
        total_tasks = sum(d.get("tasks", 0) for d in self.data.get("daily_log", {}).values())
        streak = self.data.get("streak", 0)

        # --- SKILL.md ---
        skill_md = f"""---
name: user-{self.username}
description: "{positioning or self.username + ' 的个人学习画像'}。包含常聊话题、薄弱领域、学习偏好。当讨论到 {', '.join(frequent_topics[:5]) if frequent_topics else '技术'} 相关话题时参考。"
when-to-use: "当需要了解 {self.username} 的学习背景、常见错误模式、或复习已学内容时触发。"
arguments: [mode]
argument-hint: "<mode: profile|notes|mistakes|sessions|review>"
version: 2.0.0
---

# {self.username} 的学习画像

## 用户定位
{positioning or '未设置'}

## 基本信息
- 水平: {level_cn}
- 连续学习: {streak} 天
- 累计任务: {total_tasks} 个

## 画像三要素

### 常聊话题
"""
        if frequent_topics:
            for topic in frequent_topics:
                skill_md += f"- {topic}\n"
        else:
            skill_md += "- 暂无记录\n"

        skill_md += "\n### 薄弱领域\n"
        if weak_areas:
            for area in weak_areas:
                skill_md += f"- {area}\n"
        else:
            skill_md += "- 暂无记录\n"

        skill_md += f"\n### 用户偏好\n{preference_type or '数据积累中'}\n"

        skill_md += f"""
## 可用资源

本 skill 包含以下资源，可通过对应的 mode 参数访问：

- `references/notes.md` — 学习笔记
- `references/mistakes.md` — 错题本
- `references/sessions/` — 历史会话记录（最近 10 个完整，更早的已压缩）

## 模式说明

### profile 模式（默认）
展示完整的用户画像，包括三要素和技能详情。

### notes 模式
读取学习笔记，展示最近的记录。

### mistakes 模式
读取错题本，按类型分组展示。

### sessions 模式
读取历史会话记录，展示最近的学习过程。

### review 模式
基于用户的薄弱领域和常错类型，生成针对性的复习计划。

## 使用建议

- 该用户定位为: {positioning or '未设置'}
- 讨论到 **{', '.join(frequent_topics[:3]) if frequent_topics else '技术'}** 等话题时，优先参考该用户的技能水平和常见错误
- 薄弱领域: {', '.join(weak_areas[:3]) if weak_areas else '待分析'}
- 用户偏好: {preference_type or '数据积累中'}
"""
        (output_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

        # --- references/notes.md ---
        notes_md = f"# {self.username} 的学习笔记\n\n"
        notes = self.get_notes(limit=50)
        if notes:
            for note in notes:
                notes_md += f"## {note.get('title', '无标题')}\n"
                notes_md += f"- 时间: {note.get('timestamp', '')}\n"
                if note.get("file_path"):
                    notes_md += f"- 关联文件: {note['file_path']}\n"
                notes_md += f"\n{note.get('content', '')}\n\n"
        else:
            notes_md += "暂无笔记。\n"
        (refs_dir / "notes.md").write_text(notes_md, encoding="utf-8")

        # --- references/mistakes.md ---
        mistakes_md = f"# {self.username} 的错题本\n\n"
        mistakes = self.get_mistakes_from_file(limit=50)
        if mistakes:
            by_type = {}
            for m in mistakes:
                t = m.get("source", "") or m.get("title", "未分类")
                by_type.setdefault(t, []).append(m)

            for mtype, items in by_type.items():
                mistakes_md += f"## {mtype}（{len(items)} 题）\n\n"
                for m in items:
                    mistakes_md += f"### {m.get('title', '无标题')}\n"
                    mistakes_md += f"- 时间: {m.get('timestamp', '')}\n"
                    mistakes_md += f"- 得分: {m.get('score', 0)}\n"
                    mistakes_md += f"- 错误次数: {m.get('count', 1)}\n"
                    if m.get("question"):
                        mistakes_md += f"\n**题目：** {m['question']}\n"
                    if m.get("user_answer"):
                        mistakes_md += f"\n**你的回答：** {m['user_answer']}\n"
                    if m.get("correct_answer"):
                        mistakes_md += f"\n**正确答案：** {m['correct_answer']}\n"
                    if m.get("feedback"):
                        mistakes_md += f"\n**反馈：** {m['feedback']}\n"
                    mistakes_md += "\n"
        else:
            mistakes_md += "暂无错题。\n"
        (refs_dir / "mistakes.md").write_text(mistakes_md, encoding="utf-8")

        # --- references/sessions/ ---
        exported_sessions = self._export_sessions(sessions_dir, session_store, compress_old_sessions)

        return {
            "output_dir": str(output_dir),
            "files": [
                str(output_dir / "SKILL.md"),
                str(refs_dir / "notes.md"),
                str(refs_dir / "mistakes.md"),
            ] + [str(sessions_dir / f) for f in exported_sessions],
            "summary": {
                "username": self.username,
                "positioning": positioning,
                "level": level_cn,
                "notes_count": len(notes),
                "mistakes_count": len(mistakes),
                "sessions_count": len(exported_sessions),
                "frequent_topics": frequent_topics,
                "weak_areas": weak_areas,
                "preference_type": preference_type,
            },
        }

    def _export_sessions(self, sessions_dir, session_store=None, compress_old=True):
        """导出 session 到 sessions 目录。最近 10 个完整导出，更早的压缩。"""
        import json as _json
        exported = []

        sessions = self.data.get("sessions", [])
        if not sessions:
            (sessions_dir / "no_sessions.md").write_text("暂无会话记录。\n", encoding="utf-8")
            return ["no_sessions.md"]

        # 最近 10 个完整导出
        recent = sessions[:10]
        older = sessions[10:]

        for s in recent:
            filename = f"session_{s.get('id', 'unknown')}.md"
            md = f"# Session: {s.get('title', '无标题')}\n\n"
            md += f"- ID: {s.get('id', '')}\n"
            md += f"- 时间: {s.get('created_at', '')}\n"
            md += f"- 消息数: {s.get('history_count', 0)}\n"
            md += f"- 工作目录: {s.get('work_dir', '')}\n"
            if s.get("topics"):
                md += f"- 话题: {', '.join(s['topics'])}\n"
            if s.get("summary"):
                md += f"\n## 摘要\n{s['summary']}\n"

            # 如果有 session_store，尝试加载完整 history
            if session_store and s.get("id"):
                try:
                    full_session = session_store.load(s["id"])
                    history = full_session.get("history", [])
                    if history:
                        md += "\n## 完整对话记录\n\n"
                        for item in history:
                            role = item.get("role", "")
                            content = item.get("content", "")
                            if isinstance(content, list):
                                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                                tool_parts = [f"[调用工具: {p.get('name', '')}]" for p in content if isinstance(p, dict) and p.get("type") == "tool_use"]
                                content = " ".join(text_parts + tool_parts)
                            md += f"**{role}**: {str(content)[:500]}\n\n"
                except Exception:
                    md += "\n（无法加载完整对话记录）\n"

            (sessions_dir / filename).write_text(md, encoding="utf-8")
            exported.append(filename)

        # 更早的 session 压缩成一个摘要文件
        if older:
            summary_md = "# 更早的会话摘要\n\n"
            summary_md += f"共 {len(older)} 个更早的会话（按时间倒序）：\n\n"
            for s in older:
                summary_md += f"## {s.get('title', '无标题')}\n"
                summary_md += f"- 时间: {s.get('created_at', '')}\n"
                summary_md += f"- 消息数: {s.get('history_count', 0)}\n"
                if s.get("topics"):
                    summary_md += f"- 话题: {', '.join(s['topics'])}\n"
                if s.get("summary"):
                    summary_md += f"- 摘要: {s['summary']}\n"
                summary_md += "\n"
            (sessions_dir / "older_summary.md").write_text(summary_md, encoding="utf-8")
            exported.append("older_summary.md")

        return exported


def save_current_user(username):
    CURRENT_USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_USER_FILE.write_text(username, encoding="utf-8")


def load_current_user():
    if CURRENT_USER_FILE.exists():
        return CURRENT_USER_FILE.read_text(encoding="utf-8").strip()
    return "default"


def list_profiles(profiles_dir=None):
    profiles_dir = Path(profiles_dir or DEFAULT_PROFILES_DIR)
    if not profiles_dir.exists():
        return []
    return [p.stem for p in sorted(profiles_dir.glob("*.json"))]
