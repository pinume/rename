import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from rename import main
from rename.operations import classify, export_markdown, rename_by_number, summarize


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "资料"
        self.root.mkdir()
        self.excel = self.base / "台账.xlsx"

    def workbook(self, rows, headers=None):
        book = Workbook()
        sheet = book.active
        sheet.title = "家电台账"
        sheet.append(["标题"])
        sheet.append(headers or ["交易参考号", "品牌品类", "序号"])
        for row in rows:
            sheet.append(row)
        book.save(self.excel)
        book.close()

    def folder(self, name):
        folder = self.root / name
        folder.mkdir(parents=True)
        (folder / "发票.jpg").write_bytes(b"invoice")
        (folder / "SN图片.png").write_bytes(b"serial")
        return folder

    def test_classify_summary_and_recursive_rename_preserve_contents(self):
        self.workbook([["T1", "海尔电冰箱", "1-01"], ["T2", "海尔电冰箱", "1-02"]])
        self.folder("T1")
        self.folder("T2")
        original = {p.relative_to(self.root).as_posix(): p.read_bytes()
                    for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(classify(self.root, self.excel).success, 2)
        self.assertEqual(summarize(self.root).success, 2)
        self.assertTrue((self.root / "海尔电冰箱").is_dir())
        self.assertEqual(summarize(self.root).success, 0)
        self.assertEqual(rename_by_number(self.root, self.excel).success, 2)
        for path, data in original.items():
            reference, filename = path.split("/")
            number = {"T1": "1-01", "T2": "1-02"}[reference]
            self.assertEqual((self.root / "汇总" / number / filename).read_bytes(), data)

    def test_unmatched_empty_conflicting_and_unsafe_values_are_preserved(self):
        self.workbook([["empty", None, ""], ["conflict", "甲", "1"],
                       ["conflict", "乙", "2"], ["unsafe", "../外部", "../外部"]])
        for name in ("empty", "conflict", "unsafe", "missing"):
            self.folder(name)
        for operation in (classify, rename_by_number):
            report = operation(self.root, self.excel)
            self.assertEqual(report.success, 0 if operation is classify else 1)
            self.assertEqual(len(report.skipped), 4 if operation is classify else 3)
        self.assertEqual(len(list(self.root.iterdir())), 4)
        self.assertTrue((self.root / "conflict（序号异常）/发票.jpg").is_file())

    def test_multiple_numbers_are_marked_and_existing_marks_are_preserved(self):
        self.workbook([["T1", "品牌", "1-01"], ["T1", "品牌", "1-02"],
                       ["T2", "品牌", "2-01"], ["T2", "品牌", "2-01"]])
        self.folder("品牌/T1")
        self.folder("品牌/T2")
        report = rename_by_number(self.root, self.excel)
        self.assertEqual(report.success, 2)
        self.assertEqual((self.root / "品牌/T1（序号异常）/发票.jpg").read_bytes(), b"invoice")
        self.assertTrue((self.root / "品牌/2-01").is_dir())
        self.folder("品牌/T1")
        report = rename_by_number(self.root, self.excel)
        self.assertEqual(report.success, 0)
        self.assertTrue((self.root / "品牌/T1").is_dir())
        self.assertTrue(any("目标已存在" in message for message in report.skipped))

    def test_duplicate_targets_and_existing_targets_are_not_merged(self):
        self.workbook([["T1", "品牌", "1-01"], ["T2", "品牌", "1-01"]])
        self.folder("T1")
        self.folder("T2")
        report = rename_by_number(self.root, self.excel)
        self.assertEqual(len(report.skipped), 2)
        self.assertEqual(report.success, 0)
        self.workbook([["T1", "品牌", "1-01"]])
        self.folder("1-01")
        report = rename_by_number(self.root, self.excel)
        self.assertEqual(report.success, 0)
        self.assertEqual((self.root / "T1/发票.jpg").read_bytes(), b"invoice")
        self.folder("品牌/T1")
        self.assertEqual(classify(self.root, self.excel).success, 0)

    def test_summary_skips_all_duplicate_leaf_names(self):
        self.folder("品牌甲/T1")
        self.folder("品牌乙/T1")
        self.folder("汇总/T2")
        report = summarize(self.root)
        self.assertEqual(report.success, 0)
        self.assertEqual(len(report.skipped), 2)
        self.assertTrue((self.root / "品牌甲/T1").is_dir())
        self.assertFalse((self.root / "汇总/汇总").exists())

    def test_markdown_filters_root_files_and_system_files_and_confirms_overwrite(self):
        self.folder("品牌/T1")
        (self.root / "下载清单.csv").write_text("root file")
        (self.root / "品牌/T1/.DS_Store").write_bytes(b"metadata")
        output = export_markdown(self.root)
        expected = ("```text\n└── 品牌/\n    └── T1/\n"
                    "        ├── SN图片.png\n        └── 发票.jpg\n```\n")
        self.assertEqual(output.read_text(), expected)
        with self.assertRaises(FileExistsError):
            export_markdown(self.root)
        self.assertEqual(export_markdown(self.root, overwrite=True).read_text(), expected)

    def test_missing_headers_and_invalid_root_do_not_move_files(self):
        self.folder("T1")
        self.workbook([["T1"]], headers=["错误表头"])
        with self.assertRaises(ValueError):
            classify(self.root, self.excel)
        with self.assertRaises(FileNotFoundError):
            summarize(self.root / "不存在")
        self.assertTrue((self.root / "T1").is_dir())

    def test_classify_finds_headers_in_arbitrary_sheets_and_rows(self):
        self.excel = self.base / "任意文件名.xlsx"
        book = Workbook()
        book.active.title = "说明"
        book.active.append(["不含台账"])
        sheet = book.create_sheet("销售记录")
        sheet.append(["标题"])
        sheet.append([])
        sheet.append([" 品牌品类 ", "其他", "交易参考号"])
        sheet.append(["品牌甲", "备注", "T1"])
        sheet = book.create_sheet("补充资料")
        sheet.append(["交易参考号", "品牌品类"])
        sheet.append(["T2", "品牌乙"])
        sheet.append(["T3", "品牌甲"])
        sheet = book.create_sheet("冲突记录")
        sheet.append(["品牌品类", "交易参考号"])
        sheet.append(["品牌乙", "T3"])
        book.save(self.excel)
        book.close()
        for name in ("T1", "T2", "T3"):
            self.folder(name)
        report = classify(self.root, self.excel)
        self.assertEqual(report.success, 2)
        self.assertEqual(len(report.skipped), 1)
        self.assertTrue((self.root / "品牌甲/T1/发票.jpg").is_file())
        self.assertTrue((self.root / "品牌乙/T2/发票.jpg").is_file())
        self.assertTrue((self.root / "T3/发票.jpg").is_file())

    def test_symlinks_are_not_followed_or_used_as_output_targets(self):
        self.workbook([["T1", "品牌", "1-01"]])
        self.folder("T1")
        outside = self.base / "外部"
        outside.mkdir()
        (self.root / "品牌").symlink_to(outside, target_is_directory=True)
        self.assertEqual(classify(self.root, self.excel).success, 0)
        self.assertEqual(list(outside.iterdir()), [])
        export_markdown(self.root)
        (self.root / "目录结构.md").unlink()
        sentinel = outside / "保留.md"
        sentinel.write_text("保留")
        (self.root / "目录结构.md").symlink_to(sentinel)
        with self.assertRaises(ValueError):
            export_markdown(self.root, overwrite=True)
        self.assertEqual(sentinel.read_text(), "保留")

    def test_move_failure_is_reported(self):
        self.workbook([["T1", "品牌", "1-01"]])
        self.folder("T1")
        with patch.object(Path, "rename", side_effect=PermissionError("无权限")):
            report = classify(self.root, self.excel)
        self.assertEqual(len(report.failed), 1)
        self.assertTrue((self.root / "T1").exists())

    def test_cli_invalid_choice_export_overwrite_and_exit(self):
        with patch("builtins.input", side_effect=["invalid", "2", str(self.root), "0"]), \
                patch("builtins.print") as output:
            main()
        self.assertTrue((self.root / "目录结构.md").is_file())
        self.assertTrue(any("无效选项" in str(call) for call in output.call_args_list))
        with patch("builtins.input", side_effect=["2", str(self.root), "n", "0"]), \
                patch("builtins.print") as output:
            main()
        output.assert_any_call("已取消导出。")

    def test_cli_error_and_interrupt_handling(self):
        with patch("builtins.input", side_effect=["3", "", "3", str(self.root / "不存在"), "0"]), \
                patch("builtins.print") as output:
            main()
        self.assertEqual(sum("无法执行" in str(call) for call in output.call_args_list), 2)
        for exception in (EOFError, KeyboardInterrupt):
            with patch("builtins.input", side_effect=exception), patch("builtins.print") as output:
                main()
            output.assert_any_call("\n已退出。")

    def test_cli_classify_rename_and_summary_are_separate_choices(self):
        self.workbook([["T1", "品牌", "1-01"]])
        self.folder("T1")
        answers = ["1", str(self.root), str(self.excel),
                   "4", str(self.root), str(self.excel), "3", str(self.root), "0"]
        with patch("builtins.input", side_effect=answers), patch("builtins.print"):
            main()
        self.assertEqual((self.root / "汇总/1-01/发票.jpg").read_bytes(), b"invoice")

    def test_cli_corrupt_excel_returns_to_menu(self):
        self.excel.write_bytes(b"not an xlsx file")
        answers = ["1", str(self.root), str(self.excel), "0"]
        with patch("builtins.input", side_effect=answers), patch("builtins.print") as output:
            main()
        self.assertTrue(any("无法执行" in str(call) for call in output.call_args_list))


if __name__ == "__main__":
    unittest.main()
