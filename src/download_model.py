"""下载 faster-whisper 语音识别模型（使用国内镜像加速）。

运行一次即可，下载完成后 gui.py 会自动使用本地模型。
模型大小约 500 MB，存放于 ~/.cache/whisper_direct/small/
"""

import os
import sys
import shutil

import requests

MODEL_ID = "Systran/faster-whisper-small"
SAVE_DIR = os.path.expanduser("~/.cache/whisper_direct/small")
MIRROR = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

FILES = [
    "config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.txt",
]


def download_file(filename: str) -> bool:
    """从 hf-mirror 下载单个模型文件。"""
    url = f"{MIRROR}/{MODEL_ID}/raw/main/{filename}"
    path = os.path.join(SAVE_DIR, filename)

    print(f"  {filename} ...", end=" ", flush=True)
    try:
        r = requests.get(url, stream=True, timeout=120)
        r.raise_for_status()
        with open(path, "wb") as f:
            shutil.copyfileobj(r.raw, f)
        size = os.path.getsize(path)
        print(f"OK ({size / 1024 / 1024:.1f} MB)")
        return True
    except Exception as e:
        print(f"FAILED: {e}")
        return False


def main() -> None:
    os.makedirs(SAVE_DIR, exist_ok=True)

    print(f"下载 faster-whisper-small 模型")
    print(f"镜像源: {MIRROR}")
    print(f"保存到: {SAVE_DIR}")
    print(f"文件数: {len(FILES)}\n")

    success = True
    for fname in FILES:
        if not download_file(fname):
            success = False

    if success:
        print(f"\n✅ 模型下载完成！现在可以直接运行 gui.py 或调用转写功能。")
    else:
        print(f"\n❌ 部分文件下载失败，请检查网络后重试。")
        sys.exit(1)


if __name__ == "__main__":
    main()
