# parser 开发期替身（conftest 注入，仅当仓库根 parser.py 不可导入时启用）。
#
# 按 deep-dive/hwinfo-plugin.md §3 公共接口逐字签名实现占位函数（行为与真实
# parser 对齐），供 H-01 提交前插件侧自测；H-01 落地后本文件不生效，main.py
# 自动经真实 parser.py 公共接口调用（conftest 仅在 import parser 失败时注入）。

import csv
import datetime
import re
import sys
import types
from typing import Dict, List, Optional

_DATE_FORMATS = {
    "d.m.y": "%d.%m.%Y",
    "m.d.y": "%m.%d.%Y",
    "y.m.d": "%Y.%m.%d",
}

_UNIT_RE = re.compile(r"\[(.*?)\]")
_INVALID_ID_CHARS = re.compile(r"[^a-z0-9_]")
_ID_UNDERSCORE_RUN = re.compile(r"_+")


class Column:
    """表头列元信息。"""

    __slots__ = ("index", "name", "unit", "metric_id", "kind")

    def __init__(self, index: int, name: str, unit: Optional[str],
                 metric_id: str, kind: str) -> None:
        self.index = index
        self.name = name
        self.unit = unit
        self.metric_id = metric_id
        self.kind = kind


class HwInfoSchema:
    """一次 load 冻结的表头与列分类结果。"""

    def __init__(self, columns: List[Column]) -> None:
        self.columns = columns
        self.include_bool = False

    @property
    def numeric(self) -> List[Column]:
        return [c for c in self.columns
                if c.kind == "numeric" or (c.kind == "bool" and self.include_bool)]

    @property
    def bool_columns(self) -> List[Column]:
        return [c for c in self.columns if c.kind == "bool"]


def split_csv_line(line: str) -> List[str]:
    """引号感知切列（RFC4180 子集：双引号包裹、"" 转义；Python csv 模块实现）。"""
    line = line.rstrip("\r\n")
    try:
        return next(csv.reader([line]))
    except csv.Error:
        return [line]


def parse_header(header_line: str) -> List[str]:
    """解析表头行 → 列名原文列表（去引号）。"""
    return split_csv_line(header_line)


def detect_date_format(date_text: str) -> str:
    """auto 探测日期顺序：首个字段数值 >12 → "d.m.y"；否则默认 "d.m.y"（spec 2.3）。"""
    first = date_text.split(".", 1)[0].strip()
    if first.isdigit() and int(first) > 12:
        return "d.m.y"
    return "d.m.y"


def parse_timestamp(date_text: str, time_text: str, date_format: str) -> Optional[int]:
    """HWiNFO 时间戳 → UTC 毫秒（本地时间直读，spec D3）。解析失败返回 None。"""
    try:
        fmt = _DATE_FORMATS[date_format]
        dt = datetime.datetime.strptime(date_text.strip(), fmt)
        try:
            t = datetime.datetime.strptime(time_text.strip(), "%H:%M:%S.%f").time()
        except ValueError:
            t = datetime.datetime.strptime(time_text.strip(), "%H:%M:%S").time()
        dt = dt.replace(hour=t.hour, minute=t.minute,
                        second=t.second, microsecond=t.microsecond)
    except (KeyError, ValueError):
        return None
    return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)


def normalize_metric_id(name: str, used: set) -> str:
    """列名 → 指标 id：去 "[...]" 单位后缀 → 小写 → 非 [a-z0-9_] 替换为 "_" →
    连续下划线折叠 → 首尾下划线去除；与 used 冲突时追加 "_2"、"_3"... 保证唯一。"""
    base = _UNIT_RE.sub("", name)
    base = base.lower()
    base = _INVALID_ID_CHARS.sub("_", base)
    base = _ID_UNDERSCORE_RUN.sub("_", base)
    base = base.strip("_")
    candidate = base
    n = 2
    while candidate in used:
        candidate = "{}_{}".format(base, n)
        n += 1
    used.add(candidate)
    return candidate


def _is_bool_cell(raw: str) -> bool:
    return raw.strip().lower() in {"yes", "no"}


def _is_float(raw: str) -> bool:
    try:
        float(raw.strip())
    except ValueError:
        return False
    return True


def classify_columns(header: List[str], sample_cells: List[List[str]],
                     include_bool: bool) -> HwInfoSchema:
    """前 1000 行样本判定列类型（spec 2.5）；Date/Time（index 0/1）恒为 drop。"""
    columns: List[Column] = []
    used: set = set()
    for i, name in enumerate(header):
        unit = None
        units = _UNIT_RE.findall(name)
        if units:
            unit = units[-1]
        kind = "drop"
        if i not in (0, 1):
            values = [row[i] for row in sample_cells if i < len(row)]
            if values and all(_is_bool_cell(v) for v in values):
                kind = "bool"
            elif any(_is_float(v) for v in values):
                kind = "numeric"
        columns.append(Column(i, name, unit, normalize_metric_id(name, used), kind))
    schema = HwInfoSchema(columns)
    schema.include_bool = include_bool
    return schema


def parse_row(cells: List[str], schema: HwInfoSchema, ts_ms: int,
              include_bool: bool) -> List[Dict]:
    """一行数据 → Record 列表（每数值列一条：{"timestamp","metric","value"}）。
    值解析失败（非数值文本且非 Yes/No）跳过该列；include_bool=True 时布尔列产出 1|0。"""
    records: List[Dict] = []
    for col in schema.columns:
        if col.kind == "drop":
            continue
        if col.kind == "bool" and not include_bool:
            continue
        raw = cells[col.index].strip()
        if col.kind == "bool":
            lower = raw.lower()
            if lower == "yes":
                records.append({"timestamp": ts_ms, "metric": col.metric_id, "value": 1})
            elif lower == "no":
                records.append({"timestamp": ts_ms, "metric": col.metric_id, "value": 0})
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        records.append({"timestamp": ts_ms, "metric": col.metric_id, "value": value})
    return records


def _install_stub() -> None:
    if sys.modules.get("parser") is not None:
        return
    module = types.ModuleType("parser")
    module.Column = Column
    module.HwInfoSchema = HwInfoSchema
    module.split_csv_line = split_csv_line
    module.parse_header = parse_header
    module.detect_date_format = detect_date_format
    module.parse_timestamp = parse_timestamp
    module.normalize_metric_id = normalize_metric_id
    module.classify_columns = classify_columns
    module.parse_row = parse_row
    sys.modules["parser"] = module


_install_stub()
