"""启动Bongo Web界面"""
import subprocess
import sys
import webbrowser
import time
from pathlib import Path
import os


def main():
    print("🚀 Starting Bongo Web Interface...")

    # 设置环境变量
    os.environ.setdefault("BONGO_WORKSPACE", ".")
    os.environ.setdefault("BONGO_PROVIDER", "openai")
    os.environ.setdefault("BONGO_MODEL", "qwen3.5-plus-2026-02-15")
    os.environ.setdefault("BONGO_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    # 检查API密钥
    if not os.environ.get("BONGO_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        print("⚠️  Warning: No API key found. Please set BONGO_API_KEY or OPENAI_API_KEY environment variable.")
        print("   Example: set BONGO_API_KEY=your-api-key")
        sys.exit(1)

    # 启动Flask服务器
    backend_script = "D:\\aiAgentStudy\\bongo-main\\bongo\\web\\server.py"
    print(f"📡 Starting backend server on http://localhost:5000")

    server_process = subprocess.Popen(
        [sys.executable, str(backend_script)],
        cwd=str(Path(__file__).parent.parent)
    )

    # 等待服务器启动
    time.sleep(2)

    # 打开浏览器
    frontend_file = Path(__file__).parent.parent / "bongo" / "web" / "index.html"
    if not frontend_file.exists():
        print(f"⚠️  Warning: Frontend file not found at {frontend_file}")
        print(f"   Please make sure index.html exists in bongo/web/ directory")
        frontend_url = "http://localhost:5000"
    else:
        frontend_url = f"file:///{frontend_file.absolute()}"

    print(f"🌐 Opening web interface...")
    webbrowser.open(frontend_url)

    print("\n✅ Bongo Web Interface is running!")
    print(f"   Backend: http://localhost:5000")
    print(f"   Frontend: {frontend_url}")
    print("\nPress Ctrl+C to stop the server.\n")

    try:
        server_process.wait()
    except KeyboardInterrupt:
        print("\n🛑 Stopping server...")
        server_process.terminate()
        server_process.wait()
        print("✅ Server stopped.")


if __name__ == "__main__":
    main()
