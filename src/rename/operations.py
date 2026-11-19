"""资料整理操作；所有移动都保留原目录内的文件。"""

import csv
import os
import re
import stat
import tempfile
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager, suppress
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
    if classify:
        # 目标父目录也是另一项来源时，结果取决于执行顺序，关联的来源全部跳过。
        sources = {source for source, _ in plans}
        linked = defaultdict(set)
        for source, target in plans:
            if target.parent in sources and target.parent != source:
                linked[source].add(target.parent)
                linked[target.parent].add(source)
        for source in sorted(linked):
            others = "、".join(str(p) for p in sorted(linked[source]))
            report.skipped.append(f"{source}：与 {others} 的分类移动相互影响")
        plans = [plan for plan in plans if plan[0] not in linked]
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
    current = root
    try:
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
    except OSError as exc:
        # 扫描中止时返回已累计的结果，菜单仍能显示已完成的数量。
        report.failed.append(f"{exc.filename or current}：扫描失败，停止处理：{exc}")
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
    with _open_output(output, overwrite, "utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["文件夹名称"])
        writer.writerows([name] for name in names)
    return output


@contextmanager
def _open_output(output: Path, overwrite: bool, encoding: str):
    if not overwrite:
        with output.open("x", encoding=encoding, newline="") as stream:
            yield stream
        return
    # 覆盖时先写同目录临时文件，完整关闭后再替换，失败时保留原输出。
    fd, temp = tempfile.mkstemp(dir=output.parent, prefix=f".{output.name}.")
    try:
        with open(fd, "w", encoding=encoding, newline="") as stream:
            # mkstemp 创建的文件权限为 0600，沿用原输出的权限。
            with suppress(FileNotFoundError):
                os.chmod(temp, stat.S_IMODE(os.stat(output).st_mode))
            yield stream
        os.replace(temp, output)
    except BaseException:
        os.unlink(temp)
        raise


_MATERIAL_SETS = {"第一套": {"发票", "POS小票", "SN图片"},
                  "第二套": {"二合一照片", "发票", "IMEI", "POS小票"}}


def _material_problem(folder: Path | None) -> str:
    if folder is None:
        return "缺少文件夹"
    stems = {p.stem for p in folder.iterdir()
             if not p.name.startswith(".") and p.suffix.lower() in _IMAGE_SUFFIXES
             # is_file() 会把状态读取错误当作 False，用 lstat 让错误上抛，避免误报缺少。
             and stat.S_ISREG(p.lstat().st_mode)}
    if stems in _MATERIAL_SETS.values():
        return ""
    name = "第二套" if stems & {"二合一照片", "IMEI"} else "第一套"
    expected = _MATERIAL_SETS[name]
    diff = [f"{label} {'、'.join(sorted(names))}"
            for label, names in (("缺少", expected - stems), ("多出", stems - expected)) if names]
    return f"{name}：{'，'.join(diff)}"


def export_missing_materials(root: Path, csv_path: Path, column: str, *,
                             overwrite: bool = False) -> Path:
    if column not in ("序号", "交易参考号"):
        raise ValueError(f"不支持的核对列：{column}")
    root = _directory(root)
    output = root / "缺少资料.csv"
    if output.is_symlink():
        raise ValueError("缺少资料.csv 是符号链接，不能覆盖")
    if output.exists() and os.path.samefile(csv_path, output):
        raise ValueError("核对 CSV 与结果文件相同，不能覆盖")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream, strict=True)
        for header in rows:
            names = [v.strip() for v in header]
            if names.count(column) == 1:
                break
        else:
            raise ValueError(f"CSV 未找到包含唯一“{column}”列的表头")
        index, width = names.index(column), len(header)
        records = []
        for row in rows:
            if any(v.strip() for v in row[width:]):
                raise ValueError(f"CSV 第 {rows.line_num} 行字段数超出表头")
            row = row[:width] + [""] * (width - len(row))
            if key := row[index].strip():
                records.append((key, row))
    folders = {p.name: p for p in root.iterdir() if stat.S_ISDIR(p.lstat().st_mode)}
    problems = {}
    for key, _ in records:
        if key not in problems:
            problems[key] = _material_problem(folders.get(key))
    reason, number = "缺少原因", 1
    while reason in names:
        number += 1
        reason = f"缺少原因_{number}"
    with _open_output(output, overwrite, "utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(header + [reason])
        writer.writerows(row + [problems[key]] for key, row in records if problems[key])
    return output
