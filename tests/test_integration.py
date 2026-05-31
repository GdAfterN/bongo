"""Integration tests simulating /ask agent CRUD operations.

Each test verifies:
1. Operation succeeds
2. Tool completes in a single call (no retries needed)
3. Output is clean (no errors, no unexpected content)
"""

import os
import sys
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def env(tmp_path):
    """Set up isolated environment with workspace, agent, and profile."""
    import bongo.profile as pp

    old_profiles = pp.DEFAULT_PROFILES_DIR
    old_notes = pp.DEFAULT_NOTES_DIR
    old_mistakes = pp.DEFAULT_MISTAKES_DIR
    old_current = pp.CURRENT_USER_FILE

    pp.DEFAULT_PROFILES_DIR = tmp_path / "profiles"
    pp.DEFAULT_NOTES_DIR = tmp_path / "workspace" / "notes"
    pp.DEFAULT_MISTAKES_DIR = tmp_path / "workspace" / "mistakes"
    pp.CURRENT_USER_FILE = tmp_path / "current_user"
    pp.CURRENT_USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    pp.CURRENT_USER_FILE.write_text("testuser", encoding="utf-8")

    work_dir = tmp_path / "workspace"
    work_dir.mkdir()

    agent = MagicMock()
    agent.root = work_dir
    agent.depth = 0
    agent.max_depth = 3

    def path(p):
        p_path = Path(p)
        if p_path.is_absolute():
            return p_path
        return (work_dir / p).resolve()

    agent.path = path
    agent.shell_env.return_value = os.environ.copy()

    from bongo.profile import UserProfile
    profile = UserProfile("testuser")

    yield {"agent": agent, "profile": profile, "work_dir": work_dir}

    pp.DEFAULT_PROFILES_DIR = old_profiles
    pp.DEFAULT_NOTES_DIR = old_notes
    pp.DEFAULT_MISTAKES_DIR = old_mistakes
    pp.CURRENT_USER_FILE = old_current


class TestNoteReadOperations:
    """Test reading notes - should complete in one call."""

    def test_list_notes(self, env):
        """read_notes returns note list, no errors."""
        from bongo.tools import tool_write_note, tool_read_notes

        for t in ["Note A", "Note B", "Note C"]:
            tool_write_note(env["agent"], {"title": t, "content": f"Content of {t}"})

        result = tool_read_notes(env["agent"], {"limit": 10})

        # Should contain all notes
        assert "Note A" in result
        assert "Note B" in result
        assert "Note C" in result
        # Should not contain errors
        assert "error" not in result.lower()
        assert "超出" not in result

    def test_read_entry_by_number(self, env):
        """read_entry returns correct note content, no errors."""
        from bongo.tools import tool_write_note, tool_read_entry

        tool_write_note(env["agent"], {"title": "Target Note", "content": "Specific content here"})
        tool_write_note(env["agent"], {"title": "Other Note", "content": "Other content"})

        notes_file = env["profile"].notes_file
        result = tool_read_entry(env["agent"], {"path": str(notes_file), "entry": 1})

        assert "Target Note" in result
        assert "Specific content" in result
        assert "error" not in result.lower()

    def test_read_entry_second(self, env):
        """read_entry entry=2 returns second note."""
        from bongo.tools import tool_write_note, tool_read_entry

        tool_write_note(env["agent"], {"title": "First", "content": "First content"})
        tool_write_note(env["agent"], {"title": "Second", "content": "Second content"})

        notes_file = env["profile"].notes_file
        result = tool_read_entry(env["agent"], {"path": str(notes_file), "entry": 2})

        assert "Second" in result
        assert "Second content" in result
        assert "error" not in result.lower()


