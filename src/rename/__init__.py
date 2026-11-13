from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

from openpyxl.utils.exceptions import InvalidFileException

from .operations import (classify, classify_by_batch, export_folder_names, rename_by_number,
                         rename_pos_images, summarize)


def _input_path(prompt: str) -> Path:
    value = input(prompt).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not value:
        raise ValueError("路径不能为空")
    return Path(value).expanduser()


# 按整理流程排序：先分类，再重命名，最后汇总和导出。
_MENU = {
    "1": ("按品牌品类分类", classify, True),
    "2": ("按拨付批次分类", classify_by_batch, True),
    "3": ("重命名为序号", rename_by_number, True),
    "4": ("pos小票fmt", rename_pos_images, False),
    "5": ("汇总资料子文件夹", summarize, False),
    "6": ("导出文件夹名称", export_folder_names, False),
}


def main() -> None:
    try:
        while True:
            print("\n" + "\n".join(f"{key}. {label}" for key, (label, _, _) in _MENU.items())
                  + "\n0. 退出")
            choice = input("请选择功能：").strip()
            if choice == "0":
                return
            if choice not in _MENU:
                print(f"无效选项，请输入 0–{len(_MENU)}。")
                continue
            _, operation, needs_excel = _MENU[choice]
            try:
                root = _input_path("资料目录路径：")
                if operation is export_folder_names:
                    output = root / "子文件夹名称.csv"
                    overwrite = False
                    if output.exists() or output.is_symlink():
                        overwrite = input("子文件夹名称.csv 已存在，是否覆盖？[y/N]：").strip().lower() == "y"
                        if not overwrite:
                            print("已取消导出。")
                            continue
                    output = export_folder_names(root, overwrite=overwrite)
                    print(f"成功：1，跳过：0，失败：0。已保存：{output}")
                    continue
                excel = _input_path("Excel 文件路径：") if needs_excel else None
                # 以下操作直接改名或移动且不留记录，执行前确认路径。
                if input(f"将直接修改 {root}，继续？[y/N]：").strip().lower() != "y":
                    print("已取消。")
                    continue
                report = operation(root, excel) if needs_excel else operation(root)
                print(f"成功：{report.success}，跳过：{len(report.skipped)}，失败：{len(report.failed)}。")
                for message in report.skipped:
                    print(f"跳过：{message}")
                for message in report.failed:
                    print(f"失败：{message}")
            except (OSError, ValueError, BadZipFile, InvalidFileException, KeyError, ParseError) as exc:
                print(f"无法执行：{exc}")
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
