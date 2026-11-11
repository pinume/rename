"""资料整理操作；所有移动都保留原目录内的文件。"""

import os
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
        if "家电台账" not in workbook.sheetnames:
            raise ValueError("Excel 缺少“家电台账”工作表")
        rows = workbook["家电台账"].iter_rows(min_row=2, values_only=True)
        headers = [str(v).strip() if v is not None else "" for v in next(rows, ())]
        for name in ("交易参考号", target):
            if headers.count(name) != 1:
                raise ValueError(f"Excel 第 2 行必须包含唯一的“{name}”列")
        reference_index, target_index = headers.index("交易参考号"), headers.index(target)
        result = defaultdict(set)
        for row in rows:
            reference = row[reference_index]
            value = row[target_index]
            if reference is not None and str(reference).strip():
                result[str(reference).strip()].add(str(value).strip() if value is not None else "")
        return dict(result)
    finally:
        workbook.close()


def _target_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and not any(c in name for c in "/\\\0")


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


def classify(root: Path, excel: Path) -> Report:
    root = _directory(root)
    mapping = _mapping(excel, "品牌品类")
    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    return _organize(folders, mapping, root, classify=True)


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


def export_markdown(root: Path, *, overwrite: bool = False) -> Path:
    root = _directory(root)
    output = root / "目录结构.md"
    if output.is_symlink():
        raise ValueError("目录结构.md 是符号链接，不能覆盖")
    lines = []

    def tree(folder: Path, prefix: str = ""):
        entries = sorted((p for p in folder.iterdir()
                          if p.name != ".DS_Store" and not p.is_symlink()
                          and (folder != root or p.is_dir())), key=lambda p: p.name)
        for index, entry in enumerate(entries):
            last = index == len(entries) - 1
            name = entry.name.replace("\n", "\\n").replace("\r", "\\r")
            lines.append(prefix + ("└── " if last else "├── ") + name
                         + ("/" if entry.is_dir() else ""))
            if entry.is_dir():
                tree(entry, prefix + ("    " if last else "│   "))

    tree(root)
    # 文件名可能包含反引号，使用更长的围栏保持 Markdown 完整。
    fence = "```"
    content = "\n".join(lines)
    while fence in content:
        fence += "`"
    with output.open("w" if overwrite else "x", encoding="utf-8") as stream:
        stream.write(f"{fence}text\n{content}\n{fence}\n")
    return output
