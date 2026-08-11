# H-01 parser 单测（tests/test_parser.py）—— 覆盖 deep-dive §3 全部公共函数。

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser import (
    Column,
    HwInfoSchema,
    classify_columns,
    detect_date_format,
    normalize_metric_id,
    parse_header,
    parse_row,
    parse_timestamp,
    split_csv_line,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- split_csv_line ----------

def test_split_quoted_column_with_comma():
    assert split_csv_line('"a,b",c') == ["a,b", "c"]


def test_split_escaped_quotes():
    assert split_csv_line('"a""b""c",d') == ['a"b"c', "d"]


def test_split_plain_columns():
    assert split_csv_line("a,b,c") == ["a", "b", "c"]


def test_split_empty_columns():
    assert split_csv_line("a,,c,") == ["a", "", "c", ""]


def test_split_strips_trailing_newline():
    assert split_csv_line("a,b\r\n") == ["a", "b"]


# ---------- parse_header ----------

def test_parse_header_dequotes():
    header = 'Date,Time,"Virtual Memory Committed [MB]",X'
    assert parse_header(header) == ["Date", "Time", "Virtual Memory Committed [MB]", "X"]


# ---------- detect_date_format ----------

def test_detect_first_field_leq_12_defaults_dmy():
    assert detect_date_format("6.8.2026") == "d.m.y"


def test_detect_first_field_gt_12_dmy():
    assert detect_date_format("25.8.2026") == "d.m.y"


# ---------- parse_timestamp ----------

def test_parse_timestamp_dmy_with_ms():
    ts = parse_timestamp("6.8.2026", "15:29:1.752", "d.m.y")
    assert ts == 1786030141752


def test_parse_timestamp_dmy_magnitude():
    ts = parse_timestamp("6.8.2026", "15:29:1.752", "d.m.y")
    assert 1_000_000_000_000 < ts < 2_000_000_000_000
    assert abs(ts - 1785601200000) < 10 * 24 * 60 * 60 * 1000


def test_parse_timestamp_mdy():
    assert parse_timestamp("8.6.2026", "15:29:1", "m.d.y") == 1786030141000


def test_parse_timestamp_ymd():
    assert parse_timestamp("2026.8.6", "15:29:1", "y.m.d") == 1786030141000


def test_parse_timestamp_no_ms_fallback():
    assert parse_timestamp("6.8.2026", "15:29:1", "d.m.y") == 1786030141000


def test_parse_timestamp_bad_time_returns_none():
    assert parse_timestamp("6.8.2026", "15:99:1.752", "d.m.y") is None


def test_parse_timestamp_bad_date_returns_none():
    assert parse_timestamp("32.8.2026", "15:29:1", "d.m.y") is None


def test_parse_timestamp_unknown_format_returns_none():
    assert parse_timestamp("6.8.2026", "15:29:1", "x.y.z") is None


# ---------- normalize_metric_id ----------

def test_normalize_unit_suffix_stripped():
    used: set = set()
    assert normalize_metric_id("Virtual Memory Committed [MB]", used) == "virtual_memory_committed"


def test_normalize_dash_and_case():
    used: set = set()
    assert normalize_metric_id("P-core 0 Voltage [V]", used) == "p_core_0_voltage"


def test_normalize_duplicate_gets_suffix():
    used = {"a"}
    assert normalize_metric_id("A [W]", used) == "a_2"
    assert normalize_metric_id("A [V]", used) == "a_3"


def test_normalize_non_ascii_falls_back_to_col():
    used: set = set()
    assert normalize_metric_id("温度 [°C]", used) == "col"
    assert normalize_metric_id("电流 [A]", used) == "col_2"


# ---------- classify_columns ----------

def test_classify_date_time_always_drop():
    header = ["Date", "Time", "CPU Usage [%]", "Fan Present [Yes/No]", "Version"]
    sample = [
        ["6.8.2026", "15:29:1.752", "37.3", "Yes", "v1.2"],
        ["6.8.2026", "15:29:2.771", "35.2", "No", "v1.2"],
    ]
    schema = classify_columns(header, sample, include_bool=False)
    assert [c.kind for c in schema.columns] == ["drop", "drop", "numeric", "bool", "drop"]


def test_classify_numeric_bool_text_three_way():
    header = ["Date", "Time", "A [W]", "B [Yes/No]", "C"]
    sample = [
        ["6.8.2026", "15:29:1.752", "1.5", "Yes", "text"],
        ["6.8.2026", "15:29:2.771", "2.5", "No", "other"],
        ["6.8.2026", "15:29:3.756", "3.0", "yes", "again"],
    ]
    schema = classify_columns(header, sample, include_bool=False)
    kinds = {c.name: c.kind for c in schema.columns}
    assert kinds["A [W]"] == "numeric"
    assert kinds["B [Yes/No]"] == "bool"
    assert kinds["C"] == "drop"


def test_classify_bool_case_insensitive():
    header = ["Date", "Time", "Flag [Yes/No]"]
    sample = [["6.8.2026", "15:29:1.752", "YES"], ["6.8.2026", "15:29:2.771", "no"]]
    schema = classify_columns(header, sample, include_bool=False)
    assert schema.columns[2].kind == "bool"


def test_classify_include_bool_merges_into_numeric():
    header = ["Date", "Time", "A [W]", "B [Yes/No]"]
    sample = [["6.8.2026", "15:29:1.752", "1.5", "Yes"]]
    schema = classify_columns(header, sample, include_bool=True)
    assert [c.kind for c in schema.numeric] == ["numeric", "bool"]
    assert [c.kind for c in schema.bool_columns] == ["bool"]


def test_classify_metric_ids_unique_and_units():
    header = ["Date", "Time", "A [W]", "A [V]"]
    sample = [["6.8.2026", "15:29:1.752", "1.5", "2.0"]]
    schema = classify_columns(header, sample, include_bool=False)
    ids = [c.metric_id for c in schema.columns]
    assert ids == ["date", "time", "a", "a_2"]
    assert schema.columns[2].unit == "W"
    assert schema.columns[3].unit == "V"
    assert schema.columns[0].unit is None


# ---------- parse_row ----------

def _schema(header, sample, include_bool=False):
    return classify_columns(header, sample, include_bool=include_bool)


def test_parse_row_normal():
    header = ["Date", "Time", "A [W]", "B [V]"]
    schema = _schema(header, [["6.8.2026", "15:29:1.752", "1.5", "2.0"]])
    rows = parse_row(["6.8.2026", "15:29:1.752", "3.5", "4.25"], schema, 1786030141752, False)
    assert rows == [
        {"timestamp": 1786030141752, "metric": "a", "value": 3.5},
        {"timestamp": 1786030141752, "metric": "b", "value": 4.25},
    ]


def test_parse_row_bool_ones_and_zeros():
    header = ["Date", "Time", "Flag [Yes/No]", "A [W]"]
    schema = _schema(header, [["6.8.2026", "15:29:1.752", "Yes", "1.5"]], include_bool=True)
    rows = parse_row(["6.8.2026", "15:29:1.752", "No", "2.0"], schema, 1786030141752, True)
    assert rows == [
        {"timestamp": 1786030141752, "metric": "flag", "value": 0},
        {"timestamp": 1786030141752, "metric": "a", "value": 2.0},
    ]


def test_parse_row_bool_excluded_when_not_included():
    header = ["Date", "Time", "Flag [Yes/No]", "A [W]"]
    schema = _schema(header, [["6.8.2026", "15:29:1.752", "Yes", "1.5"]], include_bool=False)
    rows = parse_row(["6.8.2026", "15:29:1.752", "Yes", "2.0"], schema, 1786030141752, False)
    assert rows == [{"timestamp": 1786030141752, "metric": "a", "value": 2.0}]


def test_parse_row_non_numeric_skipped():
    header = ["Date", "Time", "A [W]", "B [V]"]
    schema = _schema(header, [["6.8.2026", "15:29:1.752", "1.5", "2.0"]])
    rows = parse_row(["6.8.2026", "15:29:1.752", "N/A", "2.0"], schema, 1786030141752, False)
    assert rows == [{"timestamp": 1786030141752, "metric": "b", "value": 2.0}]


# ---------- fixtures ----------

def test_fixture_sample_shape():
    lines = (FIXTURES / "hwinfo_sample.csv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 25
    cells = split_csv_line(lines[0])
    assert cells[0] == "Date" and cells[1] == "Time"
    assert any("[" in c and "]" in c for c in cells)
    for data_line in lines[1:]:
        assert split_csv_line(data_line)[0].count(".") == 2


def test_fixture_sample_parses_records():
    lines = (FIXTURES / "hwinfo_sample.csv").read_text(encoding="utf-8").splitlines()
    header = parse_header(lines[0])
    sample = [split_csv_line(l) for l in lines[1:25]]
    schema = classify_columns(header, sample, include_bool=False)
    assert schema.columns[0].kind == "drop" and schema.columns[1].kind == "drop"
    assert len(schema.numeric) > 100
    total = 0
    for l in lines[1:]:
        cells = split_csv_line(l)
        ts = parse_timestamp(cells[0], cells[1], "d.m.y")
        assert ts is not None
        total += len(parse_row(cells, schema, ts, False))
    assert total == len(lines[1:]) * len(schema.numeric)


def test_fixture_malformed_counts():
    lines = (FIXTURES / "hwinfo_malformed.csv").read_text(encoding="utf-8").splitlines()
    header = parse_header(lines[0])
    assert len(header) == 6
    schema = classify_columns(header, [split_csv_line(l) for l in lines[1:]], False)
    bad = 0
    good = 0
    for l in lines[1:]:
        cells = split_csv_line(l)
        if len(cells) != len(schema.columns):
            bad += 1
            continue
        ts = parse_timestamp(cells[0], cells[1], "d.m.y")
        if ts is None:
            bad += 1
            continue
        good += 1
    assert bad == 3
    assert good == 5


def test_fixture_bool_has_yes_no_column():
    lines = (FIXTURES / "hwinfo_bool.csv").read_text(encoding="utf-8").splitlines()
    header = parse_header(lines[0])
    sample = [split_csv_line(l) for l in lines[1:]]
    assert any("[Yes/No]" in name for name in header)
    schema = classify_columns(header, sample, include_bool=False)
    assert len(schema.bool_columns) == 1
    assert len(sample) == 8


def test_fixture_gbk_is_gbk_encoded():
    raw = (FIXTURES / "hwinfo_gbk.csv").read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf"
    text = raw.decode("gbk")
    header = parse_header(text.splitlines()[0])
    assert header[3] == "温度 [°C]"
