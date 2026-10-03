"""Lazy OCR engine registry with optional persistent process isolation."""

from __future__ import annotations

import importlib
import multiprocessing
import traceback
from dataclasses import dataclass
from typing import Any

from bookocr.core.types import EngineResult, PageImage, Region


def _resolve_class(class_path: str):
    module_name, class_name = class_path.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _worker_main(connection, class_path: str, config: dict) -> None:
    try:
        engine = _resolve_class(class_path)(config)
        connection.send({"ready": True, "name": engine.name, "version": engine.version})
    except BaseException:
        connection.send({"ready": False, "error": traceback.format_exc()})
        connection.close()
        return

    while True:
        try:
            message = connection.recv()
        except EOFError:
            break
        if message.get("command") == "close":
            shutdown = getattr(importlib.import_module(class_path.split(":", 1)[0]), "shutdown", None)
            if shutdown is not None:
                shutdown()
            connection.send({"closed": True})
            break
        if message.get("command") != "recognize":
            connection.send({"ok": False, "error": "unknown worker command"})
            continue
        try:
            result = engine.recognize(message["page"], message.get("regions"))
            connection.send({"ok": True, "result": result})
        except BaseException:
            connection.send({"ok": False, "error": traceback.format_exc()})
    connection.close()


@dataclass(frozen=True)
class EngineSpec:
    name: str
    class_path: str


class EngineRunner:
    name: str
    version: str

    def recognize(self, page: PageImage, regions: list[Region] | None = None) -> EngineResult:
        raise NotImplementedError

    def close(self) -> None:
        return None


class InProcessEngineRunner(EngineRunner):
    def __init__(self, spec: EngineSpec, config: dict):
        self._engine = _resolve_class(spec.class_path)(config)
        self.name = self._engine.name
        self.version = self._engine.version

    def recognize(self, page: PageImage, regions: list[Region] | None = None) -> EngineResult:
        return self._engine.recognize(page, regions)

    def close(self) -> None:
        module = importlib.import_module(self._engine.__class__.__module__)
        shutdown = getattr(module, "shutdown", None)
        if shutdown is not None:
            shutdown()


class ProcessEngineRunner(EngineRunner):
    def __init__(self, spec: EngineSpec, config: dict, *, start_method: str = "spawn", timeout_s: float = 900):
        context = multiprocessing.get_context(start_method)
        parent, child = context.Pipe()
        self._connection = parent
        self._timeout_s = timeout_s
        self._process = context.Process(target=_worker_main, args=(child, spec.class_path, config), daemon=True)
        self._process.start()
        child.close()
        if not parent.poll(timeout_s):
            self._terminate()
            raise TimeoutError(f"Timed out starting OCR worker {spec.name!r}")
        ready = parent.recv()
        if not ready.get("ready"):
            self._terminate()
            raise RuntimeError(f"OCR worker {spec.name!r} failed to start:\n{ready.get('error', 'unknown error')}")
        self.name = ready["name"]
        self.version = ready["version"]

    def recognize(self, page: PageImage, regions: list[Region] | None = None) -> EngineResult:
        self._connection.send({"command": "recognize", "page": page, "regions": regions})
        if not self._connection.poll(self._timeout_s):
            self._terminate()
            raise TimeoutError(f"OCR worker {self.name!r} exceeded {self._timeout_s:g}s")
        response = self._connection.recv()
        if not response.get("ok"):
            raise RuntimeError(f"OCR worker {self.name!r} failed:\n{response.get('error', 'unknown error')}")
        return response["result"]

    def _terminate(self) -> None:
        if getattr(self, "_process", None) is not None and self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5)

    def close(self) -> None:
        if not self._process.is_alive():
            return
        try:
            self._connection.send({"command": "close"})
            if self._connection.poll(10):
                self._connection.recv()
        finally:
            self._process.join(timeout=10)
            self._terminate()
            self._connection.close()


class EngineRegistry:
    def __init__(self):
        self._specs: dict[str, EngineSpec] = {}
        self._runners: list[EngineRunner] = []

    @classmethod
    def with_builtins(cls) -> "EngineRegistry":
        registry = cls()
        registry.register(EngineSpec("paddleocr", "bookocr.engines.paddle_engine:PaddleOCREngine"))
        registry.register(EngineSpec("qari-ocr", "bookocr.engines.qari_engine:QariOCREngine"))
        return registry

    def register(self, spec: EngineSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"OCR engine {spec.name!r} is already registered")
        self._specs[spec.name] = spec

    def create(self, name: str, config: dict, execution: dict, *, escalation: bool = False) -> EngineRunner:
        try:
            spec = self._specs[name]
        except KeyError as error:
            raise ValueError(f"Unknown OCR engine {name!r}; available: {sorted(self._specs)}") from error
        mode = execution.get("mode", "process")
        if mode == "in_process":
            runner: EngineRunner = InProcessEngineRunner(spec, config)
        elif mode == "process":
            timeout_key = "escalation_timeout_s" if escalation else "primary_timeout_s"
            runner = ProcessEngineRunner(
                spec,
                config,
                start_method=execution.get("start_method", "spawn"),
                timeout_s=float(execution.get(timeout_key, 3600 if escalation else 900)),
            )
        else:
            raise ValueError(f"Unknown OCR execution mode {mode!r}")
        self._runners.append(runner)
        return runner

    def close_all(self) -> None:
        while self._runners:
            self._runners.pop().close()
