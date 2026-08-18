"""End-to-end test: verify tools work with real LLM."""
import shutil, tempfile, sys
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix='bongo_e2e_'))
shutil.copytree('CC', tmp / 'CC')
print(f'Workspace: {tmp}')

from bongo.cli import build_agent, build_arg_parser

args = build_arg_parser().parse_args([
    '--cwd', str(tmp),
    '--approval', 'auto',
    '--max-steps', '10',
])
agent = build_agent(args)

passed = 0
failed = 0

def run_test(label, question, max_rounds=5):
    global passed, failed
    print(f'\n{"="*60}')
    print(f'TEST: {label}')
    agent.session['history'] = []
    agent.session['memory'] = {}
    try:
        result = agent.ask(question)
        history = agent.session.get('history', [])
        tool_calls = []
        for msg in history:
            if msg.get('role') == 'assistant' and isinstance(msg.get('content'), list):
                for block in msg['content']:
                    if isinstance(block, dict) and block.get('type') == 'tool_use':
                        tool_calls.append(block.get('name'))
        rounds = len([m for m in history if m.get('role') == 'assistant'])
        print(f'  Rounds: {rounds}, Tools: {tool_calls}')
        if rounds <= max_rounds:
            print(f'  PASS (<= {max_rounds} rounds)')
            passed += 1
        else:
            print(f'  FAIL (expected <= {max_rounds} rounds, got {rounds})')
            failed += 1
    except Exception as e:
        print(f'  ERROR: {e}')
        failed += 1

# Test 1: append — should be 1-2 rounds
run_test('append', '在 CC/reference.md 最后加上一行: test_append_123', max_rounds=5)

# Test 2: delete line — should be 1-3 rounds
run_test('delete line', '删掉 CC/reference.md 的第一行', max_rounds=9)

# Test 3: read tail — should be 1-2 rounds
run_test('read tail', '读取 CC/01-overview.md 最后5行', max_rounds=2)

# Test 4: file info — should be 1-2 rounds
run_test('file info', 'CC/02-agent-loop.md 有多少行', max_rounds=2)

# Test 5: grep — should be 1-2 rounds
run_test('grep', '在 CC/01-overview.md 中搜索包含 TODO 的行', max_rounds=4)

# Test 6: insert — should be 1-3 rounds
run_test('insert', '在 CC/reference.md 第1行前面插入: <!-- test -->', max_rounds=4)

# Test 7: patch — should be 2-3 rounds
run_test('patch', '把 CC/reference.md 中的 test_append_123 改为 patched_ok', max_rounds=6)

# Test 8: write new file — should be 1-2 rounds
run_test('write new', '创建文件 CC/_test_new.md 写入 hello world', max_rounds=4)

# Test 9: delete file — should be 1-2 rounds
run_test('delete file', '删除文件 CC/_test_new.md', max_rounds=5)

shutil.rmtree(tmp)
print(f'\n{"="*60}')
print(f'Results: {passed} passed, {failed} failed')
sys.exit(1 if failed > 0 else 0)
