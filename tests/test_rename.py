import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rename import main
from rename.operations import (classify, classify_by_batch, export_folder_names,
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
        with patch("builtins.input", side_effect=["6", str(self.root), "0"]), patch("builtins.print"):
            main()
        self.assertEqual(output.read_bytes().decode("utf-8-sig"), "文件夹名称\r\nT1\r\n")
        (self.root / "T2").mkdir()
        with patch("builtins.input", side_effect=["6", str(self.root), "n", "0"]), \
                patch("builtins.print") as printed:
            main()
        printed.assert_any_call("已取消导出。")
        self.assertNotIn("T2", output.read_text(encoding="utf-8-sig"))
        with patch("builtins.input", side_effect=["6", str(self.root), " Y ", "0"]), \
                patch("builtins.print") as printed:
            main()
        self.assertIn("T2", output.read_text(encoding="utf-8-sig"))
        self.assertTrue(any("已保存" in str(call) for call in printed.call_args_list))

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
        # 按文件名排序：POS小票_02、a、b、发票截图；_2 被目录占用，_3 已存在。
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

    def test_pos_image_failure_is_reported_and_others_continue(self):
        for name in ("a.jpg", "b.jpg"):
            (self.root / name).write_bytes(name.encode())
        original = Path.rename

        def rename(source, target):
            if source.name == "a.jpg":
                raise PermissionError("无权限")
            return original(source, target)

        with patch.object(Path, "rename", rename):
            report = rename_pos_images(self.root)
        self.assertEqual((report.success, len(report.failed)), (1, 1))
        self.assertEqual((self.root / "a.jpg").read_bytes(), b"a.jpg")
        self.assertEqual((self.root / "POS小票_2.jpg").read_bytes(), b"b.jpg")

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

if __name__ == "__main__":
    unittest.main()
