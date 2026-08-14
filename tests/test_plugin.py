# H-02 插件级单测（tests/test_plugin.py）—— 覆盖 deep-dive/hwinfo-plugin.md §5
# 全部 on_* 处理器与任务卡 DoD 断言（on_can_handle 全分支 / on_load_file /
# on_schema 并集去重 / on_parse / on_key_values / on_unload_file）。

import os
from pathlib import Path

import pytest

from analysisbuddy import CancelledError, EmitContext, FileLoadFailedError

from main import HwInfoLogPlugin, decode_data_line

FIXTURES = Path(__file__).parent / "fixtures"


class RecordingContext:
    """on_parse 上下文替身：记录全部产出，SDK 有/无环境通用（仅依赖 ctx 公共 API：
    emit_records / progress / check_cancelled / cancel / records_so_far）。"""

    def __init__(self) -> None:
        self.records: list = []
        self.progress_calls: list = []
        self.cancelled = False

    @property
    def records_so_far(self) -> int:
        return len(self.records)

    def emit_records(self, records) -> None:
        self.records.extend(records)

    def progress(self, percent=None, bytes_read=None) -> None:
        self.progress_calls.append({"percent": percent, "bytes_read": bytes_read})

    def check_cancelled(self) -> None:
        if self.cancelled:
            raise CancelledError("parse cancelled")

    def cancel(self) -> None:
        self.cancelled = True


def _load(plugin, path, file_id="f1"):
    return plugin.on_load_file({"path": str(path), "file_id": file_id})


# ---------- on_can_handle（§5.2，DoD 全分支） ----------

def test_can_handle_abstain_when_ext_not_csv():
    plugin = HwInfoLogPlugin()
    r = plugin.on_can_handle({"ext": "log", "head_sample": 'Date,Time,"CPU Usage [%]"'})
    # skip-if-empty 约定（§1.0）：reason 无值即省略键，不输出 null。
    assert r == {"can_handle": False, "confidence": 0.0}
    assert "reason" not in r


def test_can_handle_060_basic_hwinfo_signature():
    plugin = HwInfoLogPlugin()
    r = plugin.on_can_handle({"ext": "csv",
                              "head_sample": '"Date","Time","CPU Usage [%] Hz"'})
    assert r["can_handle"] is True
    assert r["confidence"] == 0.6


def test_can_handle_080_with_unit_suffix():
    plugin = HwInfoLogPlugin()
    r = plugin.on_can_handle({"ext": "csv", "head_sample": '"Date","Time","CPU Usage [%]"'})
    assert r["confidence"] == 0.8


def test_can_handle_090_with_unit_suffix_and_fingerprint():
    plugin = HwInfoLogPlugin()
    r = plugin.on_can_handle({"ext": "csv", "head_sample": 'Date,Time,"CPU Usage [%]"'})
    assert r["can_handle"] is True
    assert r["confidence"] == 0.9


def test_can_handle_confidence_capped_at_one(monkeypatch):
    import main as main_module

    plugin = HwInfoLogPlugin()
    seen = []

    def fake_min(a, b):
        seen.append((a, b))
        return min(a, b)

    monkeypatch.setattr(main_module, "min", fake_min, raising=False)
    r = plugin.on_can_handle({"ext": "csv", "head_sample": 'Date,Time,"CPU Usage [%]"'})
    assert (0.9, 1.0) in seen
    assert r["confidence"] == 0.9
    assert r["confidence"] <= 1.0


def test_can_handle_bom_truncated_head_claims():
    """真实中文版 head_sample = 前 4096 字节（UTF-8 BOM + 截断的超长表头，无换行符，
    cells[0] 带 \ufeff）→ 前缀判据认领（剥 BOM 后以 Date,Time, 开头）。"""
    plugin = HwInfoLogPlugin()
    head = "\ufeffDate,Time,\"提交虚拟内存 [MB]\",\"可用虚拟内存 [MB]\"," + "x" * 4500
    r = plugin.on_can_handle({"ext": "csv", "head_sample": head[:4096]})
    assert r["can_handle"] is True
    assert r["confidence"] == 0.9  # 0.6 前缀 + 0.2 单位后缀 + 0.1 指纹子串
    assert "reason" in r


def test_can_handle_bom_full_header_line():
    """BOM + 完整首行（中文列名）→ 前缀判据认领。"""
    plugin = HwInfoLogPlugin()
    head = "\ufeff" + 'Date,Time,"CPU 温度 [℃]",核心过热降频 [Yes/No]'
    r = plugin.on_can_handle({"ext": "csv", "head_sample": head})
    assert r["can_handle"] is True
    assert r["confidence"] == 0.9


