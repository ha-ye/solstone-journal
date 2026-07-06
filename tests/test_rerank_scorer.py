# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 sol pbc

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing

from solstone.think.indexer import rerank_scorer
from solstone.think.providers import rerank_install

MODEL_PATH = "onnx/model.onnx"
TOKENIZER_PATH = "tokenizer.json"
FIXTURE_REPO = "Xenova/ms-marco-MiniLM-L-6-v2"
FIXTURE_REVISION = "a09144355adeed5f58c8ed011d209bf8ee5a1fec"


@pytest.fixture(autouse=True)
def _reset_scorer_state():
    _reset_scorer()
    yield
    _reset_scorer()


def _reset_scorer() -> None:
    rerank_scorer._session = None
    rerank_scorer._tokenizer = None
    rerank_scorer._np = None
    rerank_scorer._disabled = False


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fixture_model(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    inputs = [
        helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"]),
        helper.make_tensor_value_info(
            "attention_mask", TensorProto.INT64, ["batch", "seq"]
        ),
        helper.make_tensor_value_info(
            "token_type_ids", TensorProto.INT64, ["batch", "seq"]
        ),
    ]
    output = helper.make_tensor_value_info("logits", TensorProto.FLOAT, ["batch", 1])
    nodes = [
        helper.make_node("Cast", ["input_ids"], ["ids_f"], to=TensorProto.FLOAT),
        helper.make_node("Cast", ["attention_mask"], ["mask_f"], to=TensorProto.FLOAT),
        helper.make_node("Cast", ["token_type_ids"], ["types_f"], to=TensorProto.FLOAT),
        helper.make_node("Add", ["ids_f", "mask_f"], ["ids_mask_f"]),
        helper.make_node("Add", ["ids_mask_f", "types_f"], ["combined_f"]),
        helper.make_node(
            "ReduceMean", ["combined_f"], ["logits"], axes=[1], keepdims=1
        ),
    ]
    model = helper.make_model(
        helper.make_graph(nodes, "rerank_fixture", inputs, [output]),
        opset_imports=[helper.make_opsetid("", 13)],
    )
    model.ir_version = 10
    onnx.checker.check_model(model)
    onnx.save(model, path)


def _write_fixture_tokenizer(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tokenizer = Tokenizer(
        WordLevel(
            vocab={
                "[PAD]": 0,
                "[UNK]": 1,
                "[CLS]": 2,
                "[SEP]": 3,
                "query": 4,
                "doc": 5,
                "one": 6,
                "two": 7,
                "three": 8,
            },
            unk_token="[UNK]",
        )
    )
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.post_processor = TemplateProcessing(
        single="[CLS]:0 $A:0 [SEP]:0",
        pair="[CLS]:0 $A:0 [SEP]:0 $B:1 [SEP]:1",
        special_tokens=[("[CLS]", 2), ("[SEP]", 3)],
    )
    tokenizer.save(str(path))


def _fixture_spec(
    model_path: Path, tokenizer_path: Path
) -> rerank_install.RerankModelSpec:
    return rerank_install.RerankModelSpec(
        repo=FIXTURE_REPO,
        revision=FIXTURE_REVISION,
        files=(
            rerank_install.RerankFileSpec(
                path=MODEL_PATH,
                sha256=_sha256(model_path),
                size_bytes=model_path.stat().st_size,
            ),
            rerank_install.RerankFileSpec(
                path=TOKENIZER_PATH,
                sha256=_sha256(tokenizer_path),
                size_bytes=tokenizer_path.stat().st_size,
            ),
        ),
    )


def _stage_fixture_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> rerank_install.RerankModelSpec:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    model_path = (
        tmp_path / "cache" / "providers" / "rerank" / FIXTURE_REVISION / MODEL_PATH
    )
    tokenizer_path = (
        tmp_path / "cache" / "providers" / "rerank" / FIXTURE_REVISION / TOKENIZER_PATH
    )
    _write_fixture_model(model_path)
    _write_fixture_tokenizer(tokenizer_path)
    spec = _fixture_spec(model_path, tokenizer_path)
    monkeypatch.setattr(rerank_install, "RERANK_MODEL_SPEC", spec)
    return spec


class _Input:
    def __init__(self, name: str) -> None:
        self.name = name


class _Encoding:
    ids = [2, 4, 3, 5, 3]
    attention_mask = [1, 1, 1, 1, 1]
    type_ids = [0, 0, 0, 1, 1]


class _FakeTokenizer:
    def encode_batch(self, pairs):
        return [_Encoding() for _pair in pairs]


def test_real_session_scores_deterministically(tmp_path, monkeypatch) -> None:
    _stage_fixture_assets(tmp_path, monkeypatch)

    result = rerank_scorer.score(
        "query one",
        ["doc two", "doc one two three"],
    )

    assert result == pytest.approx([4.444444656, 6.444444656])


def test_missing_assets_fail_closed_and_latch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SOLSTONE_JOURNAL", str(tmp_path))
    spec = rerank_install.RerankModelSpec(
        repo=FIXTURE_REPO,
        revision=FIXTURE_REVISION,
        files=(
            rerank_install.RerankFileSpec(
                path=MODEL_PATH,
                sha256="0" * 64,
                size_bytes=1,
            ),
            rerank_install.RerankFileSpec(
                path=TOKENIZER_PATH,
                sha256="1" * 64,
                size_bytes=1,
            ),
        ),
    )
    monkeypatch.setattr(rerank_install, "RERANK_MODEL_SPEC", spec)
    calls = 0
    original_load = rerank_scorer._load

    def spy_load():
        nonlocal calls
        calls += 1
        return original_load()

    monkeypatch.setattr(rerank_scorer, "_load", spy_load)

    assert rerank_scorer.score("query", ["doc"]) is None
    assert rerank_scorer.score("query", ["doc"]) is None
    assert calls == 1
    assert rerank_scorer._disabled is True


def test_inference_exception_fails_closed_and_latches(monkeypatch) -> None:
    class FailingSession:
        def get_inputs(self):
            return [_Input("input_ids")]

        def run(self, *_args: Any, **_kwargs: Any):
            raise RuntimeError("inference broke")

    calls = 0

    def fake_load():
        nonlocal calls
        calls += 1
        return _FakeTokenizer(), FailingSession(), np

    monkeypatch.setattr(rerank_scorer, "_load", fake_load)

    assert rerank_scorer.score("query", ["doc"]) is None
    assert rerank_scorer.score("query", ["doc"]) is None
    assert calls == 1
    assert rerank_scorer._disabled is True


def test_unexpected_output_shape_fails_closed_and_latches(monkeypatch) -> None:
    class BadShapeSession:
        def get_inputs(self):
            return [_Input("input_ids")]

        def run(self, _outputs: Any, feed: dict[str, Any]):
            return [np.zeros((feed["input_ids"].shape[0], 2), dtype=np.float32)]

    monkeypatch.setattr(
        rerank_scorer,
        "_load",
        lambda: (_FakeTokenizer(), BadShapeSession(), np),
    )

    assert rerank_scorer.score("query", ["doc"]) is None
    assert rerank_scorer.score("query", ["doc"]) is None
    assert rerank_scorer._disabled is True


def test_scoring_path_never_calls_installer_or_downloader(
    tmp_path, monkeypatch
) -> None:
    _stage_fixture_assets(tmp_path, monkeypatch)
    monkeypatch.setattr(
        rerank_install,
        "_download_file",
        lambda *_args, **_kwargs: pytest.fail("scoring should not download"),
    )
    monkeypatch.setattr(
        rerank_install,
        "install_rerank_model",
        lambda *_args, **_kwargs: pytest.fail("scoring should not install"),
    )

    assert rerank_scorer.score("query one", ["doc two"]) is not None
    _reset_scorer()
    missing_spec = rerank_install.RerankModelSpec(
        repo=FIXTURE_REPO,
        revision="missing-revision",
        files=rerank_install.RERANK_MODEL_SPEC.files,
    )
    monkeypatch.setattr(rerank_install, "RERANK_MODEL_SPEC", missing_spec)
    assert rerank_scorer.score("query", ["doc"]) is None


def test_mocked_session_batches_and_feeds_declared_subset(monkeypatch) -> None:
    class RecordingSession:
        def __init__(self) -> None:
            self.feeds: list[dict[str, Any]] = []

        def get_inputs(self):
            return [_Input("input_ids")]

        def run(self, _outputs: Any, feed: dict[str, Any]):
            self.feeds.append(feed)
            assert set(feed) == {"input_ids"}
            assert feed["input_ids"].dtype == np.int64
            batch = feed["input_ids"].shape[0]
            return [np.arange(batch, dtype=np.float32).reshape(batch, 1)]

    session = RecordingSession()
    monkeypatch.setattr(
        rerank_scorer,
        "_load",
        lambda: (_FakeTokenizer(), session, np),
    )

    result = rerank_scorer.score("query", [f"doc {i}" for i in range(17)])

    assert result is not None
    assert len(result) == 17
    assert [feed["input_ids"].shape[0] for feed in session.feeds] == [16, 1]
