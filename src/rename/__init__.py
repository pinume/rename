import csv
from pathlib import Path

from .operations import (classify, classify_by_batch, export_folder_names,
                         export_missing_materials, rename_by_number, rename_pos_images,
                         rename_to_reference, summarize)


def _input_path(prompt: str, *, optional: bool = False) -> Path | None:
    value = input(prompt).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if not value:
        if optional:
            return None
        raise ValueError("路径不能为空")
    return Path(value).expanduser()


def _export(operation, name: str, root: Path, *args) -> Path | None:
    output = root / name
    overwrite = False
    if output.exists() or output.is_symlink():
        overwrite = input(f"{name} 已存在，是否覆盖？[y/N]：").strip().lower() == "y"
        if not overwrite:
            print("已取消导出。")
            return None
    return operation(root, *args, overwrite=overwrite)


# 按整理流程排序：先分类，再重命名（含改回参考号），再把资料夹移到输入目录，最后导出。
_MENU = {
    "1": ("按品牌品类分类", classify, True),
    "2": ("按拨付批次分类", classify_by_batch, True),
    "3": ("重命名为序号", rename_by_number, True),
    "4": ("重命名为参考号", rename_to_reference, True),
    "5": ("pos小票fmt", rename_pos_images, False),
    "6": ("汇总文件夹", summarize, False),
    "7": ("核对资料", export_folder_names, False),
}
_CHECK_COLUMNS = {"1": "序号", "2": "交易参考号"}


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
            _, operation, needs_csv = _MENU[choice]
            try:
                root = _input_path("资料目录路径：")
                if operation is export_folder_names:
                    if not (output := _export(operation, "子文件夹名称.csv", root)):
                        continue
                    print(f"成功：1，跳过：0，失败：0。已保存：{output}")
                    if not (csv_path := _input_path("核对 CSV 文件路径（回车跳过）：", optional=True)):
                        continue
                    while (column := input("核对列：1. 序号  2. 交易参考号，请选择：").strip()
                           ) not in _CHECK_COLUMNS:
                        print("无效选择，请输入 1 或 2。")
                    if output := _export(export_missing_materials, "缺少资料.csv", root,
                                         csv_path, _CHECK_COLUMNS[column]):
                        print(f"核对完成。已保存：{output}")
                    continue
                csv_path = _input_path("CSV 文件路径：") if needs_csv else None
                if operation is summarize:
                    print(f"将 {root}/分类目录/资料夹 整夹移到 {root}/资料夹，并删除其中的空目录。"
                          "整理后层级已改变，不要直接重跑，否则会把资料夹内的子目录再次移出。")
                # 以下操作直接改名或移动且不留记录，执行前确认路径。
                if input(f"将直接修改 {root}，继续？[y/N]：").strip().lower() != "y":
                    print("已取消。")
                    continue
                report = operation(root, csv_path) if needs_csv else operation(root)
                removed = f"删除空目录：{report.removed}，" if operation is summarize else ""
                print(f"成功：{report.success}，{removed}跳过：{len(report.skipped)}，"
                      f"失败：{len(report.failed)}。")
                for message in report.skipped:
                    print(f"跳过：{message}")
                for message in report.failed:
                    print(f"失败：{message}")
            except (OSError, ValueError, csv.Error) as exc:
                print(f"无法执行：{exc}")
    except (KeyboardInterrupt, EOFError) as exc:
        print("\n已退出。")
        for message in filter(None, str(exc).splitlines()):
            print(f"失败：{message}")
