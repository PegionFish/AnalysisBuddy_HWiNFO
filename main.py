# hwinfo-log — HWiNFO64 CSV 硬件监控日志插件（deep-dive/hwinfo-plugin.md §5）。
#
# 仅经 analysisbuddy-sdk 公共 API（AnalysisBuddyPlugin 子类）与 parser.py 公共接口
# 调用，不定义任何 parser 内部逻辑。仓库根即插件目录，clone 即用：
#   pip install analysisbuddy-sdk   # 开发机一次安装
#   python main.py                  # 宿主以 plugin.json entry 拉起
#
# 能力（§5）：on_can_handle 打分（0.6 基础 + 0.2 单位后缀 + 0.1 指纹，封顶 1.0）；
# on_load_file 只做 O(表头+样本) 预扫描（§7.2 P0 纪律）：冻结 schema、first_ts 取
# 样本首行、last_ts 取尾部采样末行、row_count 按样本平均行字节估算（GB 级不击穿
# 10s 预算；精确统计推迟到 parse 期，bad_lines 由 parse 回填）；
# on_schema 全部已 load 文件列集合并集去重；on_parse 逐行产出 Record（每 500 条
# 附 raw_line、2 万行心跳按字节进度估算 percent）；on_key_values 恒空；
# 不覆写 on_annotate。

import io
import json
import os
from typing import Dict, List, Optional

from analysisbuddy import AnalysisBuddyPlugin, FileLoadFailedError

from parser import (
    classify_columns,
    detect_date_format,
    parse_header,
    parse_row,
    parse_timestamp,
    split_csv_line,
)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_DEFAULT_CONFIG = {
    "date_format": "auto",
    "encoding": "auto",
    "include_bool_columns": False,
}