# ---------- decode_data_line（行级双解码，§5.3 混合编码文件） ----------

def test_decode_data_line_utf8_primary_falls_back_to_gbk():
    """主编码 utf-8、值区为 GBK 字节（是=0xCA 0xC7）→ 整行 gbk 重解码恢复中文。"""
    raw = ("15.7.2025,17:40:11.073,92.0,".encode("ascii")
           + "是".encode("gbk") + b"," + "是".encode("gbk") + ",85.0".encode("ascii"))
    expected = "15.7.2025,17:40:11.073,92.0,是,是,85.0"
    assert decode_data_line(raw, "utf-8") == expected
    assert decode_data_line(raw, "utf-8-sig") == expected  # utf-8-sig 系同样走 gbk 兜底


def test_decode_data_line_utf8_primary_pure_utf8_unchanged():
    raw = "15.7.2025,17:40:11.073,92.0,85.0".encode("utf-8")
    assert decode_data_line(raw, "utf-8") == "15.7.2025,17:40:11.073,92.0,85.0"


def test_decode_data_line_gbk_primary_direct():
    raw = "温度 [°C]".encode("gbk")
    assert decode_data_line(raw, "gbk") == "温度 [°C]"


def test_decode_data_line_utf8_sig_strips_bom():
    raw = b"\xef\xbb\xbf" + 'Date,Time,"CPU 温度 [℃]"'.encode("utf-8")
    assert decode_data_line(raw, "utf-8-sig") == 'Date,Time,"CPU 温度 [℃]"'


def test_decode_data_line_utf16_primary_direct():
    raw = "15.7.2025".encode("utf-16")
    assert decode_data_line(raw, "utf-16") == "15.7.2025"


# ---------- on_load_file（§5.3，DoD） ----------

def test_load_sample_schema_frozen_hint_range_note():
    plugin = HwInfoLogPlugin()
    summary = _load(plugin, FIXTURES / "hwinfo_sample.csv")
    # record_count_hint 为字节估算（int(文件字节 / 样本平均行字节)），非精确值
    assert summary["record_count_hint"] == 30
    tr = summary["time_range"]
    assert 1_000_000_000_000 < tr["start_ms"] < 2_000_000_000_000
    assert tr["start_ms"] <= tr["end_ms"]
    assert summary["note"] == (
        "hwinfo-log: 523 columns, record_count_hint is an estimate (head/tail sampled)")

    data = plugin._files["f1"]
    assert data["path"] == str(FIXTURES / "hwinfo_sample.csv")
    assert data["row_count"] == 30  # 估算（真实 24 行；精确统计在 parse 期）
    assert data["bad_lines"] == 0  # load 期不再计坏行，parse 结束后回填
    assert 1_000_000_000_000 < data["first_ts"] < 2_000_000_000_000
    assert data["last_ts"] >= data["first_ts"]
    assert len(data["schema"].columns) == 523
    assert data["schema"].columns[0].kind == "drop"
    assert data["schema"].columns[1].kind == "drop"
    assert len(data["schema"].numeric) > 100
    assert data["config"]["date_format_resolved"] == "d.m.y"


def test_load_malformed_estimate_and_bad_lines_deferred():
    plugin = HwInfoLogPlugin()
    summary = _load(plugin, FIXTURES / "hwinfo_malformed.csv")
    data = plugin._files["f1"]
    assert data["row_count"] == 10  # 字节估算（真实 8 行：5 好 + 3 坏）
    assert data["bad_lines"] == 0  # 坏行统计推迟到 parse 期
    assert summary["record_count_hint"] == 10
    assert summary["note"] == (
        "hwinfo-log: 6 columns, record_count_hint is an estimate (head/tail sampled)")
    # 时间范围仍由样本首行 + 尾部采样给出
    assert summary["time_range"]["start_ms"] == data["first_ts"]
    assert summary["time_range"]["end_ms"] == data["last_ts"]


def test_load_missing_file_raises_file_load_failed():
    plugin = HwInfoLogPlugin()
    with pytest.raises(FileLoadFailedError) as ei:
        plugin.on_load_file({"path": "no/such/file.csv", "file_id": "f1"})
    assert ei.value.data["path"] == "no/such/file.csv"


