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
plugin.json          # manifest（id: hwinfo-log，extensions: csv，双指纹 "Date,Time," / "Date","Time"）
config.json          # 插件私有配置（见下）
parser.py            # 解析核心：表头切分 / 时间戳 / 列分类 / 行解析（纯 stdlib）
main.py              # 插件主体：on_can_handle / on_load_file / on_schema / on_parse …
tests/               # pytest 单测（parser + 插件级；无 SDK 环境自动注入替身）
scripts/pack.ps1     # 发布包打包脚本（zip 根含 plugin.json + 内嵌自检 + SHA256SUMS）
.github/workflows/   # CI：semver tag 触发 Release 发布管线
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
- **load 预扫描纪律（大文件）**：load 只做 O(表头+样本) 预扫描 —— 前 1000 行样本冻结
  schema、尾部采样末 5 行估 `last_ts`、`record_count_hint` 按样本平均行字节估算，
  不整文件计行（GB 级也不击穿 10s 预算）；坏行数精确统计推迟到 parse 期（WARN 报告，
  load 返回的 note 只标注 hint 为估算）。
- **config 扩展点**：新增行为优先加在 `config.json`（在 `main.py` `_DEFAULT_CONFIG` 与
  `_load_config` 登记新键，解析逻辑保持在 `parser.py` 公共接口内），保持
  `main.py` 只做编排、`parser.py` 只做解析的分层。
- **时间戳语义**：HWiNFO 本地时间直读转 UTC 毫秒（不做时区换算），跨时区机器比对时注意。
- **协议升级**：manifest 的 `min_protocol_version` 与 SDK 能力探测保持一致；新增宿主能力
  （如 annotate）需同步更新 `plugin.json` 与 README。

## 场景预设（presets）

本插件是动态 schema 插件（指标集由本机硬件决定，跨机器不可移植），presets 采用
「`want` 语义槽 + 宽候选族 + `keywords` 兜底」写法（manifest 字段逐字按宿主 schema）：

- `want` 只做语义槽声明，**首个命中**即吸收硬件差异（如独显/集显共存时
  `GPU Clock [MHz]` 重名 → `gpu_clock_2` 后缀候选）；
- 候选族同时写规范化 metric_id 与原始表头串双通道（如 `cpu_package_temperature` /
  `CPU Package [°C]`）；
- `keywords` 是整预设完全失配时的最后防线，不承担精确匹配职责。

当前内置 2 个预设：`cpu-temp-monitor`（核心温度监控，含内存/存储分组示范）与
`gpu-monitor`（GPU 监控）。后续新增预设沿用同一写法。

## 打包与发布

发布包必须以「plugin.json 位于 zip 根」形态分发（宿主安装管线要求根目录直接是
plugin.json，GitHub "Download ZIP" 源码包不合规），用 `scripts/pack.ps1` 打包：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/pack.ps1
# 产出 dist/AnalysisBuddy_hwinfo-log_v<version>.zip + dist/SHA256SUMS.txt
```

脚本只收白名单文件（plugin.json/main.py/parser.py/config.json/README.md/LICENSE），
自动排除 .git/tests/.github/scripts/__pycache__；并内嵌自检：zip 条目无绝对路径与
`..` 越界、根含 plugin.json、zip 内 id/version 与仓库根一致。发布流程：

1. 版本改动同步更新 `plugin.json` 的 `version` 与 `changelog`，提交推送到 main；
2. 打 tag 并推送（tag 与 version 严格一致，如 `v0.2.0`）：

   ```powershell
   git tag v0.2.0
   git push origin v0.2.0
   ```

3. `.github/workflows/release.yml` 自动：checkout → pack.ps1（校验 tag 与 version
   一致）→ 上传 zip + SHA256SUMS 为 Release 资产（GitHub Actions 需该仓启用）。

## 更新链路

- manifest 已声明 `update_url`（GitHub 仓库全 URL），宿主更新链路
  （`check_plugin_update` → 最新 Release tag 转 semver 比较）据此检查新版本；
- 每个 Release 必须且只能附**一个 zip 资产**（更新管线按唯一资产下载），zip 内
  id/version 与 tag 自洽且严格递增；
- 升级由宿主侧发起，插件自身不主动联网。

## 数据纪律（NDA）

工作场景可能涉及 NDA 工具，插件进程无文件系统沙箱（宿主 G7 缺口），靠本纪律约束：

- **插件数据只写插件自身目录**：config.json 等私有数据一律只落在插件安装目录内，
  不读写任何外部路径；
- **卸载即清除**：宿主卸载流程删除整个插件目录，数据随插件彻底移除 —— 无需保留的
  中间数据不要散落在系统其他位置；
- **升级需自迁移**：宿主升级安装会先删旧目录再解压新包，**config.json 会被覆盖删除**
  （宿主暂无可插拔迁移钩子）—— 升级前如有需要保留的配置，请先备份，或在新版本中
  把配置迁移逻辑放进 `_load_config` 的默认值路径。

## 测试

```powershell
python -m pytest tests/ -q
```

SDK 未安装时 conftest 自动注入 `tests/analysisbuddy_stub.py` 替身；`parser.py` 未落地前
自动注入 `tests/parser_stub.py`，保证插件测试可独立运行。
