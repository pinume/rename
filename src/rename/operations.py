"""资料整理操作；所有移动都保留原目录内的文件。"""

import csv
import os
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path


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


def _mapping(csv_path: Path, target: str) -> dict[str, set[str]]:
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream, strict=True)
        for row in rows:
            headers = [v.strip() for v in row]
            if all(headers.count(name) == 1 for name in ("交易参考号", target)):
                break
        else:
            raise ValueError(f"CSV 未找到同时包含唯一的“交易参考号”和“{target}”列的表头")
        reference_index, target_index = headers.index("交易参考号"), headers.index(target)
        result = defaultdict(set)
        for row in rows:
            # 短行缺少的字段视为空值。
            reference, value = (row[i].strip() if i < len(row) else ""
                                for i in (reference_index, target_index))
            if reference:
                result[reference].add(value)
    return dict(result)


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


def classify(root: Path, csv_path: Path, column: str = "品牌品类") -> Report:
    root = _directory(root)
    mapping = _mapping(csv_path, column)
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    return _organize(folders, mapping, root, classify=True)


def classify_by_batch(root: Path, csv_path: Path) -> Report:
    return classify(root, csv_path, "拨付批次")


def rename_by_number(root: Path, csv_path: Path) -> Report:
    root = _directory(root)
    mapping = _mapping(csv_path, "序号")
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

def _sort_key(name: str):
    # 数字按数值排序，使 POS小票_10 排在 _9 之后，重复执行时编号稳定。
    return [int(p) if i % 2 else p for i, p in enumerate(re.split(r"(\d+)", name))], name


def _rename_new(source: Path, target: Path) -> None:
    # POSIX 的 rename 会静默覆盖已有文件，先检查目标。
    if os.path.lexists(target):
        raise FileExistsError(f"目标已存在 {target}")
    source.rename(target)


def _restore(staged: list[tuple[Path, Path]], report: Report) -> None:
    for source, temp in staged:
        try:
            _rename_new(temp, source)
        except OSError as exc:
            report.failed.append(f"{source}：恢复失败，文件现位于 {temp}：{exc}")


def rename_pos_images(root: Path) -> Report:
    root = _directory(root)
    report = Report()
    for current, _, files in _walk(root):
        pending = []
        for name in sorted(files, key=_sort_key):
            source = current / name
            # 隐藏文件（如外接盘上的 ._ 辅助文件）不是真实图片。
            if (name.startswith(".") or source.suffix.lower() not in _IMAGE_SUFFIXES
                    or source.stem in _KEEP_STEMS):
                continue
            if source.is_symlink():
                report.skipped.append(f"{source}：符号链接")
                continue
            pending.append(source)
        _renumber(current, pending, report)
    return report


def _renumber(current: Path, pending: list[Path], report: Report) -> None:
    names = {p.name for p in pending}
    # 待处理图片的旧名称不算占用；其余按不区分大小写记录，避免在大小写不敏感的文件系统上撞名。
    occupied = {name.casefold() for name in os.listdir(current) if name not in names}
    next_numbers = {}
    moves = []
    for source in pending:
        number = next_numbers.get(source.suffix.casefold(), 1)
        while (target := f"POS小票{f'_{number}' if number > 1 else ''}{source.suffix}"
               ).casefold() in occupied:
            number += 1
        next_numbers[source.suffix.casefold()] = number
        occupied.add(target.casefold())
        if target != source.name:
            moves.append((source, current / target))
    # 先全部移到临时名称，再写入最终名称，防止新旧名称互相占位或覆盖。
    staged = []
    try:
        for source, target in moves:
            while os.path.lexists(temp := current / f"POS小票临时_{uuid.uuid4().hex}{source.suffix}"):
                pass
            # 先登记再改名，改名刚完成就中断时也能找到临时文件。
            staged.append((source, temp, target))
            try:
                _rename_new(source, temp)
            except OSError as exc:
                staged.pop()
                report.failed.append(f"{source} → {target}：暂存失败，跳过该目录：{exc}")
                _restore([(s, t) for s, t, _ in reversed(staged)], report)
                return
        for index, (source, temp, target) in enumerate(staged):
            try:
                _rename_new(temp, target)
                report.success += 1
            except OSError as exc:
                report.failed.append(f"{source} → {target}：{exc}")
                _restore([(s, t) for s, t, _ in staged[index:]], report)
                return
    except KeyboardInterrupt:
        # 中断时恢复仍处于临时名称的图片，并把无法恢复的实际路径随中断带给调用方。
        interrupted = Report()
        _restore([(s, t) for s, t, _ in staged if os.path.lexists(t)], interrupted)
        raise KeyboardInterrupt("\n".join(interrupted.failed)) from None


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


_TREE_NAME = "目录结构.txt"
_ESCAPES = str.maketrans({"\n": "\\n", "\r": "\\r", "\t": "\\t"})


def _tree_lines(folder: Path, prefix: str, skip: str | None = None) -> list[str]:
    # scandir 出错（如无权限）直接抛出，由调用方在写文件前终止。
    with os.scandir(folder) as entries:
        entries = [e for e in entries if not e.is_symlink() and e.name not in {".DS_Store", skip}]
    dirs = sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
    files = sorted(e.name for e in entries if not e.is_dir(follow_symlinks=False))
    items = [(name, True) for name in dirs] + [(name, False) for name in files]
    lines = []
    for index, (name, is_dir) in enumerate(items):
        last = index == len(items) - 1
        lines.append(f"{prefix}{'└── ' if last else '├── '}{name.translate(_ESCAPES)}{'/' if is_dir else ''}")
        if is_dir:
            lines += _tree_lines(folder / name, prefix + ("    " if last else "│   "))
    return lines


def export_directory_tree(root: Path, *, overwrite: bool = False) -> Path:
    root = _directory(root)
    output = root / _TREE_NAME
    if output.is_symlink():
        raise ValueError(f"{_TREE_NAME} 是符号链接，不能覆盖")
    # 先完成扫描再打开输出文件，扫描失败时保留已有文件。
    lines = [f"{(root.name or str(root)).translate(_ESCAPES)}/", *_tree_lines(root, "", _TREE_NAME)]
    with output.open("w" if overwrite else "x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
    return output
