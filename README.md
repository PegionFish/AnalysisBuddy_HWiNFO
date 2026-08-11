# AnalysisBuddy_HWiNFO —— HWiNFO 硬件监控日志解析插件

AnalysisBuddy 第三方插件：解析 HWiNFO64 导出的 CSV 硬件监控日志（`Date,Time,<列名 [单位]>...` 格式），
把每个数值列逐行产出为时序 Record（timestamp/metric/value），供宿主聚合展示。

## 运行依赖

- Python 3.10 ~ 3.14（纯 stdlib，插件本体零第三方依赖）
- `analysisbuddy-sdk`：开发机一次安装

  ```powershell
  pip install analysisbuddy-sdk
  ```

- 插件由宿主拉起：宿主按 `plugin.json` 的 `entry`（`python main.py`）启动插件进程，
  经 stdio 走 AnalysisBuddy 插件协议（load_file / schema / parse / key_values…）。
  无需手动运行；本地联调可直接 `python main.py`。

## 仓库布局

```
plugin.json          # manifest（id: hwinfo-log，extensions: csv，指纹 "Date,Time,"）
config.json          # 插件私有配置（见下）
parser.py            # 解析核心：表头切分 / 时间戳 / 列分类 / 行解析（纯 stdlib）
main.py              # 插件主体：on_can_handle / on_load_file / on_schema / on_parse …
tests/               # pytest 单测（parser + 插件级；无 SDK 环境自动注入替身）
```

## 插件私有配置（config.json）

| 键 | 取值 | 默认 | 语义 |
|----|------|------|------|
| `date_format` | `"auto"` / `"d.m.y"` / `"m.d.y"` / `"y.m.d"` | `"auto"` | 日期字段顺序；auto 按首字段数值探测（>12 → d.m.y，否则 d.m.y） |
| `encoding` | `"auto"` / `"utf-8"` / `"gbk"` | `"auto"` | 文件编码；auto：BOM 探测 → UTF-8 宽松解码，表头/首行样本 ≥10% 替换符则回退 GBK |
| `include_bool_columns` | `true` / `false` | `false` | false：`[Yes/No]` 布尔列 drop；true：产出 1/0 数值 |

配置文件不存在或 JSON 解析失败时使用全默认值（stderr 输出 WARN）；未知键忽略。
修改后需重新 load 文件生效。

## 后续格式更新维护指引

- **换表头自适应**：解析完全以每次 load 时的表头行为准 —— 表头列名变化、增删列、
  列顺序调整均无需改代码；`classify_columns` 重新判定各列数值/布尔/文本并冻结 schema。
  列数不足的坏行与时间解析失败行在 load/parse 期自动跳过并计数（note / WARN 报告）。
- **config 扩展点**：新增行为优先加在 `config.json`（在 `main.py` `_DEFAULT_CONFIG` 与
  `_load_config` 登记新键，解析逻辑保持在 `parser.py` 公共接口内），保持
  `main.py` 只做编排、`parser.py` 只做解析的分层。
- **时间戳语义**：HWiNFO 本地时间直读转 UTC 毫秒（不做时区换算），跨时区机器比对时注意。
- **协议升级**：manifest 的 `min_protocol_version` 与 SDK 能力探测保持一致；新增宿主能力
  （如 annotate）需同步更新 `plugin.json` 与 README。

## 测试

```powershell
python -m pytest tests/ -q
```

SDK 未安装时 conftest 自动注入 `tests/analysisbuddy_stub.py` 替身；`parser.py` 未落地前
自动注入 `tests/parser_stub.py`，保证插件测试可独立运行。
