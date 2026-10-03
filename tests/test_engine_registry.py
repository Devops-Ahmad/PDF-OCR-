from bookocr.core.engine_registry import EngineRegistry, EngineSpec
from bookocr.core.types import EngineResult, PageImage, Region


class FakeEngine:
    name = "fake"
    version = "test-1"

    def __init__(self, config):
        self.text = config.get("text", "نص تجريبي")

    def recognize(self, page, regions=None):
        region = Region(kind="body", bbox=(0, 0, page.width, page.height), text=self.text, reading_order=0)
        return EngineResult(self.text, 99.0, self.name, self.version, regions=[region], model_revision="fixture")


def _registry():
    registry = EngineRegistry()
    registry.register(EngineSpec("fake", "test_engine_registry:FakeEngine"))
    return registry


def _page():
    return PageImage("book", 1, "/does/not/matter", 100, 200, 300)


def test_registry_runs_engine_in_process():
    registry = _registry()
    runner = registry.create("fake", {"text": "داخل العملية"}, {"mode": "in_process"})
    assert runner.recognize(_page()).text == "داخل العملية"
    registry.close_all()


def test_registry_runs_engine_in_persistent_child_process():
    registry = _registry()
    runner = registry.create(
        "fake",
        {"text": "داخل worker"},
        {"mode": "process", "start_method": "spawn", "primary_timeout_s": 30},
    )
    assert runner.recognize(_page()).text == "داخل worker"
    assert runner.recognize(_page()).engine_version == "test-1"
    registry.close_all()
