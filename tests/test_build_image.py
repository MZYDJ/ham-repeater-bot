#!/usr/bin/env python3
"""Offline tests for the edge-tts version updater used by Docker builds."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from update_edge_tts_version import parse_latest_version, update_env_file  # noqa: E402


class EdgeTTSBuildVersionTests(unittest.TestCase):
    def test_parse_latest_version(self):
        output = (
            "Looking in indexes: https://mirrors.aliyun.com/pypi/simple/\n"
            "edge-tts (7.3.1)\n"
            "Available versions: 7.3.1, 7.3.0, 7.2.8\n"
        )
        self.assertEqual(parse_latest_version(output), "7.3.1")

    def test_parse_fails_closed_when_index_output_is_unexpected(self):
        with self.assertRaises(RuntimeError):
            parse_latest_version("ERROR: Could not fetch package index")

    def test_update_existing_env_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("TZ=Asia/Shanghai\nEDGE_TTS_VERSION=7.2.8\nOTHER=value\n", encoding="utf-8")
            update_env_file(env, "7.3.1")
            self.assertEqual(
                env.read_text(encoding="utf-8"),
                "TZ=Asia/Shanghai\nEDGE_TTS_VERSION=7.3.1\nOTHER=value\n",
            )

    def test_update_env_collapses_duplicate_version_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("EDGE_TTS_VERSION=7.2.8\nOTHER=value\nEDGE_TTS_VERSION=7.2.9\n", encoding="utf-8")
            update_env_file(env, "7.3.1")
            self.assertEqual(
                env.read_text(encoding="utf-8"),
                "EDGE_TTS_VERSION=7.3.1\nOTHER=value\n",
            )

    def test_update_creates_env_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            update_env_file(env, "7.3.1")
            self.assertEqual(env.read_text(encoding="utf-8"), "EDGE_TTS_VERSION=7.3.1\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
