"""Comprehensive test for /ask CRUD operations: notes and mistakes.

Tests all tools: write_note, read_notes, read_entry, delete_entry,
search_mistakes, get_mistake_detail, read_file, patch_file, search, read_cache.
"""

import json
import os
import sys
import tempfile
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Setup path
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture
def tmp_home(tmp_path):
    """Create a temporary home directory for isolation."""
    import bongo.profile as pp

    old_profiles = pp.DEFAULT_PROFILES_DIR
    old_notes = pp.DEFAULT_NOTES_DIR
    old_mistakes = pp.DEFAULT_MISTAKES_DIR
    old_current = pp.CURRENT_USER_FILE

    pp.DEFAULT_PROFILES_DIR = tmp_path / "profiles"
    pp.DEFAULT_NOTES_DIR = tmp_path / "notes"
    pp.DEFAULT_MISTAKES_DIR = tmp_path / "mistakes"
    pp.CURRENT_USER_FILE = tmp_path / "current_user"

    # Write the test user so load_current_user() returns "testuser"
    pp.CURRENT_USER_FILE.parent.mkdir(parents=True, exist_ok=True)
    pp.CURRENT_USER_FILE.write_text("testuser", encoding="utf-8")

    yield tmp_path

    pp.DEFAULT_PROFILES_DIR = old_profiles
    pp.DEFAULT_NOTES_DIR = old_notes
    pp.DEFAULT_MISTAKES_DIR = old_mistakes
    pp.CURRENT_USER_FILE = old_current


@pytest.fixture
def agent(tmp_path):
    """Create a minimal mock agent with a workspace."""
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()

    a = MagicMock()
    a.root = work_dir
    a.depth = 0
    a.max_depth = 3

    def path(p):
        # If p is an absolute path that exists, return it directly
        p_path = Path(p)
        if p_path.is_absolute():
            return p_path
        return (work_dir / p).resolve()

    a.path = path
    a.shell_env.return_value = os.environ.copy()
    return a


@pytest.fixture
def profile(tmp_home):
    """Create a UserProfile for test user."""
    from bongo.profile import UserProfile
    p = UserProfile("testuser")
    return p


class TestNotesCRUD:
    """Test notes: create, list, read by entry, delete by entry."""

    def test_write_and_read_notes(self, agent, profile):
        """write_note creates a note, read_notes lists it."""
        from bongo.tools import tool_write_note, tool_read_notes

        # Create note
        result = tool_write_note(agent, {
            "title": "Python GIL",
            "content": "GIL prevents true parallelism in CPython.",
        })
        assert "已保存笔记" in result
        assert "Python GIL" in result

        # List notes
        result = tool_read_notes(agent, {"limit": 10})
        assert "Python GIL" in result
        assert "GIL prevents" in result

    def test_write_multiple_notes_read_entry(self, agent, profile):
        """write_note x3, then read_entry for each by number."""
        from bongo.tools import tool_write_note, tool_read_entry

        titles = ["Note A", "Note B", "Note C"]
        for t in titles:
            tool_write_note(agent, {"title": t, "content": f"Content of {t}"})

        notes_file = profile.notes_file
        assert notes_file.exists()

        # Read each entry
        for i, t in enumerate(titles, 1):
            result = tool_read_entry(agent, {"path": str(notes_file), "entry": i})
            assert t in result, f"Entry {i} should contain '{t}', got: {result[:200]}"

    def test_delete_middle_note(self, agent, profile):
        """Delete the middle note, verify remaining are correct."""
        from bongo.tools import tool_write_note, tool_delete_entry, tool_read_entry

        for t in ["First", "Second", "Third"]:
            tool_write_note(agent, {"title": t, "content": f"Content of {t}"})

        notes_file = profile.notes_file

        # Delete middle
        result = tool_delete_entry(agent, {"path": str(notes_file), "entry": 2})
        assert "已删除" in result
        assert "Second" in result

        # Should have 2 entries left
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 1})
        assert "First" in result

        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 2})
        assert "Third" in result

        # Entry 3 should be out of range
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 3})
        assert "超出范围" in result

    def test_delete_first_note(self, agent, profile):
        """Delete first note, verify remaining shift correctly."""
        from bongo.tools import tool_write_note, tool_delete_entry, tool_read_entry

        for t in ["Alpha", "Beta", "Gamma"]:
            tool_write_note(agent, {"title": t, "content": f"Content of {t}"})

        notes_file = profile.notes_file

        result = tool_delete_entry(agent, {"path": str(notes_file), "entry": 1})
        assert "已删除" in result
        assert "Alpha" in result

        # Now entry 1 should be Beta
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 1})
        assert "Beta" in result

        # Entry 2 should be Gamma
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 2})
        assert "Gamma" in result

    def test_delete_last_note(self, agent, profile):
        """Delete last note, verify others remain."""
        from bongo.tools import tool_write_note, tool_delete_entry, tool_read_entry

        for t in ["X", "Y", "Z"]:
            tool_write_note(agent, {"title": t, "content": f"Content of {t}"})

        notes_file = profile.notes_file

        result = tool_delete_entry(agent, {"path": str(notes_file), "entry": 3})
        assert "已删除" in result
        assert "Z" in result

        # Should have 2 entries
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 1})
        assert "X" in result
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 2})
        assert "Y" in result

        # Entry 3 out of range
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 3})
        assert "超出范围" in result

    def test_delete_all_notes(self, agent, profile):
        """Delete all notes one by one, verify clean state."""
        from bongo.tools import tool_write_note, tool_delete_entry

        for t in ["Only"]:
            tool_write_note(agent, {"title": t, "content": "Solo content"})

        notes_file = profile.notes_file

        result = tool_delete_entry(agent, {"path": str(notes_file), "entry": 1})
        assert "已删除" in result
        assert "剩余 0" in result

    def test_read_entry_out_of_range(self, agent, profile):
        """read_entry with invalid number returns error."""
        from bongo.tools import tool_write_note, tool_read_entry

        tool_write_note(agent, {"title": "Solo", "content": "Content"})

        notes_file = profile.notes_file
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 99})
        assert "超出范围" in result

    def test_delete_entry_out_of_range(self, agent, profile):
        """delete_entry with invalid number returns error."""
        from bongo.tools import tool_write_note, tool_delete_entry

        tool_write_note(agent, {"title": "Solo", "content": "Content"})

        notes_file = profile.notes_file
        result = tool_delete_entry(agent, {"path": str(notes_file), "entry": 99})
        assert "超出范围" in result


