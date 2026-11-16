from __future__ import annotations

import codecs
import tempfile
import unittest
from pathlib import Path

from dagrunner.logger import TaskLogManager, decode_bytes
from dagrunner.webapp import _prepare_import_preview


WINDOWS_TASK_XML = """\
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\Reports\\DailyReport</URI>
    <Description>每日数据报表</Description>
    <Author>管理员</Author>
  </RegistrationInfo>
  <Settings>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions>
    <Exec>
      <Command>C:\\Tools\\report.exe</Command>
      <Arguments>--daily</Arguments>
      <WorkingDirectory>C:\\Tools</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class DecodeBytesTests(unittest.TestCase):
    def test_decodes_utf8_and_bom_variants(self) -> None:
        text = "中文日志 hello\n第二行 output\n"
        samples = [
            text.encode("utf-8"),
            codecs.BOM_UTF8 + text.encode("utf-8"),
            codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
            codecs.BOM_UTF16_BE + text.encode("utf-16-be"),
        ]
        for raw in samples:
            self.assertIn("中文日志", decode_bytes(raw))

    def test_decodes_utf16_without_bom(self) -> None:
        text = "中文日志 hello\n第二行 output\n"
        for encoding in ("utf-16-le", "utf-16-be"):
            decoded = decode_bytes(text.encode(encoding))
            self.assertIn("中文日志", decoded)
            self.assertNotIn("\x00", decoded)

    def test_decodes_gbk_on_any_platform(self) -> None:
        raw = "windows - 清洗转换数据\r\n无法识别的名称\r\n".encode("gbk")
        decoded = decode_bytes(raw)
        self.assertIn("清洗转换数据", decoded)

    def test_keeps_plain_ascii_and_json_intact(self) -> None:
        raw = b'{"processDefinition": {"name": "daily report"}}\n'
        self.assertEqual(decode_bytes(raw, strict=True), raw.decode("utf-8"))

    def test_strict_rejects_binary(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            decode_bytes(bytes(range(256)) * 4, strict=True)

    def test_task_log_manager_reads_each_encoding(self) -> None:
        text = "中文日志 hello\n第二行 output\n"
        for raw in (
            text.encode("utf-8"),
            codecs.BOM_UTF16_LE + text.encode("utf-16-le"),
            text.encode("utf-16-be"),
            text.encode("gbk"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "sample.log"
                path.write_bytes(raw)
                lines = TaskLogManager.read_lines(path)
            self.assertEqual(lines[0], "中文日志 hello")


class WindowsTaskImportEncodingTests(unittest.TestCase):
    def test_import_preview_accepts_utf16_xml_with_bom(self) -> None:
        preview = _prepare_import_preview(
            "daily-report.xml",
            codecs.BOM_UTF16_LE + WINDOWS_TASK_XML.encode("utf-16-le"),
        )
        self.assertEqual(preview["source"], "windows-task-scheduler")
        self.assertIn("每日数据报表", str(preview["definition"]))

    def test_import_preview_accepts_utf16_xml_without_bom(self) -> None:
        preview = _prepare_import_preview(
            "daily-report.xml",
            WINDOWS_TASK_XML.encode("utf-16-be"),
        )
        self.assertEqual(preview["source"], "windows-task-scheduler")
        self.assertIn("每日数据报表", str(preview["definition"]))

    def test_import_preview_still_accepts_utf8_xml_string(self) -> None:
        preview = _prepare_import_preview("daily-report.xml", WINDOWS_TASK_XML)
        self.assertEqual(preview["source"], "windows-task-scheduler")

    def test_import_preview_rejects_binary(self) -> None:
        with self.assertRaises(UnicodeDecodeError):
            _prepare_import_preview("broken.xml", bytes(range(256)) * 4)


if __name__ == "__main__":
    unittest.main()
