"""Run a real server and CLI snapshot/restore; use only a disposable directory."""

from pathlib import Path
import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import homelab  # noqa: E402 — import the checked repository module from this script.

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "homelab.py"),
            "serve",
            "--data",
            str(root / "data"),
            "--port",
            str(port),
            "--interval",
            "1",
        ],
        stdout=subprocess.DEVNULL,
    )
    try:
        for attempt in range(100):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/healthz", timeout=1
                ) as response:
                    assert json.load(response)["status"] == "ok"
                break
            except (OSError, ValueError):
                time.sleep(0.1)
        else:
            raise RuntimeError("Real monitor did not become healthy.")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/history") as response:
            result = json.load(response)
            assert result["samples"][-1]["disk_total_bytes"] > 0
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "homelab.py"),
                "backup",
                "--data",
                str(root / "data"),
                "--out",
                str(root / "backup"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "homelab.py"),
                "restore",
                "--backup",
                str(root / "backup"),
                "--data",
                str(root / "restored"),
            ],
            check=True,
        )
        assert homelab.history(root / "restored")
        synthetic = subprocess.check_output(
            [
                sys.executable,
                str(ROOT / "homelab.py"),
                "demo",
                "--out",
                str(root / "demo"),
            ],
            text=True,
        )
        assert synthetic == (ROOT / "examples/synthetic-recovery.json").read_text()
        print(
            "Actual HTTP health/history, CLI snapshot/restore and deterministic synthetic fixture passed."
        )
    finally:
        process.terminate()
        process.wait(timeout=5)
