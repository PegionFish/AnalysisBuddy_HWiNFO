"""hwinfo-log parser —— HWiNFO64 CSV 日志解析核心（纯 stdlib）。"""

import csv
import datetime
import re
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
        # index: 列序号（0 起）；name: 表头原文（含 [unit]）
        # unit: 列名中 [unit] 提取的单位，无则 None
        # metric_id: 规范化指标 id（会话内唯一，§3.4）
        # kind: "numeric" | "bool" | "drop"
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
        # kind == "numeric" 的列（含 bool 列当 include_bool_columns 时）
        return [c for c in self.columns
                if c.kind == "numeric" or (c.kind == "bool" and self.include_bool)]

    @property
    def bool_columns(self) -> List[Column]:
        # kind == "bool" 的列
        return [c for c in self.columns if c.kind == "bool"]


def split_csv_line(line: str) -> List[str]:
    """引号感知切列（RFC4180 子集：双引号包裹、"" 转义；Python csv 模块实现）。
    输入行末尾去除 \r\n。返回切分后的列值列表。"""
    line = line.rstrip("\r\n")
    try:
        return next(csv.reader([line]))
    except csv.Error:
        return [line]


def parse_header(header_line: str) -> List[str]:
    """解析表头行 → 列名原文列表（去引号）。调用 split_csv_line 即可。"""
    return split_csv_line(header_line)


def detect_date_format(date_text: str) -> str:
    """auto 探测日期顺序：首个字段数值 >12 → "d.m.y"；否则默认 "d.m.y"（spec 2.3）。
    返回 "d.m.y" | "m.d.y" | "y.m.d" 之一。date_text 形如 "6.8.2026"。"""
    first = date_text.split(".", 1)[0].strip()
    if first.isdigit() and int(first) > 12:
        return "d.m.y"
    return "d.m.y"


def parse_timestamp(date_text: str, time_text: str, date_format: str) -> Optional[int]:
    """HWiNFO 时间戳 → UTC 毫秒（本地时间直读，spec D3）。
    date_format ∈ {"d.m.y","m.d.y","y.m.d"} 对应 strptime "%d.%m.%Y" / "%m.%d.%Y" / "%Y.%m.%d"；
    time_text 形如 "15:29:1.752"（秒无前导零、毫秒 0~3 位），strptime "%H:%M:%S.%f"，
    无毫秒时回退 "%H:%M:%S"。解析失败返回 None。"""
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
    连续下划线折叠 → 首尾下划线去除；与 used 冲突时追加 "_2"、"_3"... 保证唯一。
    规范化结果为空（如纯非 ASCII 列名"温度"）时兜底为 "col"（重名仍走 _2/_3 后缀）。"""
    base = _UNIT_RE.sub("", name)
    base = base.lower()
    base = _INVALID_ID_CHARS.sub("_", base)
    base = _ID_UNDERSCORE_RUN.sub("_", base)
    base = base.strip("_") or "col"
    candidate = base
    n = 2
    while candidate in used:
        candidate = "{}_{}".format(base, n)
        n += 1
    used.add(candidate)
    return candidate


def _is_bool_cell(raw: str) -> bool:
    return raw.strip().lower() in {"yes", "no", "是", "否"}


def classify_columns(header: List[str], sample_cells: List[List[str]],
                     include_bool: bool) -> HwInfoSchema:
    """前 1000 行样本判定列类型（spec 2.5）：
    - 数值列：样本中出现 ≥1 个可 float() 的值 → kind "numeric"
    - 布尔列：样本值全部 ∈ {Yes,No,是,否}（大小写不敏感；中文"是/否"来自
      HWiNFO 中文版 [Yes/No] 值区的 GBK 字节，经行级双解码恢复）→ kind "bool"
    - 其余 → kind "drop"
    include_bool=True 时布尔列并入 numeric（产出 1/0 值，spec 2.7）。
    Date/Time 两列（index 0/1）恒为 "drop"，不产出指标。"""
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


def _is_float(raw: str) -> bool:
    try:
        float(raw.strip())
    except ValueError:
        return False
    return True


def parse_row(cells: List[str], schema: HwInfoSchema, ts_ms: int,
              include_bool: bool) -> List[Dict]:
    """一行数据 → Record 列表（每数值列一条：{"timestamp","metric","value"}）。
    值解析失败（非数值文本且非 Yes/No/是/否）跳过该列并计数（调用方累计 bad 计数）。
    include_bool=True 时布尔列产出 {"timestamp","metric","value": 1|0}（中文
    "是/否"与英文 Yes/No 同逻辑）。"""
    records: List[Dict] = []
    for col in schema.columns:
        if col.kind == "drop":
            continue
        if col.kind == "bool" and not include_bool:
            continue
        raw = cells[col.index].strip()
        if col.kind == "bool":
            lower = raw.lower()
            if lower in ("yes", "是"):
                records.append({"timestamp": ts_ms, "metric": col.metric_id, "value": 1})
            elif lower in ("no", "否"):
                records.append({"timestamp": ts_ms, "metric": col.metric_id, "value": 0})
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        records.append({"timestamp": ts_ms, "metric": col.metric_id, "value": value})
    return records