class HwInfoLogPlugin(AnalysisBuddyPlugin):
    id = "hwinfo-log"
    name = "HWiNFO 硬件监控日志解析器"
    version = "0.1.0"

    def __init__(self) -> None:
        super().__init__()
        # file_id -> {"path", "schema": HwInfoSchema, "config": dict,
        #             "row_count": int(估算), "bad_lines": int(parse 后回填),
        #             "first_ts": Optional[int](样本首行), "last_ts": Optional[int](尾部采样)}
        self._files: Dict[str, Dict] = {}

    # ---- 生命周期 ------------------------------------------------------

    def on_can_handle(self, p: dict) -> dict:
        ext = p.get("ext", "")
        head = p.get("head_sample") or ""
        first_line = head.split("\n", 1)[0]
        cells = split_csv_line(first_line)
        is_hwinfo = (ext == "csv" and len(cells) >= 3
                     and cells[0].strip('"') == "Date" and cells[1].strip('"') == "Time"
                     and any("[" in c and "]" in c for c in cells))
        score = 0.6 if is_hwinfo else 0.0
        if score and any(c.strip('"').endswith("]") for c in cells):
            score += 0.2
        if score and "date,time," in head.lower():
            score += 0.1
        score = min(score, 1.0)
        result = {"can_handle": score > 0.0, "confidence": score}
        if score:
            # skip-if-empty 约定（§1.0）：reason 无值即省略键，不输出 null。
            result["reason"] = f"hwinfo csv: {len(cells)} columns, unit suffix detected"
        return result

    def on_load_file(self, p: dict) -> dict:
        path = p["path"]
        if not os.path.exists(path):
            raise FileLoadFailedError(f"file not found: {path}", data={"path": path})

        cfg = self._load_config()
        cfg["_path"] = path
        enc = _decode_name(cfg)
        header_text, sample_texts = self._read_head(path, enc)
        if (cfg["encoding"] == "auto" and enc == "utf-8"
                and self._needs_gbk_fallback(header_text, sample_texts)):
            enc = "gbk"
            header_text, sample_texts = self._read_head(path, "gbk")
        cfg["_encoding_resolved"] = enc

        header = parse_header(header_text)
        sample_cells = [split_csv_line(t) for t in sample_texts if t.strip()]
        schema = classify_columns(header, sample_cells,
                                  bool(cfg.get("include_bool_columns", False)))
        date_format = cfg.get("date_format", "auto")
        if date_format == "auto":
            date_format = detect_date_format(sample_cells[0][0]) if sample_cells else "d.m.y"
        cfg["date_format_resolved"] = date_format

        # 预扫描纪律（§7.2 P0）：不整文件计行 —— first_ts 取样本首行，
        # last_ts 取尾部采样末行，row_count 按字节/平均行字节估算（GB 级安全）。
        first_ts: Optional[int] = None
        if sample_cells:
            first_ts = parse_timestamp(sample_cells[0][0], sample_cells[0][1], date_format)
        last_ts = self._tail_last_ts(path, enc, date_format)

        file_bytes = os.path.getsize(path)
        if sample_cells:
            avg_bytes = (sum(len(t.encode(enc)) for t in sample_texts if t.strip())
                         / len(sample_cells))
        else:
            avg_bytes = 0.0
        row_count = int(file_bytes / avg_bytes) if avg_bytes > 0 else 0

        self._files[p["file_id"]] = {
            "path": path,
            "schema": schema,
            "config": cfg,
            "row_count": row_count,
            "bad_lines": 0,  # 精确统计推迟到 parse 期，结束后回填
            "first_ts": first_ts,
            "last_ts": last_ts,
        }
        summary = {"record_count_hint": row_count}
        if first_ts is not None and last_ts is not None:
            summary["time_range"] = {"start_ms": first_ts, "end_ms": last_ts}
        summary["note"] = (f"hwinfo-log: {len(schema.columns)} columns, "
                           "record_count_hint is an estimate (head/tail sampled)")
        return summary

    def on_schema(self) -> dict:
        metrics = []
        seen: set = set()
        for data in self._files.values():
            for col in data["schema"].numeric:
                if col.metric_id in seen:
                    continue
                seen.add(col.metric_id)
                m = {"id": col.metric_id, "name": col.name,
                     "aggregation": "avg",
                     "description": f"column {col.name} of HWiNFO log"}
                if col.unit:
                    m["unit"] = col.unit
                metrics.append(m)
        return {"metrics": metrics}

    def on_parse(self, file_id: str, options, ctx) -> int:
        data = self._files[file_id]
        path, schema, cfg = data["path"], data["schema"], data["config"]
        include_bool = cfg.get("include_bool_columns", False)
        total = 0
        bad = 0
        line_no = 0
        file_bytes = os.path.getsize(path)
        with open(path, "rb") as raw:
            f = io.TextIOWrapper(raw, encoding=_decode_name(cfg), errors="replace")
            next(f, None)  # 跳过表头
            for line in f:
                ctx.check_cancelled()
                line_no += 1
                cells = split_csv_line(line)
                if len(cells) != len(schema.columns):
                    bad += 1
                    continue
                ts_ms = parse_timestamp(cells[0], cells[1], cfg["date_format_resolved"])
                if ts_ms is None:
                    bad += 1
                    continue
                records = parse_row(cells, schema, ts_ms, include_bool)
                if not records:
                    bad += 1
                    continue
                if total % 500 == 0:
                    for r in records:
                        r["raw_line"] = line.rstrip("\n")
                ctx.emit_records(records)
                total += len(records)
                if line_no % 20000 == 0:
                    # 文件大小无关的字节进度估算（§7.2 P0：不依赖 load 期全量统计）
                    percent = 100.0 if not file_bytes else min(
                        100.0, 100.0 * raw.tell() / file_bytes)
                    ctx.progress(percent=percent, bytes_read=None)
        data["bad_lines"] = bad
        if bad:
            self.log("WARN", f"{path}: skipped {bad} bad line(s)")
        ctx.progress(percent=100.0, bytes_read=file_bytes)
        return total

    def on_key_values(self, file_id: str, timestamp_ms: int) -> dict:
        return {"entries": []}

    def on_unload_file(self, file_id: str) -> None:
        self._files.pop(file_id, None)

    # ---- 内部小工具 ----------------------------------------------------

    def _load_config(self) -> dict:
        """config.json（§4）：文件不存在 → 全默认；JSON 解析失败 → 全默认 + WARN；
        未知键忽略。"""
        if not os.path.exists(CONFIG_PATH):
            return dict(_DEFAULT_CONFIG)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            self.log("WARN", f"config.json unreadable ({exc}); using defaults")
            return dict(_DEFAULT_CONFIG)
        if not isinstance(data, dict):
            self.log("WARN", "config.json root is not an object; using defaults")
            return dict(_DEFAULT_CONFIG)
        result = dict(_DEFAULT_CONFIG)
        for key in _DEFAULT_CONFIG:
            if key in data:
                result[key] = data[key]
        return result

    @staticmethod
    def _read_head(path: str, enc: str):
        """读表头 + 前 1000 行样本（classify 用，§5.3 第 4 步）。"""
        with open(path, "r", encoding=enc, errors="replace") as f:
            header_text = f.readline()
            sample_texts = [f.readline() for _ in range(1000)]
        return header_text, sample_texts

    @staticmethod
    def _read_tail(path: str, enc: str, tail_bytes: int = 4096,
                   n_lines: int = 5) -> List[str]:
        """读文件尾部 tail_bytes 字节 → 拆行 → 末 n_lines 行（去空行）。
        seek 起点 clamp ≥0；丢弃块内首行（可能被截断），用已解析编码解码，
        尾部字节不完整时 errors=replace 兜底。"""
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - tail_bytes))
            raw = f.read()
        parts = raw.decode(enc, errors="replace").split("\n", 1)
        body = parts[1] if len(parts) > 1 else parts[0]
        lines = [l for l in body.split("\n") if l.strip()]
        return lines[-n_lines:]

    @staticmethod
    def _tail_last_ts(path: str, enc: str, date_format: str) -> Optional[int]:
        """尾部采样末行时间戳；末 n_lines 行都解析失败 → None（坏尾兜底）。"""
        for line in reversed(HwInfoLogPlugin._read_tail(path, enc)):
            cells = split_csv_line(line)
            if len(cells) < 2:
                continue
            ts_ms = parse_timestamp(cells[0], cells[1], date_format)
            if ts_ms is not None:
                return ts_ms
        return None

    @staticmethod
    def _needs_gbk_fallback(header_text: str, sample_texts: List[str]) -> bool:
        """表头/首行样本出现 ≥10% 替换符（U+FFFD）→ 重开 gbk（§5.3 第 3 步）。"""
        candidates = [header_text]
        if sample_texts:
            candidates.append(sample_texts[0])
        for line in candidates:
            if line and line.count("\ufffd") / len(line) >= 0.1:
                return True
        return False


def _decode_name(cfg: dict) -> str:
    """cfg -> Python 编码名（"utf-8-sig"/"utf-16"/"utf-8"/"gbk"），§5.3 内部小工具。
    load 期已解析的 _encoding_resolved 优先；"auto" 按首 3 字节 BOM 探测。"""
    resolved = cfg.get("_encoding_resolved")
    if resolved:
        return resolved
    enc = cfg.get("encoding", "auto")
    if enc == "gbk":
        return "gbk"
    if enc == "utf-8":
        return "utf-8"
    head = b""
    path = cfg.get("_path")
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                head = f.read(3)
        except OSError:
            head = b""
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    return "utf-8"


if __name__ == "__main__":
    HwInfoLogPlugin().serve()
