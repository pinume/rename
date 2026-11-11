from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from .operations import classify, export_markdown, rename_by_number, summarize


def _input_path(prompt: str) -> Path:
    value = input(prompt).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not value:
        raise ValueError("路径不能为空")
    return Path(value).expanduser()


def main() -> None:
    try:
        while True:
            print("\n1. 按品牌品类分类\n2. 导出 Markdown 目录结构"
                  "\n3. 汇总资料子文件夹\n4. 按 Excel 序号重命名子文件夹\n0. 退出")
            choice = input("请选择功能：").strip()
            if choice == "0":
                return
            if choice not in {"1", "2", "3", "4"}:
                print("无效选项，请输入 0–4。")
                continue
            try:
                root = _input_path("资料目录路径：")
                if choice in {"1", "4"}:
                    excel = _input_path("Excel 文件路径：")
                    operation = classify if choice == "1" else rename_by_number
                    report = operation(root, excel)
                elif choice == "3":
                    report = summarize(root)
                else:
                    output = root / "目录结构.md"
                    overwrite = False
                    if output.exists() or output.is_symlink():
                        overwrite = input("目录结构.md 已存在，是否覆盖？[y/N]：").strip().lower() == "y"
                        if not overwrite:
                            print("已取消导出。")
                            continue
                    output = export_markdown(root, overwrite=overwrite)
                    print(f"成功：1，跳过：0，失败：0。已保存：{output}")
                    continue
                print(f"成功：{report.success}，跳过：{len(report.skipped)}，失败：{len(report.failed)}。")
                for message in report.skipped:
                    print(f"跳过：{message}")
                for message in report.failed:
                    print(f"失败：{message}")
            except (OSError, ValueError, BadZipFile, InvalidFileException, KeyError, ParseError) as exc:
                print(f"无法执行：{exc}")
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
