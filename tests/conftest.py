# pytest 夹具：把插件仓库根加入 sys.path（仓库根即插件目录，deep-dive §1）。
# SDK 未安装时注入 analysisbuddy_stub 替身；parser 不可导入时注入 parser_stub
#（H-01 落地前自测用，签名不变）。H-01/SDK 落地后自动走真实实现。

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

if importlib.util.find_spec("analysisbuddy") is None:
    import analysisbuddy_stub  # noqa: F401  注册 sys.modules["analysisbuddy"]
if importlib.util.find_spec("parser") is None:
    import parser_stub  # noqa: F401  注册 sys.modules["parser"]


@pytest.fixture
def plugin():
    from main import HwInfoLogPlugin

    return HwInfoLogPlugin()
