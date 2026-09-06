"""Run an isolated, no-key local cluster for UI review. Ctrl+C stops it."""
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.config import TARGET_LANGUAGES


def main():
    env = {**os.environ, "POLYGLOT_BROKER_1_PORT": "17601", "POLYGLOT_BROKER_2_PORT": "17602"}
    commands = [["broker.py", "--id", str(i)] for i in (1, 2)]
    commands += [["translator_worker.py", "--lang", lang, "--mock"] for lang in [*TARGET_LANGUAGES, "en"]]
    commands += [["web_ui.py", "--port", "18080"]]
    children = []
    try:
        for command in commands:
            children.append(subprocess.Popen([sys.executable, *command], cwd=ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        print("Mock preview: http://localhost:18080 — Ctrl+C stops this isolated cluster", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            child.wait(timeout=5)


if __name__ == "__main__":
    main()