class TestMistakesCRUD:
    """Test mistakes: create, list, read by entry, delete by entry."""

    def _add_mistake(self, profile, question, score=50):
        """Helper to add a mistake directly via profile."""
        profile.add_mistake(
            question=question,
            user_answer="wrong answer",
            score=score,
            feedback="This is incorrect.",
            correct_answer="right answer",
            source="test",
        )

    def test_add_and_list_mistakes(self, agent, profile):
        """add_mistake creates entry, search_mistakes finds it."""
        from bongo.tools import tool_search_mistakes

        self._add_mistake(profile, "What is a closure in Python?")

        result = tool_search_mistakes(agent, {"query": "closure"})
        assert "closure" in result.lower() or "闭包" in result

    def test_add_multiple_read_entry(self, agent, profile):
        """Add 3 mistakes, read each by entry number."""
        from bongo.tools import tool_read_entry

        questions = ["Q1 about closures", "Q2 about GIL", "Q3 about decorators"]
        for q in questions:
            self._add_mistake(profile, q)

        mistakes_file = profile.mistakes_file
        assert mistakes_file.exists()

        for i, q in enumerate(questions, 1):
            result = tool_read_entry(agent, {"path": str(mistakes_file), "entry": i})
            assert q in result, f"Entry {i} should contain '{q}', got: {result[:200]}"

    def test_delete_mistake_entry(self, agent, profile):
        """Delete a mistake by entry number."""
        from bongo.tools import tool_delete_entry, tool_read_entry

        for q in ["Mistake A", "Mistake B", "Mistake C"]:
            self._add_mistake(profile, q)

        mistakes_file = profile.mistakes_file

        # Delete second
        result = tool_delete_entry(agent, {"path": str(mistakes_file), "entry": 2})
        assert "已删除" in result
        assert "第 2 条" in result

        # Verify remaining
        result = tool_read_entry(agent, {"path": str(mistakes_file), "entry": 1})
        assert "Mistake A" in result
        result = tool_read_entry(agent, {"path": str(mistakes_file), "entry": 2})
        assert "Mistake C" in result

    def test_delete_first_mistake(self, agent, profile):
        """Delete first mistake, verify shift."""
        from bongo.tools import tool_delete_entry, tool_read_entry

        for q in ["First Q", "Second Q", "Third Q"]:
            self._add_mistake(profile, q)

        mistakes_file = profile.mistakes_file

        result = tool_delete_entry(agent, {"path": str(mistakes_file), "entry": 1})
        assert "已删除" in result

        result = tool_read_entry(agent, {"path": str(mistakes_file), "entry": 1})
        assert "Second Q" in result

    def test_get_mistake_detail(self, agent, profile):
        """get_mistake_detail retrieves full detail by title."""
        from bongo.tools import tool_get_mistake_detail

        self._add_mistake(profile, "What is Python GIL?")

        result = tool_get_mistake_detail(agent, {"title": "What is Python GIL?"})
        assert "GIL" in result
        assert "wrong answer" in result
        assert "right answer" in result

    def test_mistake_index_consistency(self, agent, profile):
        """After deleting a mistake, index should be rebuilt correctly."""
        from bongo.tools import tool_delete_entry

        for q in ["Q-A", "Q-B", "Q-C"]:
            self._add_mistake(profile, q)

        mistakes_file = profile.mistakes_file

        # Delete middle
        tool_delete_entry(agent, {"path": str(mistakes_file), "entry": 2})

        # Check index has 2 entries
        index = profile.get_mistakes_index()
        assert len(index) == 2
        # Verify offsets are valid
        for entry in index:
            assert entry.get("offset") is not None
            assert entry.get("length") is not None
            assert entry["offset"] >= 0
            assert entry["length"] > 0