def test_load_gbk_auto_fallback_to_gbk():
    plugin = HwInfoLogPlugin()
    summary = _load(plugin, FIXTURES / "hwinfo_gbk.csv")
    data = plugin._files["f1"]
    assert data["config"]["_encoding_resolved"] == "gbk"
    assert data["row_count"] == 4  # 字节估算（真实 3 行）
    assert data["bad_lines"] == 0
    assert summary["record_count_hint"] == 4
    assert data["schema"].columns[3].name == "温度 [°C]"


def test_load_explicit_date_format_mdy(tmp_path, monkeypatch):
    csv_file = tmp_path / "mdy.csv"
    csv_file.write_text(
        'Date,Time,"A [W]"\n'
        "6.13.2026,15:29:1.752,1.5\n"
        "6.13.2026,15:29:2.771,2.5\n",
        encoding="utf-8",
    )
    cfg = tmp_path / "config.json"
    cfg.write_text('{"date_format": "m.d.y", "encoding": "utf-8",'
                   ' "include_bool_columns": false}', encoding="utf-8")
    monkeypatch.setattr("main.CONFIG_PATH", str(cfg))
    plugin = HwInfoLogPlugin()
    _load(plugin, csv_file)
    data = plugin._files["f1"]
    assert data["config"]["date_format_resolved"] == "m.d.y"
    assert data["row_count"] == 2  # 字节估算恰好等于真实行数
    assert data["bad_lines"] == 0
    assert data["first_ts"] < data["last_ts"]


def test_load_include_bool_merges_into_numeric(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"date_format": "auto", "encoding": "auto",'
                   ' "include_bool_columns": true}', encoding="utf-8")
    monkeypatch.setattr("main.CONFIG_PATH", str(cfg))
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_bool.csv")
    schema = plugin._files["f1"]["schema"]
    assert len(schema.bool_columns) == 1
    assert len(schema.numeric) == 3  # 2 数值列 + 1 布尔列并入


