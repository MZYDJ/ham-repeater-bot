#!/usr/bin/env python3
"""Query newest stable edge-tts and update the local Compose env file."""

from __future__ import annotations

import datetime
import re
import subprocess
import sys
from pathlib import Path

INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"
VERSION_LINE = re.compile(r"(?im)^edge-tts\s+\(([^)]+)\)\s*$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+_-]*$")
ENV_LINE = re.compile(r"^[ \t]*EDGE_TTS_VERSION[ \t]*=.*$")


def parse_latest_version(output: str) -> str:
    """Parse the latest stable version reported by pip index versions."""
    match = VERSION_LINE.search(output)
    if not match:
        raise RuntimeError(
            "无法从 PyPI 镜像响应中识别 edge-tts 最新版本；为避免使用陈旧版本，已停止。"
        )
    version = match.group(1).strip()
    if not SAFE_VERSION.fullmatch(version):
        raise RuntimeError(f"PyPI 返回了不安全或无效的版本字符串：{version!r}")
    return version


def query_latest_version() -> str:
    command = [
        sys.executable,
        "-m",
        "pip",
        "index",
        "versions",
        "--index-url",
        INDEX_URL,
        "edge-tts",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"无法启动 pip 查询 edge-tts 版本：{exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "查询 edge-tts 最新版本失败；没有修改 .env，也不会回退到旧版本。"
            + (f"\n{detail}" if detail else "")
        )
    return parse_latest_version(result.stdout + "\n" + result.stderr)


def update_env_file(env_path: Path, version: str) -> None:
    """Set EDGE_TTS_VERSION while preserving unrelated .env settings."""
    if not SAFE_VERSION.fullmatch(version):
        raise ValueError(f"无效的 edge-tts 版本字符串：{version!r}")
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    updated: list[str] = []
    replaced = False
    for line in lines:
        if ENV_LINE.match(line):
            if not replaced:
                updated.append(f"EDGE_TTS_VERSION={version}")
                replaced = True
        else:
            updated.append(line)
    if not replaced:
        if updated and updated[-1] != "":
            updated.append("")
        updated.append(f"EDGE_TTS_VERSION={version}")
    env_path.write_text("\n".join(updated).rstrip("\n") + "\n", encoding="utf-8")


def update_env_values(env_path: Path, values: dict[str, str]) -> None:
    """Update selected .env keys while preserving unrelated local settings."""
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    lines = existing.splitlines()
    updated: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if key not in values:
            updated.append(line)
        elif key not in seen:
            updated.append(f"{key}={values[key]}")
            seen.add(key)
    for key, value in values.items():
        if key not in seen:
            if updated and updated[-1] != "":
                updated.append("")
            updated.append(f"{key}={value}")
    env_path.write_text("\n".join(updated).rstrip("\n") + "\n", encoding="utf-8")

def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env_path = root / ".env"
    try:
        version = query_latest_version()
        update_env_file(env_path, version)
        try:
            git_result = subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            git_commit = git_result.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            # NAS 上以文件上传而非 git clone 部署时，提交号不可可靠推断。
            git_commit = "unknown"
        build_time = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        update_env_values(
            env_path,
            {"BUILD_TIME": build_time, "GIT_COMMIT": git_commit},
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(f"[OK] edge-tts 最新稳定版：{version}")
    print(f"[OK] 构建时间：{build_time}")
    print(f"[OK] Git 提交：{git_commit}")
    print(f"[OK] 已更新 {env_path}（该文件为本地配置，不会提交到 Git）")
    print("[NEXT] 现在可以在飞牛 OS Compose 界面构建，或运行 ./scripts/build-image.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