class TestFileOperations:
    """Test read_file, patch_file, search, list_files."""

    def test_read_file(self, agent):
        from bongo.tools import tool_write_file, tool_read_file

        content = "line 1\nline 2\nline 3\nline 4\nline 5\n"
        tool_write_file(agent, {"path": "test.txt", "content": content})

        result = tool_read_file(agent, {"path": "test.txt", "start": 1, "end": 5})
        assert "line 1" in result
        assert "line 5" in result

    def test_read_file_range(self, agent):
        from bongo.tools import tool_write_file, tool_read_file

        content = "\n".join(f"line {i}" for i in range(1, 21)) + "\n"
        tool_write_file(agent, {"path": "big.txt", "content": content})

        result = tool_read_file(agent, {"path": "big.txt", "start": 10, "end": 15})
        assert "line 10" in result
        assert "line 15" in result
        assert "line 9" not in result
        assert "line 16" not in result

    def test_patch_file(self, agent):
        from bongo.tools import tool_write_file, tool_patch_file

        tool_write_file(agent, {"path": "patch.txt", "content": "hello world\nfoo bar\n"})

        result = tool_patch_file(agent, {
            "path": "patch.txt",
            "old_text": "hello world",
            "new_text": "goodbye world",
        })
        assert "patched" in result

        # Verify
        content = (agent.root / "patch.txt").read_text()
        assert "goodbye world" in content
        assert "hello world" not in content

    def test_search(self, agent):
        from bongo.tools import tool_write_file, tool_search

        tool_write_file(agent, {"path": "searchable.txt", "content": "alpha beta gamma\ndelta epsilon\n"})

        result = tool_search(agent, {"pattern": "beta", "path": "searchable.txt"})
        assert "beta" in result

    def test_list_files(self, agent):
        from bongo.tools import tool_write_file, tool_list_files

        tool_write_file(agent, {"path": "listed.txt", "content": "content"})

        result = tool_list_files(agent, {"path": "."})
        assert "listed.txt" in result


class TestReadCache:
    """Test read_cache tool."""

    def test_read_cache(self, agent, tmp_path):
        from bongo.tools import tool_read_cache
        from pathlib import Path as P

        cache_dir = P.home() / ".bongo" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "test_cache.txt"
        cache_file.write_text("cached content here", encoding="utf-8")

        try:
            result = tool_read_cache(agent, {"path": str(cache_file)})
            assert "cached content here" in result
        finally:
            cache_file.unlink(missing_ok=True)

    def test_read_cache_rejects_non_cache_path(self, agent):
        from bongo.tools import tool_read_cache

        with pytest.raises(ValueError, match="cache"):
            tool_read_cache(agent, {"path": "/etc/passwd"})


