"""资料整理操作；所有移动都保留原目录内的文件。"""

import csv
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook


@dataclass
class Report:
    success: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _directory(path: Path) -> Path:
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"不是目录：{path}")
    return path


def _mapping(excel: Path, target: str) -> dict[str, set[str]]:
    workbook = load_workbook(excel, read_only=True, data_only=True)
    try:
        result = defaultdict(set)
        found = False
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            for row in rows:
                headers = [str(v).strip() if v is not None else "" for v in row]
                if all(headers.count(name) == 1 for name in ("交易参考号", target)):
                    found = True
                    break
            else:
                continue
            reference_index, target_index = headers.index("交易参考号"), headers.index(target)
            for row in rows:
                # 缺少表格范围信息时，行尾空单元格不会出现在行中。
                reference, value = (row[i] if i < len(row) else None
                                    for i in (reference_index, target_index))
                if reference is not None and str(reference).strip():
                    result[str(reference).strip()].add(str(value).strip() if value is not None else "")
        if not found:
            raise ValueError(f"Excel 未找到同时包含唯一的“交易参考号”和“{target}”列的表头")
        return dict(result)
    finally:
        workbook.close()


def _target_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and not any(c in name for c in "/\\\0:")


def _walk(root: Path, exclude_summary: bool = False):
    def onerror(error):
        raise error

    for current, dirs, files in os.walk(root, onerror=onerror, followlinks=False):
        current = Path(current)
        dirs[:] = sorted(d for d in dirs if not (current / d).is_symlink()
                         and not (exclude_summary and current == root and d == "汇总"))
        yield current, dirs, sorted(f for f in files if f != ".DS_Store")


def _move(plans: list[tuple[Path, Path]], report: Report) -> Report:
    destinations = Counter(target for _, target in plans)
    for source, target in plans:
        if destinations[target] > 1:
            report.skipped.append(f"{source}：多个来源对应同一目标 {target}")
            continue
        if target == source or source in target.parents:
            report.skipped.append(f"{source}：目标与来源相同或位于来源内部")
            continue
        try:
            if target.parent.is_symlink():
                report.skipped.append(f"{source}：目标父目录是符号链接")
                continue
            if target.exists() or target.is_symlink():
                report.skipped.append(f"{source}：目标已存在 {target}")
                continue
            target.parent.mkdir(exist_ok=True)
            source.rename(target)
            report.success += 1
        except OSError as exc:
            report.failed.append(f"{source} → {target}：{exc}")
    return report


def classify(root: Path, excel: Path, column: str = "品牌品类") -> Report:
    root = _directory(root)
    mapping = _mapping(excel, column)
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    return _organize(folders, mapping, root, classify=True)


def classify_by_batch(root: Path, excel: Path) -> Report:
    return classify(root, excel, "拨付批次")


def rename_by_number(root: Path, excel: Path) -> Report:
    root = _directory(root)
    mapping = _mapping(excel, "序号")
    folders = [current / d for current, dirs, _ in _walk(root) for d in dirs]
    # 先处理深层目录，避免上层重命名后使尚未处理的路径失效。
    folders.sort(key=lambda p: len(p.parts), reverse=True)
    return _organize(folders, mapping, root, classify=False)


def _organize(folders: list[Path], mapping: dict[str, set[str]], root: Path,
              *, classify: bool) -> Report:
    report = Report()
    plans = []
    for source in folders:
        values = mapping.get(source.name)
        if not values:
            report.skipped.append(f"{source}：未匹配交易参考号")
        elif not classify and len(values - {""}) > 1:
            plans.append((source, source.with_name(f"{source.name}（序号异常）")))
        elif len(values) != 1 or "" in values:
            report.skipped.append(f"{source}：目标字段为空或存在不同目标值")
        else:
            name = next(iter(values))
            if not _target_name(name):
                report.skipped.append(f"{source}：目标名称无效 {name!r}")
                continue
            target = root / name / source.name if classify else source.with_name(name)
            plans.append((source, target))
    return _move(plans, report)


def summarize(root: Path) -> Report:
    root = _directory(root)
    plans = [(current, root / "汇总" / current.name)
             for current, dirs, files in _walk(root, exclude_summary=True)
             if current != root and not dirs and files]
    return _move(plans, Report())


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
                   ".tif", ".tiff", ".heic", ".heif", ".avif"}
_KEEP_STEMS = {"二合一照片", "发票", "IMEI", "SN图片"}
_POS_DONE = re.compile(r"POS小票(_([2-9]|[1-9]\d+))?")


def rename_pos_images(root: Path) -> Report:
    root = _directory(root)
    report = Report()
    plans = []
    for current, _, files in _walk(root):
        # 按不区分大小写记录占用，避免在大小写不敏感的文件系统上撞名。
        occupied = {name.casefold() for name in os.listdir(current)}
        for name in files:
            source = current / name
            # 隐藏文件（如外接盘上的 ._ 辅助文件）不是真实图片。
            if (name.startswith(".") or source.suffix.lower() not in _IMAGE_SUFFIXES
                    or source.stem in _KEEP_STEMS
                    or _POS_DONE.fullmatch(source.stem)):
                continue
            if source.is_symlink():
                report.skipped.append(f"{source}：符号链接")
                continue
            number = 1
            while (target := f"POS小票{f'_{number}' if number > 1 else ''}{source.suffix}"
                   ).casefold() in occupied:
                number += 1
            occupied.add(target.casefold())
            plans.append((source, current / target))
    return _move(plans, report)


def export_folder_names(root: Path, *, overwrite: bool = False) -> Path:
    root = _directory(root)
    output = root / "子文件夹名称.csv"
    if output.is_symlink():
        raise ValueError("子文件夹名称.csv 是符号链接，不能覆盖")
    names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    # utf-8-sig 让 Excel 正确识别中文；newline="" 交给 csv 处理名称中的换行。
    with output.open("w" if overwrite else "x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["文件夹名称"])
        writer.writerows([name] for name in names)
    return output