class TestNoteWriteOperations:
    """Test writing/modifying notes - should complete in one call."""

    def test_write_new_note(self, env):
        """write_note succeeds, note appears in list."""
        from bongo.tools import tool_write_note, tool_read_notes

        result = tool_write_note(env["agent"], {"title": "New Note", "content": "New content"})
        assert "已保存" in result
        assert "error" not in result.lower()

        # Verify it's in the list
        notes = tool_read_notes(env["agent"], {"limit": 10})
        assert "New Note" in notes

    def test_patch_note_content(self, env):
        """patch_file modifies content, returns 'patched'."""
        from bongo.tools import tool_write_file, tool_patch_file

        tool_write_file(env["agent"], {"path": "test.md", "content": "Hello World\nFoo Bar\n"})
        result = tool_patch_file(env["agent"], {
            "path": "test.md",
            "old_text": "Hello World",
            "new_text": "Hello Bongo",
        })

        assert "patched" in result.lower()
        assert "error" not in result.lower()

        # Verify content changed
        content = (env["work_dir"] / "test.md").read_text(encoding="utf-8")
        assert "Hello Bongo" in content
        assert "Hello World" not in content

    def test_patch_note_append(self, env):
        """patch_file appends text to end of line."""
        from bongo.tools import tool_write_file, tool_patch_file

        tool_write_file(env["agent"], {"path": "append.md", "content": "Some text here.\n"})
        result = tool_patch_file(env["agent"], {
            "path": "append.md",
            "old_text": "Some text here.",
            "new_text": "Some text here. 我爱你",
        })

        assert "patched" in result.lower()
        content = (env["work_dir"] / "append.md").read_text(encoding="utf-8")
        assert "我爱你" in content


class TestNoteDeleteOperations:
    """Test deleting notes - should complete in one call, no verify read needed."""

    def test_delete_note(self, env):
        """delete_entry removes note, returns success message."""
        from bongo.tools import tool_write_note, tool_delete_entry

        for t in ["Keep A", "Remove This", "Keep B"]:
            tool_write_note(env["agent"], {"title": t, "content": f"Content {t}"})

        notes_file = env["profile"].notes_file
        result = tool_delete_entry(env["agent"], {"path": str(notes_file), "entry": 2})

        assert "已删除" in result
        assert "第 2 条" in result
        assert "error" not in result.lower()

    def test_delete_then_read_remaining(self, env):
        """After delete, remaining entries are still accessible."""
        from bongo.tools import tool_write_note, tool_delete_entry, tool_read_entry

        for t in ["Alpha", "Beta", "Gamma"]:
            tool_write_note(env["agent"], {"title": t, "content": f"Content {t}"})

        notes_file = env["profile"].notes_file
        tool_delete_entry(env["agent"], {"path": str(notes_file), "entry": 2})

        # Entry 1 should still be Alpha
        r1 = tool_read_entry(env["agent"], {"path": str(notes_file), "entry": 1})
        assert "Alpha" in r1
        assert "error" not in r1.lower()

        # Entry 2 should now be Gamma
        r2 = tool_read_entry(env["agent"], {"path": str(notes_file), "entry": 2})
        assert "Gamma" in r2
        assert "error" not in r2.lower()


class TestMistakeOperations:
    """Test mistake CRUD operations."""

    def _add_mistakes(self, profile, questions):
        for q in questions:
            profile.add_mistake(
                question=q, user_answer="wrong", score=40,
                feedback="incorrect", source="test",
            )

    def test_list_mistakes(self, env):
        """search_mistakes returns results."""
        from bongo.tools import tool_search_mistakes

        self._add_mistakes(env["profile"], ["What is closure?", "What is GIL?"])

        result = tool_search_mistakes(env["agent"], {"query": "closure"})
        assert "error" not in result.lower()

    def test_read_mistake_entry(self, env):
        """read_entry on mistakes file returns correct entry."""
        from bongo.tools import tool_read_entry

        self._add_mistakes(env["profile"], ["Question A", "Question B"])

        mistakes_file = env["profile"].mistakes_file
        result = tool_read_entry(env["agent"], {"path": str(mistakes_file), "entry": 1})

        assert "Question A" in result
        assert "error" not in result.lower()

    def test_delete_mistake(self, env):
        """delete_entry on mistakes file succeeds."""
        from bongo.tools import tool_delete_entry

        self._add_mistakes(env["profile"], ["Q1", "Q2", "Q3"])

        mistakes_file = env["profile"].mistakes_file
        result = tool_delete_entry(env["agent"], {"path": str(mistakes_file), "entry": 2})

        assert "已删除" in result
        assert "error" not in result.lower()

    def test_get_mistake_detail(self, env):
        """get_mistake_detail returns full content."""
        from bongo.tools import tool_get_mistake_detail

        self._add_mistakes(env["profile"], ["What is polymorphism?"])

        result = tool_get_mistake_detail(env["agent"], {"title": "What is polymorphism?"})
        assert "polymorphism" in result.lower() or "多态" in result
        assert "error" not in result.lower()