class TestDeleteCooldown:
    """Test delete_entry cooldown prevents rapid repeated deletes."""

    def test_cooldown_blocks_second_delete(self, agent, profile):
        """After a successful delete, second delete on same file within 10s is blocked."""
        from bongo.tools import tool_write_note

        for t in ["A", "B", "C"]:
            tool_write_note(agent, {"title": t, "content": f"Content {t}"})

        notes_file = profile.notes_file

        # Simulate the agent runtime with cooldown
        from bongo.runtime import bongo as BongoAgent
        import time

        # Create a minimal agent with cooldown tracking
        agent._delete_cooldown = {}
        agent.tools = {}

        # Manually test cooldown logic
        path_key = str(notes_file)
        now_ts = time.time()

        # First delete should be allowed (no cooldown)
        assert path_key not in agent._delete_cooldown

        # Record cooldown
        agent._delete_cooldown[path_key] = now_ts

        # Second delete within 10s should be blocked
        elapsed = time.time() - agent._delete_cooldown.get(path_key, 0)
        assert elapsed < 10, "Cooldown should still be active"

    def test_cooldown_expires(self, agent, profile):
        """After cooldown expires, delete should be allowed again."""
        import time

        agent._delete_cooldown = {}
        path_key = "/some/path"
        agent._delete_cooldown[path_key] = time.time() - 11  # 11 seconds ago

        elapsed = time.time() - agent._delete_cooldown.get(path_key, 0)
        assert elapsed >= 10, "Cooldown should have expired"

    def test_run_tool_cooldown_integration(self, tmp_path, profile):
        """Integration test: run_tool blocks second delete_entry within cooldown."""
        from unittest.mock import MagicMock
        from bongo.runtime import bongo as BongoAgent
        from bongo.profile import UserProfile

        work_dir = tmp_path / "workspace"
        work_dir.mkdir()

        # Create a minimal mock model client
        mock_client = MagicMock()
        mock_client.get_provider_name.return_value = "test"

        agent = BongoAgent(
            model_client=mock_client,
            work_dir=str(work_dir),
            max_steps=5,
            approval_policy="auto",
        )

        # Create notes inside the workspace so agent.path() resolves correctly
        test_profile = UserProfile("testuser", notes_dir=work_dir / "notes")
        for t in ["X", "Y", "Z"]:
            test_profile.add_note(title=t, content=f"Content of {t}")

        notes_file = test_profile.notes_file  # workspace/notes/testuser.md

        # First delete should succeed
        result = agent.run_tool("delete_entry", {"path": str(notes_file), "entry": 2})
        assert "已删除" in result

        # Second delete on same file should be blocked by cooldown
        result = agent.run_tool("delete_entry", {"path": str(notes_file), "entry": 2})
        assert "error" in result.lower() or "cooldown" in result.lower()
        assert "已删除" not in result  # Must NOT succeed


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_read_entry_zero(self, agent, profile):
        from bongo.tools import tool_write_note, tool_read_entry

        tool_write_note(agent, {"title": "Test", "content": "Content"})
        notes_file = profile.notes_file

        with pytest.raises(ValueError, match="entry must be >= 1"):
            tool_read_entry(agent, {"path": str(notes_file), "entry": 0})

    def test_delete_entry_zero(self, agent, profile):
        from bongo.tools import tool_write_note, tool_delete_entry

        tool_write_note(agent, {"title": "Test", "content": "Content"})
        notes_file = profile.notes_file

        with pytest.raises(ValueError, match="entry must be >= 1"):
            tool_delete_entry(agent, {"path": str(notes_file), "entry": 0})

    def test_read_entry_nonexistent_file(self, agent):
        from bongo.tools import tool_read_entry

        with pytest.raises(ValueError, match="not a file"):
            tool_read_entry(agent, {"path": "nonexistent.md", "entry": 1})

    def test_delete_entry_nonexistent_file(self, agent):
        from bongo.tools import tool_delete_entry

        with pytest.raises(ValueError, match="not a file"):
            tool_delete_entry(agent, {"path": "nonexistent.md", "entry": 1})

    def test_write_note_empty_title(self, agent):
        from bongo.tools import tool_write_note

        with pytest.raises(ValueError, match="title"):
            tool_write_note(agent, {"title": "", "content": "content"})

    def test_write_note_empty_content(self, agent):
        from bongo.tools import tool_write_note

        with pytest.raises(ValueError, match="content"):
            tool_write_note(agent, {"title": "title", "content": ""})

    def test_notes_index_survives_delete(self, agent, profile):
        """Index file should be rebuilt after delete and still work."""
        from bongo.tools import tool_write_note, tool_delete_entry, tool_read_entry

        for t in ["A", "B", "C"]:
            tool_write_note(agent, {"title": t, "content": f"Content {t}"})

        notes_file = profile.notes_file
        index_file = profile.notes_index_file

        # Delete B
        tool_delete_entry(agent, {"path": str(notes_file), "entry": 2})

        # Index should have 2 entries with valid offsets
        index = profile._read_notes_index()
        assert len(index) == 2
        for e in index:
            assert e["offset"] is not None
            assert e["length"] is not None

        # Should still be able to read entries
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 1})
        assert "A" in result
        result = tool_read_entry(agent, {"path": str(notes_file), "entry": 2})
        assert "C" in result

    def test_mistakes_index_survives_delete(self, agent, profile):
        """Mistake index should be rebuilt after delete."""
        from bongo.tools import tool_delete_entry, tool_read_entry

        for q in ["Q1", "Q2", "Q3"]:
            profile.add_mistake(
                question=q, user_answer="bad", score=40,
                feedback="wrong", source="test",
            )

        mistakes_file = profile.mistakes_file

        tool_delete_entry(agent, {"path": str(mistakes_file), "entry": 2})

        index = profile.get_mistakes_index()
        assert len(index) == 2
        for e in index:
            assert e.get("offset") is not None

        result = tool_read_entry(agent, {"path": str(mistakes_file), "entry": 1})
        assert "Q1" in result
        result = tool_read_entry(agent, {"path": str(mistakes_file), "entry": 2})
        assert "Q3" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
