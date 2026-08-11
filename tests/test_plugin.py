# H-02 插件级单测（tests/test_plugin.py）—— 覆盖 deep-dive/hwinfo-plugin.md §5
# 全部 on_* 处理器与任务卡 DoD 断言（on_can_handle 全分支 / on_load_file /
# on_schema 并集去重 / on_parse / on_key_values / on_unload_file）。

import os
from pathlib import Path

import pytest

from analysisbuddy import CancelledError, EmitContext, FileLoadFailedError

from main import HwInfoLogPlugin

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
    assert r == {"can_handle": False, "confidence": 0.0, "reason": None}


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


# ---------- on_load_file（§5.3，DoD） ----------

def test_load_sample_schema_frozen_hint_range_note():
    plugin = HwInfoLogPlugin()
    summary = _load(plugin, FIXTURES / "hwinfo_sample.csv")
    assert summary["record_count_hint"] == 24
    tr = summary["time_range"]
    assert 1_000_000_000_000 < tr["start_ms"] < 2_000_000_000_000
    assert tr["start_ms"] <= tr["end_ms"]
    assert summary["note"] == "hwinfo-log: 523 columns, 0 bad lines skipped"

    data = plugin._files["f1"]
    assert data["path"] == str(FIXTURES / "hwinfo_sample.csv")
    assert data["row_count"] == 24
    assert data["bad_lines"] == 0
    assert 1_000_000_000_000 < data["first_ts"] < 2_000_000_000_000
    assert data["last_ts"] >= data["first_ts"]
    assert len(data["schema"].columns) == 523
    assert data["schema"].columns[0].kind == "drop"
    assert data["schema"].columns[1].kind == "drop"
    assert len(data["schema"].numeric) > 100
    assert data["config"]["date_format_resolved"] == "d.m.y"


def test_load_malformed_counts_bad_lines():
    plugin = HwInfoLogPlugin()
    summary = _load(plugin, FIXTURES / "hwinfo_malformed.csv")
    data = plugin._files["f1"]
    assert data["row_count"] == 5
    assert data["bad_lines"] == 3
    assert summary["record_count_hint"] == 5
    assert summary["note"] == "hwinfo-log: 6 columns, 3 bad lines skipped"


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
    assert data["row_count"] == 3
    assert data["bad_lines"] == 0
    assert summary["record_count_hint"] == 3
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
    assert data["row_count"] == 2
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