class TestFileOperations:
    """Test general file operations."""

    def test_write_and_read(self, env):
        """write_file then read_file returns correct content."""
        from bongo.tools import tool_write_file, tool_read_file

        tool_write_file(env["agent"], {"path": "hello.txt", "content": "Hello World\n"})
        result = tool_read_file(env["agent"], {"path": "hello.txt", "start": 1, "end": 5})

        assert "Hello World" in result
        assert "error" not in result.lower()

    def test_search(self, env):
        """search finds matching content."""
        from bongo.tools import tool_write_file, tool_search

        tool_write_file(env["agent"], {"path": "searchable.txt", "content": "alpha beta gamma\n"})
        result = tool_search(env["agent"], {"pattern": "beta", "path": "searchable.txt"})

        assert "beta" in result
        assert "error" not in result.lower()


class TestReadEntryAfterPatch:
    """Test that read_entry works after patch_file (UTF-8 fallback)."""

    def test_read_entry_after_patch_single(self, env):
        """After patch_file changes notes content, read_entry still works."""
        from bongo.tools import tool_write_note, tool_patch_file, tool_read_entry

        tool_write_note(env["agent"], {
            "title": "Patchable Note",
            "content": "Original content here. End marker.",
        })

        notes_file = env["profile"].notes_file

        # Patch the content (adds bytes, shifts offsets)
        tool_patch_file(env["agent"], {
            "path": str(notes_file),
            "old_text": "End marker.",
            "new_text": "End marker. 我爱你",
        })

        # read_entry should still work (fallback to full text parse)
        result = tool_read_entry(env["agent"], {"path": str(notes_file), "entry": 1})
        assert "Patchable Note" in result
        assert "error" not in result.lower()

    def test_read_entry_after_patch_second_entry(self, env):
        """After patch, second entry is still readable."""
        from bongo.tools import tool_write_note, tool_patch_file, tool_read_entry

        tool_write_note(env["agent"], {"title": "First Note", "content": "First content."})
        tool_write_note(env["agent"], {"title": "Second Note", "content": "Second content. End."})

        notes_file = env["profile"].notes_file

        # Patch first note (shifts byte offsets for second)
        tool_patch_file(env["agent"], {
            "path": str(notes_file),
            "old_text": "First content.",
            "new_text": "First content. Added text.",
        })

        # Second entry should still be readable
        result = tool_read_entry(env["agent"], {"path": str(notes_file), "entry": 2})
        assert "Second Note" in result
        assert "error" not in result.lower()


class TestDeleteCooldownIntegration:
    """Test that delete cooldown prevents repeated deletes."""

    def test_cooldown_blocks_second_delete(self, env):
        """First delete succeeds, second is blocked by cooldown."""
        from unittest.mock import MagicMock
        from bongo.runtime import bongo as BongoAgent

        work_dir = env["work_dir"]
        mock_client = MagicMock()
        mock_client.get_provider_name.return_value = "test"

        agent = BongoAgent(
            model_client=mock_client,
            work_dir=str(work_dir),
            max_steps=5,
            approval_policy="auto",
        )

        from bongo.profile import UserProfile
        test_profile = UserProfile("testuser", notes_dir=work_dir / "notes")
        for t in ["X", "Y", "Z"]:
            test_profile.add_note(title=t, content=f"Content {t}")

        notes_file = test_profile.notes_file

        # First delete succeeds
        r1 = agent.run_tool("delete_entry", {"path": str(notes_file), "entry": 2})
        assert "已删除" in r1

        # Second delete blocked by cooldown
        r2 = agent.run_tool("delete_entry", {"path": str(notes_file), "entry": 2})
        assert "已删除" not in r2
        assert "error" in r2.lower() or "cooldown" in r2.lower()

    def test_cooldown_blocks_read_after_delete(self, env):
        """After delete, read on same file is blocked."""
        from unittest.mock import MagicMock
        from bongo.runtime import bongo as BongoAgent

        work_dir = env["work_dir"]
        mock_client = MagicMock()
        mock_client.get_provider_name.return_value = "test"

        agent = BongoAgent(
            model_client=mock_client,
            work_dir=str(work_dir),
            max_steps=5,
            approval_policy="auto",
        )

        from bongo.profile import UserProfile
        test_profile = UserProfile("testuser", notes_dir=work_dir / "notes")
        for t in ["A", "B"]:
            test_profile.add_note(title=t, content=f"Content {t}")

        notes_file = test_profile.notes_file

        # Delete succeeds
        r1 = agent.run_tool("delete_entry", {"path": str(notes_file), "entry": 1})
        assert "已删除" in r1

        # Read on same file blocked
        r2 = agent.run_tool("read_file", {"path": str(notes_file), "start": 1, "end": 50})
        assert "error" in r2.lower() or "final answer" in r2.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
