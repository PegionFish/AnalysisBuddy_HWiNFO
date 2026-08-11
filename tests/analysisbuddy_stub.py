# analysisbuddy SDK 开发期替身（conftest 注入，仅当正式 SDK 未安装时启用）。
#
# sdk/python（D1 路）尚未安装的环境里，tests 用本文件镜像正式 SDK 的公共 API
# 契约（sdk-plugins.md §1.2/§1.4），使 hwinfo-log 测试可独立运行；正式 SDK 已安装
# （pip install -e sdk/python）时本文件不生效，测试走真实 SDK（真 dogfood）。
# 签名与正式 SDK 对齐：EmitContext(file_id, sender, batch_size=4000, ...)。

import sys
import types
import typing


class EmitContext:
    """开发期替身：记录 sender 通知，供测试断言（契约见 sdk-plugins.md §1.4）。"""

    def __init__(self, file_id: str, sender: typing.Callable[[str, dict], None],
                 batch_size: int = 4000, heartbeat_interval: float = 2.0,
                 stderr=None) -> None:
        self._file_id = file_id
        self._sender = sender
        self.batch_size = batch_size
        self._buffer: typing.List[dict] = []
        self._seq = 0
        self._records_so_far = 0
        self._cancelled = False

    def emit_records(self, records: typing.List[dict]) -> None:
        for record in records:
            self._buffer.append(record)
            self._records_so_far += 1
        if len(self._buffer) >= self.batch_size:
            self._flush()

    def progress(self, percent: typing.Optional[float] = None,
                 bytes_read: typing.Optional[int] = None) -> None:
        params: dict = {"file_id": self._file_id, "records_so_far": self._records_so_far}
        if percent is not None:
            params["percent"] = percent
        if bytes_read is not None:
            params["bytes_read"] = bytes_read
        self._sender("progress", params)

    def check_cancelled(self) -> None:
        if self._cancelled:
            raise CancelledError("parse cancelled")

    def cancel(self) -> None:
        self._cancelled = True

    def _flush(self) -> None:
        self._sender("RecordBatch", {
            "file_id": self._file_id,
            "seq": self._seq,
            "records": self._buffer,
            "done": False,
        })
        self._seq += 1
        self._buffer = []


class AnalysisBuddyError(Exception):
    def __init__(self, message: str, data: typing.Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data


class FileLoadFailedError(AnalysisBuddyError):
    pass


class ParseFailedError(AnalysisBuddyError):
    pass


class CancelledError(AnalysisBuddyError):
    pass


class InvalidParamsError(AnalysisBuddyError):
    pass


class AnalysisBuddyPlugin:
    """公共 API 契约镜像（sdk-plugins.md §1.2）。子类覆写 on_* 方法。"""

    id: str = ""
    name: str = ""
    version: str = "0.1.0"

    def __init__(self, id: typing.Optional[str] = None, name: typing.Optional[str] = None,
                 version: typing.Optional[str] = None) -> None:
        self._handlers: dict = {}
        self._stderr = sys.stderr
        if id is not None:
            self.id = id
        if name is not None:
            self.name = name
        if version is not None:
            self.version = version

    def log(self, level: str, msg: str) -> None:
        print(f"{level}|{self.id}|{msg}", file=self._stderr)

    def on_initialize(self, params: dict) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "capabilities": {
                "annotate": type(self).on_annotate is not AnalysisBuddyPlugin.on_annotate,
                "subscribe": False,
                "binary_sidecar": False,
            },
        }

    def on_can_handle(self, params: dict) -> dict:
        return {"can_handle": False, "confidence": 0.0}

    def on_load_file(self, params: dict) -> dict:
        return {}

    def on_parse(self, file_id: str, options: typing.Optional[dict], ctx: EmitContext) -> int:
        return 0

    def on_schema(self) -> dict:
        return {"metrics": []}

    def on_key_values(self, file_id: str, timestamp_ms: int) -> dict:
        return {"entries": []}

    def on_annotate(self, file_id: str, range: dict) -> dict:
        return {"events": []}

    def on_unload_file(self, file_id: str) -> None:
        return None

    def serve(self, stdin=None, stdout=None) -> None:
        raise NotImplementedError("serve() 由正式 SDK 提供；开发期测试直接调用 on_* 方法")


def _install_stub() -> None:
    if sys.modules.get("analysisbuddy") is not None:
        return
    module = types.ModuleType("analysisbuddy")
    module.AnalysisBuddyPlugin = AnalysisBuddyPlugin
    module.EmitContext = EmitContext
    module.FileLoadFailedError = FileLoadFailedError
    module.ParseFailedError = ParseFailedError
    module.CancelledError = CancelledError
    module.InvalidParamsError = InvalidParamsError
    sys.modules["analysisbuddy"] = module


_install_stub()
