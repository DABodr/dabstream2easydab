from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dabstream2easydab.toolchain import ToolOverrideConfig, Toolchain


class ToolchainTests(unittest.TestCase):
    def test_override_path_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_path = Path(tmpdir) / "edi2eti"
            tool_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool_path.chmod(0o755)

            toolchain = Toolchain.discover(
                ToolOverrideConfig(edi2eti_path=str(tool_path))
            )

            self.assertTrue(toolchain.edi2eti.available)
            self.assertEqual(toolchain.edi2eti.path, str(tool_path))
            self.assertEqual(toolchain.edi2eti.source, "override")

    def test_env_override_is_used_when_no_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tool_path = Path(tmpdir) / "odr-edi2edi"
            tool_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            tool_path.chmod(0o755)

            with patch.dict(
                os.environ,
                {"DABSTREAM_ODR_EDI2EDI": str(tool_path)},
                clear=False,
            ):
                toolchain = Toolchain.discover()

            self.assertTrue(toolchain.odr_edi2edi.available)
            self.assertEqual(toolchain.odr_edi2edi.path, str(tool_path))
            self.assertEqual(toolchain.odr_edi2edi.source, "env:DABSTREAM_ODR_EDI2EDI")


if __name__ == "__main__":
    unittest.main()