def test_load_corrupt_config_falls_back_to_defaults(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "config.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr("main.CONFIG_PATH", str(bad))
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_malformed.csv")
    cfg = plugin._files["f1"]["config"]
    assert cfg["date_format"] == "auto"
    assert cfg["encoding"] == "auto"
    assert cfg["include_bool_columns"] is False
    assert "WARN" in capsys.readouterr().err


# ---------- on_schema（§5.4，DoD 并集去重 / 空） ----------

def test_schema_union_deduplicated_across_files():
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_sample.csv", file_id="f1")
    _load(plugin, FIXTURES / "hwinfo_sample.csv", file_id="f2")
    schema = plugin.on_schema()
    ids = [m["id"] for m in schema["metrics"]]
    assert len(ids) == len(set(ids))
    assert len(schema["metrics"]) == len(plugin._files["f1"]["schema"].numeric)

    first_col = plugin._files["f1"]["schema"].numeric[0]
    first = schema["metrics"][0]
    assert first["id"] == first_col.metric_id == "virtual_memory_committed"
    assert first["name"] == "Virtual Memory Committed [MB]"
    assert first["aggregation"] == "avg"
    assert first["description"] == "column Virtual Memory Committed [MB] of HWiNFO log"
    assert first["unit"] == "MB"

    for m in schema["metrics"]:
        assert m["aggregation"] == "avg"
        assert m["description"].endswith(" of HWiNFO log")


def test_schema_empty_when_no_files_loaded():
    plugin = HwInfoLogPlugin()
    assert plugin.on_schema() == {"metrics": []}


# ---------- on_parse（§5.5，DoD records_total / raw_line / cancel） ----------

def test_parse_sample_records_total():
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_sample.csv")
    expected = 24 * len(plugin._files["f1"]["schema"].numeric)
    ctx = RecordingContext()
    total = plugin.on_parse("f1", None, ctx)
    assert total == expected
    assert ctx.records_so_far == expected
    assert ctx.progress_calls[-1] == {
        "percent": 100.0,
        "bytes_read": os.path.getsize(str(FIXTURES / "hwinfo_sample.csv")),
    }


def test_parse_sample_with_real_sdk_context():
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_sample.csv")
    expected = 24 * len(plugin._files["f1"]["schema"].numeric)
    sent = []
    ctx = EmitContext("f1", lambda kind, params: sent.append((kind, params)))
    total = plugin.on_parse("f1", None, ctx)
    assert total == expected
    assert ctx.records_so_far == expected
    assert any(kind == "progress" and params.get("percent") == 100.0
               for kind, params in sent)


def test_parse_malformed_skips_bad_lines(capsys):
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_malformed.csv")
    numeric = len(plugin._files["f1"]["schema"].numeric)
    ctx = RecordingContext()
    total = plugin.on_parse("f1", None, ctx)
    assert total == 5 * numeric
    assert "skipped 3 bad line(s)" in capsys.readouterr().err
    assert plugin._files["f1"]["bad_lines"] == 3  # load 期挂账，parse 结束后回填


def test_parse_raw_line_sampling_every_500(tmp_path):
    rows = 1200
    csv_file = tmp_path / "raw_line.csv"
    lines = ['Date,Time,"A [W]"']
    for i in range(rows):
        lines.append("6.8.2026,15:29:{0}.1,{0}.5".format(i % 60))
    csv_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    plugin = HwInfoLogPlugin()
    _load(plugin, csv_file)
    ctx = RecordingContext()
    total = plugin.on_parse("f1", None, ctx)
    assert total == rows  # 每行 1 条 Record

    marked = [r for r in ctx.records if "raw_line" in r]
    assert len(marked) == 3  # total == 0 / 500 / 1000 处各一批
    assert ctx.records[0]["raw_line"].startswith("6.8.2026,15:29:0.")
    assert ctx.records[500]["raw_line"].startswith("6.8.2026,15:29:")
    assert "raw_line" not in ctx.records[501]
    assert "raw_line" not in ctx.records[1199]


def test_parse_cancelled_raises_cancelled_error():
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_sample.csv")
    ctx = RecordingContext()
    ctx.cancel()
    with pytest.raises(CancelledError):
        plugin.on_parse("f1", None, ctx)


# ---------- load 预扫描：尾部采样 / 字节估算 / 字节进度（§7.2 P0） ----------

def test_load_tail_sampling_last_ts_from_file_end(tmp_path):
    """末行超出 1000 行样本窗与 4096 字节头部时，last_ts 仍取真实末行（尾部采样）。"""
    from parser import parse_timestamp

    rows = 3000
    csv_file = tmp_path / "tail.csv"
    lines = ['Date,Time,"A [W]"']
    for i in range(rows):
        m, s = divmod(i, 60)
        lines.append("6.8.2026,10:{0:02d}:{1:02d}.1,1.5".format(m, s))
    csv_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    plugin = HwInfoLogPlugin()
    summary = _load(plugin, csv_file)
    data = plugin._files["f1"]
    first_ts = parse_timestamp("6.8.2026", "10:00:00.1", "d.m.y")
    last_ts = parse_timestamp("6.8.2026", "10:49:59.1", "d.m.y")
    assert data["first_ts"] == first_ts  # 样本首行
    assert data["last_ts"] == last_ts  # 尾部采样末行（≠ 样本窗末行 10:16:39）
    assert summary["time_range"] == {"start_ms": first_ts, "end_ms": last_ts}


def test_load_record_count_hint_is_byte_estimate(tmp_path):
    """hint = int(文件字节 / 样本平均行字节)，与真实行数同量级（估算语义）。"""
    rows = 1000
    line = "6.8.2026,10:00:1.752,1.5\n"
    csv_file = tmp_path / "estimate.csv"
    # 二进制写入：规避 Windows 文本模式 \n → \r\n 的换行翻译，保证字节数可预期
    csv_file.write_bytes(('Date,Time,"A [W]"\n' + line * rows).encode("utf-8"))

    plugin = HwInfoLogPlugin()
    summary = _load(plugin, csv_file)
    size = os.path.getsize(str(csv_file))
    avg = len(line.encode("utf-8"))
    expected = int(size / avg)
    assert summary["record_count_hint"] == expected
    assert "estimate" in summary["note"]
    assert plugin._files["f1"]["row_count"] == expected
    assert abs(expected - rows) <= 2  # 头部一行不影响量级


def test_parse_heartbeat_percent_by_bytes(tmp_path):
    """2 万行心跳 percent 按字节进度估算：前半短行（小字节）处心跳远小于 50%，
    而按行数估算会恰好 50% —— 区分字节进度与行数进度的语义。"""
    short = "6.8.2026,10:00:0.1,1.5\n"
    long = "6.8.2026,10:00:0.1,1.5000000000000000000000000000000000001\n"
    csv_file = tmp_path / "bytes_progress.csv"
    csv_file.write_text('Date,Time,"A [W]"\n' + short * 20000 + long * 20000,
                        encoding="utf-8")

    plugin = HwInfoLogPlugin()
    _load(plugin, csv_file)
    ctx = RecordingContext()
    total = plugin.on_parse("f1", None, ctx)
    assert total == 40000

    mid = [c for c in ctx.progress_calls if c["percent"] is not None]
    assert any(20.0 < c["percent"] < 45.0 for c in mid)  # 字节进度 ≈33%，行数进度=50%
    assert ctx.progress_calls[-1]["percent"] == 100.0


# ---------- on_key_values / on_unload_file（§5.6，DoD） ----------

def test_key_values_always_empty():
    plugin = HwInfoLogPlugin()
    assert plugin.on_key_values("f1", 123) == {"entries": []}


def test_unload_file_pops_state_and_schema_resets():
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_sample.csv")
    assert plugin.on_schema()["metrics"]
    plugin.on_unload_file("f1")
    assert "f1" not in plugin._files
    assert plugin.on_schema() == {"metrics": []}
    plugin.on_unload_file("f1")  # 幂等，不抛错


# ---------- 真实中文版特征回归（tests/fixtures/hwinfo_zh_mixed.csv） ----------

def test_read_tail_default_window_covers_long_tail_line():
    """默认 64KB 尾部窗口在超长"传感器名"汇总行（33KB、600+ 逗号）存在时仍能
    取到数据行（4096 字节窗口会被该行整个吃掉 → last_ts=None）。"""
    lines = HwInfoLogPlugin._read_tail(str(FIXTURES / "hwinfo_zh_mixed.csv"), "utf-8-sig")
    assert lines[-1].startswith("top,")  # 传感器汇总行仍在窗口内
    assert any(l.startswith("15.7.2025") for l in lines)  # 窗口内含数据行


def test_load_zh_mixed_bom_gbk_bools_and_time_range():
    """BOM + 中文表头 + GBK [Yes/No] 值区 + 点分隔日期 + 超长尾部传感器行：
    schema 列数与表头一致、布尔列分类正确、time_range 不因尾部行缺失。"""
    from parser import parse_timestamp

    plugin = HwInfoLogPlugin()
    summary = _load(plugin, FIXTURES / "hwinfo_zh_mixed.csv")
    data = plugin._files["f1"]
    assert data["config"]["_encoding_resolved"] == "utf-8-sig"  # BOM 文件，非整文件 gbk
    schema = data["schema"]
    assert [c.kind for c in schema.columns] == [
        "drop", "drop", "numeric", "bool", "bool", "numeric"]
    assert len(schema.numeric) == 2  # include_bool 默认 false，布尔列不并入
    first = parse_timestamp("15.7.2025", "17:40:11.073", "d.m.y")
    last = parse_timestamp("15.7.2025", "17:40:13.081", "d.m.y")
    assert data["first_ts"] == first
    assert data["last_ts"] == last  # 超长尾部传感器行不阻塞尾部采样
    assert summary["time_range"] == {"start_ms": first, "end_ms": last}


def test_parse_zh_mixed_include_bool_chinese_ones_and_zeros(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"date_format": "auto", "encoding": "auto",'
                   ' "include_bool_columns": true}', encoding="utf-8")
    monkeypatch.setattr("main.CONFIG_PATH", str(cfg))
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_zh_mixed.csv")
    assert len(plugin._files["f1"]["schema"].numeric) == 4  # 2 数值 + 2 布尔并入
    ctx = RecordingContext()
    total = plugin.on_parse("f1", None, ctx)
    assert total == 12  # 3 行 × 4 指标列
    by_metric: dict = {}
    for r in ctx.records:
        by_metric.setdefault(r["metric"], []).append(r["value"])
    bool_seqs = [tuple(vs) for vs in by_metric.values() if all(v in (0, 1) for v in vs)]
    assert sorted(bool_seqs) == [(1, 0, 1), (1, 1, 0)]  # 是→1 / 否→0 逐行映射
    assert len({r["timestamp"] for r in ctx.records}) == 3


def test_parse_zh_mixed_default_drops_bool_no_extra_bad_lines(capsys):
    plugin = HwInfoLogPlugin()
    _load(plugin, FIXTURES / "hwinfo_zh_mixed.csv")
    ctx = RecordingContext()
    total = plugin.on_parse("f1", None, ctx)
    assert total == 6  # 3 行 × 2 数值列；布尔列默认丢弃（设计行为）
    assert plugin._files["f1"]["bad_lines"] == 1  # 仅超长传感器汇总行，GBK 值行不坏
    assert "skipped 1 bad line(s)" in capsys.readouterr().err
    assert all(r["value"] in (85.0, 85.5, 86.0, 91.0, 92.0, 93.5) for r in ctx.records)
