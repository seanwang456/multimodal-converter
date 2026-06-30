from app.errors import ConversionError, ErrorCode
from app.services import registry


def setup_function() -> None:
    registry.register_handlers()


def test_get_targets_supported() -> None:
    targets = {r["target_ext"] for r in registry.get_targets(".txt")}
    assert ".docx" in targets and ".pdf" in targets


def test_get_targets_unsupported_raises() -> None:
    try:
        registry.get_targets(".exe")
        assert False, "应抛错"
    except ConversionError as e:
        assert e.code == ErrorCode.UNSUPPORTED_FILE_TYPE


def test_resolve_unsupported_conversion() -> None:
    try:
        registry.resolve_handler_key(".txt", ".mp4")
        assert False
    except ConversionError as e:
        assert e.code == ErrorCode.UNSUPPORTED_CONVERSION


def test_self_check_passes_when_implemented() -> None:
    registry.self_check()  # 不抛即通过


def test_self_check_detects_missing_handler() -> None:
    orig = {k: list(v) for k, v in registry.CONVERSION_REGISTRY.items()}
    registry.CONVERSION_REGISTRY[".zzz"] = [
        {"target_ext": ".qqq", "handler_key": "_missing", "quality": "x"}
    ]
    try:
        registry.self_check()
        assert False, "应检测到缺失 handler"
    except ConversionError:
        pass
    finally:
        registry.CONVERSION_REGISTRY.clear()
        registry.CONVERSION_REGISTRY.update(orig)


def test_self_check_detects_supports_mismatch(monkeypatch) -> None:
    """handler 存在但 supports() 返回 False 也应被 self_check 拦截。"""
    registry.register_handlers()
    h = registry.get_handler("txt_to_docx")
    assert h is not None
    monkeypatch.setattr(h, "supports", lambda s, t: False)
    try:
        registry.self_check()
        assert False, "应检测到 supports() 不匹配"
    except ConversionError:
        pass
