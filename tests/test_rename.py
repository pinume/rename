import csv
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rename import main
from rename.operations import (classify, classify_by_batch, export_folder_names,
                               export_missing_materials,
                               rename_by_number, rename_pos_images, summarize)


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "资料"
        self.root.mkdir()
        self.csv_path = self.base / "台账.csv"

    def write_csv(self, rows, headers=None, encoding="utf-8-sig"):
        with self.csv_path.open("w", encoding=encoding, newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["标题"])
            writer.writerow(headers or ["交易参考号", "品牌品类", "序号"])
            writer.writerows(rows)

    def folder(self, name):
        folder = self.root / name
        folder.mkdir(parents=True)
        (folder / "发票.jpg").write_bytes(b"invoice")
        (folder / "SN图片.png").write_bytes(b"serial")
        return folder

    def test_classify_summary_and_recursive_rename_preserve_contents(self):
        self.write_csv([["T1", "海尔电冰箱", "1-01"], ["T2", "海尔电冰箱", "1-02"]])
        self.folder("T1")
        self.folder("T2")
        original = {p.relative_to(self.root).as_posix(): p.read_bytes()
                    for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(classify(self.root, self.csv_path).success, 2)
        self.assertEqual(summarize(self.root).success, 2)
        self.assertTrue((self.root / "海尔电冰箱").is_dir())
        self.assertEqual(summarize(self.root).success, 0)
        self.assertEqual(rename_by_number(self.root, self.csv_path).success, 2)
        for path, data in original.items():
            reference, filename = path.split("/")
            number = {"T1": "1-01", "T2": "1-02"}[reference]
            self.assertEqual((self.root / "汇总" / number / filename).read_bytes(), data)

    def test_unmatched_empty_conflicting_and_unsafe_values_are_preserved(self):
        self.write_csv([["empty", None, ""], ["conflict", "甲", "1"],
                       ["conflict", "乙", "2"], ["unsafe", "../外部", "../外部"]])
        for name in ("empty", "conflict", "unsafe", "missing"):
            self.folder(name)
        for operation in (classify, rename_by_number):
            report = operation(self.root, self.csv_path)
            self.assertEqual(report.success, 0 if operation is classify else 1)
            self.assertEqual(len(report.skipped), 4 if operation is classify else 3)
        self.assertEqual(len(list(self.root.iterdir())), 4)
        self.assertTrue((self.root / "conflict（序号异常）/发票.jpg").is_file())

    def test_multiple_numbers_are_marked_and_existing_marks_are_preserved(self):
        self.write_csv([["T1", "品牌", "1-01"], ["T1", "品牌", "1-02"],
                       ["T2", "品牌", "2-01"], ["T2", "品牌", "2-01"]])
        self.folder("品牌/T1")
        self.folder("品牌/T2")
        report = rename_by_number(self.root, self.csv_path)
        self.assertEqual(report.success, 2)
        self.assertEqual((self.root / "品牌/T1（序号异常）/发票.jpg").read_bytes(), b"invoice")
        self.assertTrue((self.root / "品牌/2-01").is_dir())
        self.folder("品牌/T1")
        report = rename_by_number(self.root, self.csv_path)
        self.assertEqual(report.success, 0)
        self.assertTrue((self.root / "品牌/T1").is_dir())
        self.assertTrue(any("目标已存在" in message for message in report.skipped))

    def test_duplicate_targets_and_existing_targets_are_not_merged(self):
        self.write_csv([["T1", "品牌", "1-01"], ["T2", "品牌", "1-01"]])
        self.folder("T1")
        self.folder("T2")
        report = rename_by_number(self.root, self.csv_path)
        self.assertEqual(len(report.skipped), 2)
        self.assertEqual(report.success, 0)
        self.write_csv([["T1", "品牌", "1-01"]])
        self.folder("1-01")
        report = rename_by_number(self.root, self.csv_path)
        self.assertEqual(report.success, 0)
        self.assertEqual((self.root / "T1/发票.jpg").read_bytes(), b"invoice")
        self.folder("品牌/T1")
        self.assertEqual(classify(self.root, self.csv_path).success, 0)

    def test_summary_skips_all_duplicate_leaf_names(self):
        self.folder("品牌甲/T1")
        self.folder("品牌乙/T1")
        self.folder("汇总/T2")
        report = summarize(self.root)
        self.assertEqual(report.success, 0)
        self.assertEqual(len(report.skipped), 2)
        self.assertTrue((self.root / "品牌甲/T1").is_dir())
        self.assertFalse((self.root / "汇总/汇总").exists())

    def test_folder_names_csv_lists_first_level_folders_only(self):
        names = ["乙 品牌", "a,b", 'say "hi"', "多\n行", ".隐藏", "甲"]
        for name in names:
            (self.root / name / "深层").mkdir(parents=True)
        (self.root / "普通.txt").write_text("file")
        outside = self.base / "外部"
        outside.mkdir()
        (self.root / "链接").symlink_to(outside, target_is_directory=True)
        output = export_folder_names(self.root)
        self.assertEqual(output, self.root.resolve() / "子文件夹名称.csv")
        self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
        with output.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows, [["文件夹名称"]] + [[name] for name in sorted(names)])
        original = output.read_bytes()
        with self.assertRaises(FileExistsError):
            export_folder_names(self.root)
        self.assertEqual(output.read_bytes(), original)
        (self.root / "新增").mkdir()
        export_folder_names(self.root, overwrite=True)
        with output.open(encoding="utf-8-sig", newline="") as stream:
            self.assertIn(["新增"], list(csv.reader(stream)))

    def test_folder_names_csv_empty_symlink_output_and_invalid_root(self):
        output = export_folder_names(self.root)
        self.assertEqual(output.read_bytes().decode("utf-8-sig"), "文件夹名称\r\n")
        output.unlink()
        sentinel = self.base / "保留.csv"
        sentinel.write_text("保留")
        output.symlink_to(sentinel)
        with self.assertRaises(ValueError):
            export_folder_names(self.root, overwrite=True)
        self.assertEqual(sentinel.read_text(), "保留")
        with self.assertRaises(FileNotFoundError):
            export_folder_names(self.root / "不存在")
        with self.assertRaises(ValueError):
            export_folder_names(sentinel)

    def test_cli_export_folder_names_confirm_and_cancel(self):
        (self.root / "T1").mkdir()
        output = self.root / "子文件夹名称.csv"
        with patch("builtins.input", side_effect=["6", str(self.root), "", "0"]), patch("builtins.print"):
            main()
        self.assertEqual(output.read_bytes().decode("utf-8-sig"), "文件夹名称\r\nT1\r\n")
        (self.root / "T2").mkdir()
        with patch("builtins.input", side_effect=["6", str(self.root), "n", "0"]), \
                patch("builtins.print") as printed:
            main()
        printed.assert_any_call("已取消导出。")
        self.assertNotIn("T2", output.read_text(encoding="utf-8-sig"))
        with patch("builtins.input", side_effect=["6", str(self.root), " Y ", "", "0"]), \
                patch("builtins.print") as printed:
            main()
        self.assertIn("T2", output.read_text(encoding="utf-8-sig"))
        self.assertTrue(any("已保存" in str(call) for call in printed.call_args_list))

    def materials(self, name, *files):
        folder = self.root / name
        folder.mkdir()
        for file in files:
            (folder / file).parent.mkdir(parents=True, exist_ok=True)
            (folder / file).write_bytes(b"x")
        return folder

    def check(self, rows, column="交易参考号", headers=("交易参考号", "序号", "备注")):
        self.write_csv(rows, headers=list(headers))
        output = export_missing_materials(self.root, self.csv_path, column, overwrite=True)
        with output.open(encoding="utf-8-sig", newline="") as stream:
            return list(csv.reader(stream))

    def test_missing_materials_image_rules(self):
        first, second = ["发票.jpg", "POS小票.png", "SN图片.JPG"], ["二合一照片.jpg", "发票.jpg", "IMEI.heic", "POS小票.jpg"]
        self.materials("第一套", *first, "POS小票.jpeg", "说明.txt", ".隐藏.jpg", "子/其他.jpg")
        self.materials("第二套", *second)
        self.materials("只有两张", "发票.jpg", "POS小票.jpg")
        self.materials("混用", *first, "IMEI.jpg")
        self.materials("多出", *first, "其他照片.jpg")
        self.materials("近似", "发票.jpg", "POS小票_2.jpg", "SN图片.jpg")
        self.materials("缺IMEI", "二合一照片.jpg", "发票.jpg", "POS小票.jpg")
        self.materials("空")
        self.materials("非图片", "发票.pdf")
        深层 = self.materials("深层", "发票.jpg", "SN图片.jpg", "子/POS小票.jpg")
        (深层.parent / "外部.jpg").write_bytes(b"x")
        链接 = self.materials("链接", "发票.jpg", "SN图片.jpg")
        (链接 / "POS小票.jpg").symlink_to(深层.parent / "外部.jpg")
        names = ["不存在", "第一套", "第二套", "只有两张", "混用", "多出", "近似", "空", "非图片", "深层", "链接", "缺IMEI"]
        rows = self.check([[n, str(i), ""] for i, n in enumerate(names)])
        missing = "第一套：缺少 POS小票、SN图片、发票"
        two = "第一套：缺少 SN图片"
        lack_pos = "第一套：缺少 POS小票"
        self.assertEqual(rows, [["交易参考号", "序号", "备注", "缺少原因"],
                                ["不存在", "0", "", "缺少文件夹"],
                                ["只有两张", "3", "", two],
                                ["混用", "4", "", "第二套：缺少 二合一照片，多出 SN图片"],
                                ["多出", "5", "", "第一套：多出 其他照片"],
                                ["近似", "6", "", "第一套：缺少 POS小票，多出 POS小票_2"],
                                ["空", "7", "", missing], ["非图片", "8", "", missing],
                                ["深层", "9", "", lack_pos], ["链接", "10", "", lack_pos],
                                ["缺IMEI", "11", "", "第二套：缺少 IMEI"]])

    def test_missing_materials_csv_format_and_matching(self):
        self.materials("007", "发票.jpg")
        self.materials(".隐藏")
        self.materials("T1", "发票.jpg", "POS小票.jpg", "SN图片.jpg")
        headers = ("交易参考号", " 序号 ", "缺少原因", "缺少原因_2")
        rows = [["T1", " 007 ", "a,b", "多\n行"], ["", "", "空参考号", ""], ["T2", "007"],
                ["T2", "007", "重复", "", "", ""], ["T1", ""], ["T3", ".隐藏", "", ""]]
        result = self.check(rows, column="序号", headers=headers)
        reason = "第一套：缺少 POS小票、SN图片"
        hidden = "第一套：缺少 POS小票、SN图片、发票"
        self.assertEqual(result, [[*headers, "缺少原因_3"],
                                  ["T1", " 007 ", "a,b", "多\n行", reason],
                                  ["T2", "007", "", "", reason],
                                  ["T2", "007", "重复", "", reason],
                                  ["T3", ".隐藏", "", "", hidden]])
        self.assertEqual(self.check([["T1", "1", ""]]), [["交易参考号", "序号", "备注", "缺少原因"]])
        self.assertTrue((self.root / "缺少资料.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        with patch("rename.operations._material_problem", wraps=__import__(
                "rename.operations", fromlist=["x"])._material_problem) as checked:
            self.check([["T1", "1", ""], ["T1", "2", ""]])
        self.assertEqual(checked.call_count, 1)

    def test_missing_materials_errors_keep_existing_output(self):
        self.materials("T1")
        self.write_csv([["T1", "1"]], headers=["交易参考号", "序号"])
        source = self.csv_path.read_bytes()
        output = export_missing_materials(self.root, self.csv_path, "交易参考号")
        old = output.read_bytes()
        with self.assertRaises(FileExistsError):
            export_missing_materials(self.root, self.csv_path, "交易参考号")
        with self.assertRaises(ValueError):
            export_missing_materials(self.root, self.csv_path, "品牌品类", overwrite=True)
        with self.assertRaises(ValueError):
            export_missing_materials(self.root, output, "交易参考号", overwrite=True)
        self.write_csv([["T1", "1", "多余"]], headers=["交易参考号", "序号"])
        with self.assertRaisesRegex(ValueError, "超出表头"):
            export_missing_materials(self.root, self.csv_path, "交易参考号", overwrite=True)
        self.write_csv([["T1"]], headers=["交易参考号", "交易参考号"])
        with self.assertRaisesRegex(ValueError, "表头"):
            export_missing_materials(self.root, self.csv_path, "交易参考号", overwrite=True)
        self.csv_path.write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            export_missing_materials(self.root, self.csv_path, "交易参考号", overwrite=True)
        self.write_csv([["T1", "1"]], headers=["交易参考号", "序号"])
        with patch("pathlib.Path.iterdir", side_effect=PermissionError("无权限")), \
                self.assertRaises(PermissionError):
            export_missing_materials(self.root, self.csv_path, "交易参考号", overwrite=True)
        with patch("os.replace", side_effect=PermissionError("无权限")), \
                self.assertRaisesRegex(PermissionError, "请关闭正在打开它的程序"):
            export_missing_materials(self.root, self.csv_path, "交易参考号", overwrite=True)
        self.assertEqual(output.read_bytes(), old)
        output.unlink()
        output.symlink_to(self.csv_path)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            export_missing_materials(self.root, self.csv_path, "交易参考号", overwrite=True)
        self.assertEqual(self.csv_path.read_bytes(), source)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["T1", "缺少资料.csv"])

    def test_missing_materials_stat_error_keeps_existing_output(self):
        self.materials("T1", "发票.jpg", "POS小票.jpg", "SN图片.jpg")
        self.write_csv([["T1", "1"], ["T2", "2"]], headers=["交易参考号", "序号"])
        output = export_missing_materials(self.root, self.csv_path, "交易参考号")
        old = output.read_bytes()
        lstat = Path.lstat
        for broken in ("POS小票.jpg", "T1"):
            def failing(path, broken=broken):
                if path.name == broken:
                    raise PermissionError("无权限")
                return lstat(path)
            with patch("pathlib.Path.lstat", failing), self.assertRaises(PermissionError):
                export_missing_materials(self.root, self.csv_path, "交易参考号", overwrite=True)
            self.assertEqual(output.read_bytes(), old)

    def test_cli_missing_materials_check(self):
        self.materials("T1")
        self.write_csv([["T1", "1"]], headers=["交易参考号", "序号"])
        output = self.root / "缺少资料.csv"
        answers = ["6", str(self.root), str(self.csv_path), "3", "2",
                   "6", str(self.root), "y", f"'{self.csv_path}'", "1", "n",
                   "6", str(self.root), "n", "0"]
        with patch("builtins.input", side_effect=answers), patch("builtins.print") as printed:
            main()
        printed.assert_any_call("无效选择，请输入 1 或 2。")
        self.assertEqual(sum(c.args == ("已取消导出。",) for c in printed.call_args_list), 2)
        self.assertTrue(any("核对完成" in str(c) for c in printed.call_args_list))
        self.assertIn("第一套：缺少", output.read_text(encoding="utf-8-sig"))
        self.csv_path.write_text("错误\n")
        with patch("builtins.input", side_effect=["6", str(self.root), "y", str(self.csv_path), "1", "y", "0"]), \
                patch("builtins.print") as printed:
            main()
        self.assertTrue(any("无法执行" in str(c) for c in printed.call_args_list))
        self.assertIn("第一套：缺少", output.read_text(encoding="utf-8-sig"))

    def test_overwrite_export_failure_keeps_old_output_and_no_temp_file(self):
        output = export_folder_names(self.root, overwrite=True)
        output.chmod(0o640)
        old = output.read_bytes()
        # 非 UTF-8 文件夹名在写入时编码失败；替换失败用 patch 模拟。
        bad = self.root / os.fsdecode(b"\xff")
        bad.mkdir()
        with self.assertRaises(UnicodeEncodeError):
            export_folder_names(self.root, overwrite=True)
        bad.rmdir()
        (self.root / "新增").mkdir()
        with patch("os.replace", side_effect=PermissionError("无权限")), \
                self.assertRaisesRegex(PermissionError, "请关闭正在打开它的程序"):
            export_folder_names(self.root, overwrite=True)
        self.assertEqual(output.read_bytes(), old)
        self.assertEqual({p.name for p in self.root.iterdir()}, {output.name, "新增"})
        export_folder_names(self.root, overwrite=True)
        self.assertIn("新增", output.read_text(encoding="utf-8-sig"))
        self.assertEqual(output.stat().st_mode & 0o777, 0o640)
        (self.root / "新增").rmdir()
        output.unlink()

    def test_missing_headers_and_invalid_root_do_not_move_files(self):
        self.folder("T1")
        self.write_csv([["T1"]], headers=["错误表头"])
        for operation in (classify, rename_by_number):
            with self.subTest(operation=operation.__name__), self.assertRaises(ValueError):
                operation(self.root, self.csv_path)
        with self.assertRaises(FileNotFoundError):
            summarize(self.root / "不存在")
        self.assertTrue((self.root / "T1").is_dir())

    def test_classify_finds_headers_in_arbitrary_rows(self):
        self.csv_path = self.base / "任意文件名.csv"
        self.write_csv([[], [" 品牌品类 ", "其他", "交易参考号"], ["品牌甲", "备注", "T1"],
                        ["品牌乙", "", "T2"], ["品牌甲", "", "T3"], ["品牌乙", "", "T3"]],
                       headers=["不含台账"])
        for name in ("T1", "T2", "T3"):
            self.folder(name)
        report = classify(self.root, self.csv_path)
        self.assertEqual(report.success, 2)
        self.assertEqual(len(report.skipped), 1)
        self.assertTrue((self.root / "品牌甲/T1/发票.jpg").is_file())
        self.assertTrue((self.root / "品牌乙/T2/发票.jpg").is_file())
        self.assertTrue((self.root / "T3/发票.jpg").is_file())

    def test_rename_finds_headers_in_arbitrary_file_and_rows(self):
        self.csv_path = self.base / "任意子目录/任意文件名.csv"
        self.csv_path.parent.mkdir()
        self.write_csv([[], [" 序号 ", "其他", "交易参考号"], ["1-01", "备注", "T1"],
                        ["1-02", "", "T2"], ["1-01", "", "T1"], ["1-03", "", "T3"],
                        ["1-04", "", "T3"]], headers=["不含台账"])
        for name in ("T1", "T2", "T3"):
            self.folder(name)
        report = rename_by_number(self.root, self.csv_path)
        self.assertEqual(report.success, 3)
        self.assertEqual(report.failed, [])
        self.assertEqual(report.skipped, [])
        for name in ("1-01", "1-02", "T3（序号异常）"):
            self.assertEqual((self.root / name / "发票.jpg").read_bytes(), b"invoice")

    def test_duplicate_required_headers_are_rejected(self):
        self.folder("T1")
        for operation, target in ((classify, "品牌品类"), (rename_by_number, "序号")):
            with self.subTest(operation=operation.__name__):
                self.write_csv([["T1", "甲", "乙"]], headers=["交易参考号", target, target])
                with self.assertRaises(ValueError):
                    operation(self.root, self.csv_path)
                self.assertTrue((self.root / "T1/发票.jpg").is_file())

    def test_classify_by_batch_groups_folders_and_reuses_existing_batches(self):
        self.csv_path = self.base / "任意名称.csv"
        self.write_csv([["第一批", "", "T1"], ["第一批", "", "T2"], ["第一批", "", "T2"],
                        ["第二批", "", "T3"], ["第一批", "", "T4"], ["第二批", "", "T4"],
                        ["", "", "T5"], ["../外部", "", "T6"], ["第一批", "", "T7"]],
                       headers=["拨付批次", "其他", "交易参考号"])
        for name in ("T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "第一批/T7"):
            self.folder(name)
        report = classify_by_batch(self.root, self.csv_path)
        self.assertEqual(report.success, 3)
        self.assertEqual(report.failed, [])
        self.assertEqual(len(report.skipped), 6)  # T4–T8 及已存在批次目录“第一批”本身
        for path in ("第一批/T1", "第一批/T2", "第二批/T3", "第一批/T7",
                     "T4", "T5", "T6", "T7", "T8"):
            self.assertEqual((self.root / path / "发票.jpg").read_bytes(), b"invoice")
            self.assertEqual((self.root / path / "SN图片.png").read_bytes(), b"serial")
        rerun = classify_by_batch(self.root, self.csv_path)
        self.assertEqual((rerun.success, rerun.failed), (0, []))
        self.assertFalse((self.root / "第一批/第一批").exists())
        self.assertTrue((self.root / "第一批/T1/发票.jpg").is_file())

    def test_classify_by_batch_continues_after_move_failure(self):
        self.write_csv([["T1", "第一批"], ["T2", "第一批"]], headers=["交易参考号", "拨付批次"])
        self.folder("T1")
        self.folder("T2")
        original = Path.rename

        def rename(source, target):
            if source.name == "T1":
                raise PermissionError("无权限")
            return original(source, target)

        with patch.object(Path, "rename", rename):
            report = classify_by_batch(self.root, self.csv_path)
        self.assertEqual((report.success, len(report.failed)), (1, 1))
        self.assertEqual((self.root / "T1/发票.jpg").read_bytes(), b"invoice")
        self.assertEqual((self.root / "T1/SN图片.png").read_bytes(), b"serial")
        self.assertEqual((self.root / "第一批/T2/发票.jpg").read_bytes(), b"invoice")

    def test_classify_skips_plans_whose_target_is_another_source(self):
        # (台账行, 应跳过的来源)；独立的 D 总是正常移动。
        cases = {
            "直接依赖": ([["A", "B"], ["B", "品牌"]], {"A", "B"}),
            "反向排序": ([["B", "A"], ["A", "品牌"]], {"A", "B"}),
            "链式依赖": ([["A", "B"], ["B", "C"], ["C", "E"]], {"A", "B", "C"}),
            "循环依赖": ([["A", "B"], ["B", "A"]], {"A", "B"}),
        }
        for operation, column in ((classify, "品牌品类"), (classify_by_batch, "拨付批次")):
            for label, (rows, linked) in cases.items():
                with self.subTest(operation=operation.__name__, case=label):
                    for child in self.root.iterdir():
                        shutil.rmtree(child)
                    self.write_csv(rows + [["D", "品牌"]], headers=["交易参考号", column])
                    for name in linked | {"D"}:
                        self.folder(name)
                    report = operation(self.root, self.csv_path)
                    self.assertEqual((report.success, len(report.skipped), report.failed),
                                     (1, len(linked), []))
                    for name in linked:
                        self.assertEqual((self.root / name / "发票.jpg").read_bytes(), b"invoice")
                        self.assertEqual(sorted(p.name for p in (self.root / name).iterdir()),
                                         ["SN图片.png", "发票.jpg"])
                    self.assertEqual((self.root / "品牌/D/发票.jpg").read_bytes(), b"invoice")

    def pos_files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_pos_images_are_numbered_per_directory_and_rerun_is_stable(self):
        files = ["二合一照片.jpg", "发票.png", "IMEI.webp", "SN图片.heic", "发票截图.jpg",
                 "a.jpg", "b.JPG", "c.png", "数据.csv", "POS小票_3.jpg", "POS小票_02.jpg",
                 "子目录/x.jpeg", "子目录/深层/y.tif", "子目录/深层/z.TIFF", "子目录/POS小票.gif",
                 "._小票.jpg"]
        for index, name in enumerate(files):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"内容{index}".encode())
        (self.root / "POS小票_2.jpg").mkdir()
        outside = self.base / "外部.jpg"
        outside.write_bytes(b"outside")
        (self.root / "链接.jpg").symlink_to(outside)
        contents = sorted(self.pos_files().values())
        report = rename_pos_images(self.root)
        self.assertEqual(report.failed, [])
        self.assertEqual(report.success, 8)
        self.assertEqual(len(report.skipped), 1)
        names = set(self.pos_files())
        for name in ("二合一照片.jpg", "发票.png", "IMEI.webp", "SN图片.heic", "数据.csv",
                     "POS小票_3.jpg", "子目录/POS小票.gif", "子目录/POS小票.jpeg",
                     "子目录/深层/POS小票.tif", "子目录/深层/POS小票.TIFF", "POS小票.png", "._小票.jpg"):
            self.assertIn(name, names)
        # 按文件名排序：POS小票_02、POS小票_3、a、b、发票截图；_2 被目录占用，_3 恰好保持原名。
        self.assertEqual(self.pos_files()["POS小票.jpg"], "内容10".encode())
        self.assertEqual(self.pos_files()["POS小票_4.jpg"], "内容5".encode())
        self.assertEqual(self.pos_files()["POS小票_5.JPG"], "内容6".encode())
        self.assertEqual(self.pos_files()["POS小票_6.jpg"], "内容4".encode())
        self.assertEqual(sorted(self.pos_files().values()), contents)
        self.assertTrue((self.root / "POS小票_2.jpg").is_dir())
        self.assertTrue((self.root / "链接.jpg").is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")
        rerun = rename_pos_images(self.root)
        self.assertEqual((rerun.success, rerun.failed), (0, []))
        self.assertEqual(set(self.pos_files()), names)

    def test_single_old_or_similar_pos_name_becomes_pos(self):
        for folder, name in (("一", "POS小票_2.jpg"), ("二", "POS小票截图.jpg"), ("三", "POS小票_8.jpg")):
            (self.root / folder).mkdir()
            (self.root / folder / name).write_bytes(name.encode())
        report = rename_pos_images(self.root)
        self.assertEqual((report.success, report.skipped, report.failed), (3, [], []))
        self.assertEqual(self.pos_files(), {"一/POS小票.jpg": "POS小票_2.jpg".encode(),
                                            "二/POS小票.jpg": "POS小票截图.jpg".encode(),
                                            "三/POS小票.jpg": "POS小票_8.jpg".encode()})

    def test_mixed_old_and_new_names_are_renumbered_without_loss(self):
        names = ["POS小票.jpg", "POS小票_2.jpg", "POS小票_5.JPG", "a.jpg", "b.jpg",
                 "c.jpg", "d.jpg", "e.jpg", "f.jpg", "g.jpg", "h.jpg", "pos小票.png"]
        for name in names:
            (self.root / name).write_bytes(name.encode())
        (self.root / "POS小票_4.jpg").write_text("占用", encoding="utf-8")  # 也是待处理图片，不算占用
        (self.root / "POS小票_3.JPG").mkdir()  # 目录占用 _3
        report = rename_pos_images(self.root)
        self.assertEqual(report.failed, [])
        files = self.pos_files()
        self.assertEqual(files["POS小票.jpg"], "POS小票.jpg".encode())
        self.assertEqual(files["POS小票_2.jpg"], "POS小票_2.jpg".encode())
        self.assertEqual(files["POS小票_4.jpg"], "占用".encode())
        self.assertEqual(files["POS小票_5.JPG"], "POS小票_5.JPG".encode())
        self.assertEqual(files["POS小票_6.jpg"], b"a.jpg")
        self.assertEqual(files["POS小票_13.jpg"], b"h.jpg")
        self.assertEqual(files["POS小票.png"], "pos小票.png".encode())
        self.assertEqual(report.success, 9)
        self.assertEqual(len(files), len(names) + 1)
        rerun = rename_pos_images(self.root)
        self.assertEqual((rerun.success, rerun.failed), (0, []))
        self.assertEqual(self.pos_files(), files)

    def test_pos_staging_failure_restores_directory(self):
        for name in ("POS小票_2.jpg", "a.jpg", "b.jpg"):
            (self.root / name).write_bytes(name.encode())
        original = Path.rename

        def rename(source, target):
            if source.name == "b.jpg":
                raise PermissionError("无权限")
            return original(source, target)

        with patch.object(Path, "rename", rename):
            report = rename_pos_images(self.root)
        self.assertEqual((report.success, len(report.failed)), (0, 1))
        self.assertIn("暂存失败", report.failed[0])
        self.assertEqual(self.pos_files(), {n: n.encode() for n in ("POS小票_2.jpg", "a.jpg", "b.jpg")})

    def test_pos_final_failure_restores_unfinished_and_reports_path(self):
        (self.root / "子").mkdir()
        for name in ("POS小票_2.jpg", "a.jpg", "子/c.jpg"):
            (self.root / name).write_bytes(name.encode())
        original = Path.rename

        def rename(source, target):
            if target.name == "POS小票_2.jpg":
                raise PermissionError("无权限")
            return original(source, target)

        with patch.object(Path, "rename", rename):
            report = rename_pos_images(self.root)
        # POS小票_2 → POS小票 成功；a 写入 _2 失败并恢复；子目录照常处理。
        self.assertEqual((report.success, len(report.failed)), (2, 1))
        self.assertEqual(self.pos_files(), {"POS小票.jpg": "POS小票_2.jpg".encode(),
                                            "a.jpg": b"a.jpg", "子/POS小票.jpg": "子/c.jpg".encode()})

        (self.root / "POS小票.jpg").rename(self.root / "x.jpg")

        def rename_fail_restore(source, target):
            if target.name in {"POS小票_2.jpg", "x.jpg"}:
                raise PermissionError("无权限")
            return original(source, target)

        with patch.object(Path, "rename", rename_fail_restore):
            report = rename_pos_images(self.root)
        self.assertEqual(len(report.failed), 2)
        self.assertIn("恢复失败，文件现位于", report.failed[1])
        temp = report.failed[1].split("文件现位于 ")[1].split("：")[0]
        self.assertEqual(Path(temp).read_bytes(), "POS小票_2.jpg".encode())
        self.assertEqual(sorted(self.pos_files().values()),
                         sorted([b"a.jpg", "POS小票_2.jpg".encode(), "子/c.jpg".encode()]))

    def test_pos_interrupt_restores_staged_images_and_reports_path(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (self.root / name).write_bytes(name.encode())
        original = Path.rename

        def interrupt_after_first_stage(source, target):
            original(source, target)
            if source.name == "a.jpg":
                raise KeyboardInterrupt

        with patch.object(Path, "rename", interrupt_after_first_stage), \
                self.assertRaises(KeyboardInterrupt) as caught:
            rename_pos_images(self.root)
        self.assertEqual(str(caught.exception), "")
        self.assertEqual(self.pos_files(), {n: n.encode() for n in ("a.jpg", "b.jpg", "c.jpg")})

        def interrupt_and_block_restore(source, target):
            if target.name == "b.jpg":
                raise PermissionError("无权限")
            original(source, target)
            if target.name == "POS小票.jpg":
                raise KeyboardInterrupt

        with patch.object(Path, "rename", interrupt_and_block_restore), \
                patch("builtins.input", side_effect=["4", str(self.root), "y"]), \
                patch("builtins.print") as output:
            main()
        # a 已完成；b 恢复失败留在临时名称并报告；c 恢复原名。
        files = self.pos_files()
        self.assertEqual(files.pop("POS小票.jpg"), b"a.jpg")
        self.assertEqual(files.pop("c.jpg"), b"c.jpg")
        [(temp, content)] = files.items()
        self.assertTrue(temp.startswith("POS小票临时_"))
        self.assertEqual(content, b"b.jpg")
        output.assert_any_call("\n已退出。")
        self.assertTrue(any(call.args[0].startswith("失败：") and temp in call.args[0]
                            for call in output.call_args_list))

    def test_short_rows_treat_missing_fields_as_empty(self):
        self.write_csv([["T1", "品牌"]])
        self.folder("T1")
        report = rename_by_number(self.root, self.csv_path)
        self.assertEqual((report.success, report.failed), (0, []))
        self.assertIn("目标字段为空", report.skipped[0])
        self.assertEqual(classify(self.root, self.csv_path).success, 1)

    def test_number_with_invalid_folder_characters_is_rejected(self):
        self.write_csv([["T1", "品牌", "2026/1/1"]])
        self.folder("T1")
        report = rename_by_number(self.root, self.csv_path)
        self.assertEqual(report.success, 0)
        self.assertIn("目标名称无效", report.skipped[0])
        self.assertTrue((self.root / "T1/发票.jpg").is_file())

    def test_csv_with_or_without_bom_keeps_text_and_quoted_fields(self):
        for encoding in ("utf-8-sig", "utf-8"):
            with self.subTest(encoding=encoding):
                self.root = self.base / encoding
                self.root.mkdir()
                # 表头在首行，验证 BOM 不会混入首列列名。
                with self.csv_path.open("w", encoding=encoding, newline="") as stream:
                    csv.writer(stream).writerows([["交易参考号", "品牌品类", "序号"],
                                                  ["T1", "品牌,甲", "001"],
                                                  ["T2", "多\n行", "0002"]])
                self.folder("T1")
                self.folder("T2")
                self.assertEqual(classify(self.root, self.csv_path).success, 2)
                self.assertEqual(rename_by_number(self.root, self.csv_path).success, 2)
                self.assertTrue((self.root / "品牌,甲/001/发票.jpg").is_file())
                self.assertTrue((self.root / "多\n行/0002/发票.jpg").is_file())

    def test_invalid_csv_does_not_move_files(self):
        self.folder("T1")
        for content, error in ((b"\xff\xfe\x00", ValueError),
                               ("交易参考号,品牌品类,序号\r\n\"T1\"x,品牌,1\r\n".encode(), csv.Error),
                               ("交易参考号,品牌品类,序号\r\n\"T1,品牌,1\r\n".encode(), csv.Error),
                               (b"", ValueError)):
            self.csv_path.write_bytes(content)
            for operation in (classify, classify_by_batch, rename_by_number):
                with self.subTest(content=content, operation=operation.__name__), \
                        self.assertRaises(error):
                    operation(self.root, self.csv_path)
            with patch("builtins.input", side_effect=["1", str(self.root), str(self.csv_path), "y", "0"]), \
                    patch("builtins.print") as output:
                main()
            self.assertTrue(any("无法执行" in str(call) for call in output.call_args_list))
        self.assertEqual([p.name for p in self.root.iterdir()], ["T1"])
        self.assertEqual((self.root / "T1/发票.jpg").read_bytes(), b"invoice")

    def test_symlinks_are_not_followed_or_used_as_output_targets(self):
        self.write_csv([["T1", "品牌", "1-01"]])
        self.folder("T1")
        outside = self.base / "外部"
        outside.mkdir()
        (self.root / "品牌").symlink_to(outside, target_is_directory=True)
        self.assertEqual(classify(self.root, self.csv_path).success, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_move_failure_is_reported(self):
        self.write_csv([["T1", "品牌", "1-01"]])
        self.folder("T1")
        with patch.object(Path, "rename", side_effect=PermissionError("无权限")):
            report = classify(self.root, self.csv_path)
        self.assertEqual(len(report.failed), 1)
        self.assertTrue((self.root / "T1").exists())

    def test_cli_invalid_choice_and_exit(self):
        with patch("builtins.input", side_effect=["invalid", "7", "0"]), \
                patch("builtins.print") as output:
            main()
        self.assertEqual(sum("无效选项，请输入 0–6" in str(call) for call in output.call_args_list), 2)
        menu = output.call_args_list[0].args[0]
        self.assertIn("6. 导出名称并核对资料", menu)
        self.assertNotIn("7.", menu)

    def test_cli_error_and_interrupt_handling(self):
        with patch("builtins.input", side_effect=["5", "", "5", str(self.root / "不存在"), "y", "0"]), \
                patch("builtins.print") as output:
            main()
        self.assertEqual(sum("无法执行" in str(call) for call in output.call_args_list), 2)
        for exception in (EOFError, KeyboardInterrupt):
            with patch("builtins.input", side_effect=exception), patch("builtins.print") as output:
                main()
            output.assert_any_call("\n已退出。")

    def test_cli_classify_rename_and_summary_are_separate_choices(self):
        self.write_csv([["T1", "品牌", "1-01"]])
        self.folder("T1")
        answers = ["1", str(self.root), str(self.csv_path), "y",
                   "3", str(self.root), str(self.csv_path), "y", "5", str(self.root), "y", "0"]
        with patch("builtins.input", side_effect=answers), patch("builtins.print"):
            main()
        self.assertEqual((self.root / "汇总/1-01/发票.jpg").read_bytes(), b"invoice")

    def test_cli_classify_by_batch(self):
        self.write_csv([["T1", "第一批"]], headers=["交易参考号", "拨付批次"])
        self.folder("T1")
        with patch("builtins.input", side_effect=["2", str(self.root), str(self.csv_path), " Y ", "0"]), \
                patch("builtins.print") as output:
            main()
        self.assertEqual((self.root / "第一批/T1/发票.jpg").read_bytes(), b"invoice")
        output.assert_any_call("成功：1，跳过：0，失败：0。")

    def test_cli_rename_pos_images(self):
        (self.root / "小票.jpg").write_bytes(b"pos")
        with patch("builtins.input", side_effect=["4", str(self.root), "n", "4", str(self.root), "", "0"]), \
                patch("builtins.print") as output:
            main()
        self.assertTrue((self.root / "小票.jpg").is_file())
        self.assertEqual(sum(call.args == ("已取消。",) for call in output.call_args_list), 2)
        with patch("builtins.input", side_effect=["4", str(self.root), "y", "0"]), \
                patch("builtins.print") as output:
            main()
        self.assertEqual((self.root / "POS小票.jpg").read_bytes(), b"pos")
        output.assert_any_call("成功：1，跳过：0，失败：0。")

    def test_pos_scan_failure_returns_accumulated_report(self):
        for name in ("a/小票.jpg", "b/小票.jpg", "c/小票.jpg"):
            (self.root / name).parent.mkdir()
            (self.root / name).write_bytes(name.encode())
        blocked = (self.root / "b").resolve()
        scandir, listdir = os.scandir, os.listdir

        def fail_scandir(path="."):
            if Path(path) == blocked:
                raise PermissionError(13, "无权限", str(path))
            return scandir(path)

        def fail_listdir(path="."):
            if Path(path) == blocked:
                raise PermissionError(13, "无权限", str(path))
            return listdir(path)

        for label, target, fake in (("扫描", "os.scandir", fail_scandir),
                                    ("列目录", "os.listdir", fail_listdir)):
            with self.subTest(label):
                for name in ("a", "b", "c"):
                    for path in (self.root / name).iterdir():
                        path.rename(path.with_name("小票.jpg"))
                with patch(target, fake):
                    report = rename_pos_images(self.root)
                self.assertEqual(report.success, 1)
                self.assertEqual(len(report.failed), 1)
                self.assertIn(str(blocked), report.failed[0])
                self.assertEqual(self.pos_files(), {"a/POS小票.jpg": "a/小票.jpg".encode(),
                                                    "b/小票.jpg": "b/小票.jpg".encode(),
                                                    "c/小票.jpg": "c/小票.jpg".encode()})
        with patch("os.listdir", fail_listdir), patch("builtins.print") as output, \
                patch("builtins.input", side_effect=["4", str(self.root), "y", "0"]):
            for path in (self.root / "a").iterdir():
                path.rename(path.with_name("小票.jpg"))
            main()
        output.assert_any_call("成功：1，跳过：0，失败：1。")
        self.assertTrue(any(str(blocked) in str(call) for call in output.call_args_list))

if __name__ == "__main__":
    unittest.main()
