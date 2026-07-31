from __future__ import annotations

import copy
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.extract_cases as extraction
from scripts.extraction_schema import FIELD_NAMES, EvidenceField
from scripts.extract_cases import (
    ExtractionError,
    extract_file,
    extract_pdf_pages,
    main,
    parse_model_json,
    process_directory,
    validate_case,
)


SOURCE = "2026-07/record.pdf"
DIGEST = "a" * 64
MODEL = "test-model"


def null_field() -> dict[str, object]:
    return {
        "value": None,
        "page": None,
        "quote_snippet": None,
        "confidence": None,
    }


def payload_with(field_name: str, field: dict[str, object]) -> dict[str, object]:
    payload = {name: null_field() for name in FIELD_NAMES}
    payload[field_name] = field
    return payload


def valid_field(value: object = "北投路一段") -> dict[str, object]:
    return {
        "value": value,
        "page": 1,
        "quote_snippet": "地址：北投路一段",
        "confidence": "high",
    }


@pytest.mark.parametrize(
    "text",
    [
        '{"case_number": null}',
        '```json\n{"case_number": null}\n```',
        ' \n```json\n{"case_number": null}\n```\n ',
    ],
)
def test_parse_model_json_accepts_plain_json_or_one_outer_json_fence(text: str) -> None:
    assert parse_model_json(text) == {"case_number": None}


@pytest.mark.parametrize(
    "text",
    [
        '{"case_number": null} trailing',
        '{"case_number": null} {"address": null}',
        '{"case_number": null, "case_number": {}}',
        '```json\n{"case_number": null}\n```\nexplanation',
        '```json\n{"case_number": null}\n```\n```json\n{}\n```',
    ],
)
def test_parse_model_json_rejects_trailing_prose_or_multiple_objects(text: str) -> None:
    with pytest.raises(ValueError, match="model JSON"):
        parse_model_json(text)


def test_validate_case_preserves_valid_evidence_and_normalizes_string_value() -> None:
    payload = payload_with("case_number", valid_field("  Ａ－１  "))

    case, failures = validate_case(
        payload,
        ["案件Ａ－１之地址：北投路一段。"],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert case.review_status == "pending"
    assert case.fields["case_number"] == EvidenceField(
        value="A-1",
        page=1,
        quote_snippet="地址：北投路一段",
        confidence="high",
    )
    assert failures == []


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"page": None}, "page_required"),
        ({"page": True}, "page_required"),
        ({"page": 2}, "page_out_of_range"),
        ({"quote_snippet": "   "}, "quote_required"),
        ({"quote_snippet": "地址：不存在"}, "quote_not_exact"),
        ({"confidence": "certain"}, "invalid_confidence"),
        ({"confidence": ["high"]}, "invalid_confidence"),
        ({"value": "   "}, "empty_value"),
    ],
)
def test_invalid_evidence_nulls_whole_field_and_appends_fixed_failure(
    mutation: dict[str, object], reason: str
) -> None:
    field = valid_field()
    field.update(mutation)

    case, failures = validate_case(
        payload_with("address", field),
        ["地址：北投路一段"],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert case.fields["address"] == EvidenceField.null()
    assert [failure.to_dict() for failure in failures] == [
        {"source_pdf": SOURCE, "field": "address", "reason": reason}
    ]


@pytest.mark.parametrize("value", [-1, True, "2"])
def test_invalid_tree_count_nulls_field(value: object) -> None:
    case, failures = validate_case(
        payload_with("tree_count", valid_field(value)),
        ["地址：北投路一段"],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert case.fields["tree_count"] == EvidenceField.null()
    assert failures[0].reason == "invalid_tree_count"


@pytest.mark.parametrize("value", ["2026-02-30", "2026-7-1", 20260701])
def test_invalid_meeting_date_nulls_field(value: object) -> None:
    case, failures = validate_case(
        payload_with("meeting_date", valid_field(value)),
        ["地址：北投路一段"],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert case.fields["meeting_date"] == EvidenceField.null()
    assert failures[0].reason == "invalid_meeting_date"


def test_null_value_requires_page_quote_and_confidence_all_null() -> None:
    invalid_null = null_field()
    invalid_null["page"] = 1

    case, failures = validate_case(
        payload_with("decision", invalid_null),
        ["同意移植"],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert case.fields["decision"] == EvidenceField.null()
    assert failures[0].reason == "invalid_null_contract"


def test_quote_over_500_unicode_characters_nulls_field_without_truncation() -> None:
    quote = "證" * 501
    page = ("前" * 200) + quote + ("後" * 200)
    field = valid_field("北投路一段")
    field["quote_snippet"] = quote

    case, failures = validate_case(
        payload_with("address", field),
        [page],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert case.fields["address"] == EvidenceField.null()
    assert failures[0].reason == "quote_too_long"
    assert quote not in json.dumps(case.to_dict(), ensure_ascii=False)


def test_quote_at_500_unicode_characters_is_allowed_when_narrow_evidence() -> None:
    quote = "證" * 500
    page = ("前" * 200) + quote + ("後" * 200)
    field = valid_field("北投路一段")
    field["quote_snippet"] = quote

    case, failures = validate_case(
        payload_with("address", field),
        [page],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert failures == []
    assert case.fields["address"].quote_snippet == quote


@pytest.mark.parametrize(
    ("page", "quote"),
    [
        ("地址：北投路一段", "地址：北投路一段"),
        (("甲" * 90) + ("乙" * 10), "甲" * 90),
    ],
)
def test_full_or_near_full_page_quote_nulls_field(page: str, quote: str) -> None:
    field = valid_field("北投路一段")
    field["quote_snippet"] = quote

    case, failures = validate_case(
        payload_with("address", field),
        [page],
        SOURCE,
        DIGEST,
        MODEL,
    )

    assert case.fields["address"] == EvidenceField.null()
    assert failures[0].reason == "quote_too_broad"


@pytest.mark.parametrize("change", ["missing", "unknown"])
def test_nonexact_root_field_set_nulls_all_fields(change: str) -> None:
    payload = {name: null_field() for name in FIELD_NAMES}
    if change == "missing":
        del payload["address"]
    else:
        payload["unexpected"] = null_field()

    case, failures = validate_case(payload, ["page"], SOURCE, DIGEST, MODEL)

    assert all(field == EvidenceField.null() for field in case.fields.values())
    assert [failure.reason for failure in failures] == ["invalid_field_set"]
    assert failures[0].field == "__root__"


def test_malformed_field_shape_is_isolated_to_that_field() -> None:
    payload = {name: null_field() for name in FIELD_NAMES}
    malformed = copy.deepcopy(payload)
    malformed["decision"] = {"value": "同意移植"}

    case, failures = validate_case(malformed, ["同意移植"], SOURCE, DIGEST, MODEL)

    assert case.fields["decision"] == EvidenceField.null()
    assert failures[0].reason == "invalid_field_shape"
    assert json.dumps(case.to_dict(), ensure_ascii=False)


class FakePage:
    def __init__(self, text: str | None) -> None:
        self.text = text

    def extract_text(self) -> str | None:
        return self.text


def install_reader(
    monkeypatch: pytest.MonkeyPatch,
    texts: list[str | None],
    *,
    encrypted: bool = False,
) -> None:
    class FakeReader:
        def __init__(self, _path: Path) -> None:
            self.is_encrypted = encrypted
            self.pages = [FakePage(text) for text in texts]

    monkeypatch.setattr(extraction, "PdfReader", FakeReader, raising=False)


def test_text_pdf_skips_ocr_and_preserves_readable_page_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "record.pdf"
    pdf.write_text("fake", encoding="utf-8")
    install_reader(monkeypatch, ["  第一頁\n文字  ", "第二頁"])

    def forbidden_runner(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("OCR must not run for a text PDF")

    assert extract_pdf_pages(pdf, runner=forbidden_runner) == ["  第一頁\n文字  ", "第二頁"]


def test_blank_pages_run_argument_list_ocr_only_for_blanks_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "record.pdf"
    pdf.write_text("fake", encoding="utf-8")
    install_reader(monkeypatch, ["PDF 第一頁", "   ", None])
    calls: list[tuple[list[str], dict[str, object]]] = []
    temporary_directory: Path | None = None

    def runner(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal temporary_directory
        calls.append((command, kwargs))
        if command[0] == "pdftoppm":
            prefix = Path(command[-1])
            temporary_directory = prefix.parent
            for page in range(1, 4):
                prefix.with_name(f"{prefix.name}-{page}.png").write_text("image", encoding="utf-8")
            return SimpleNamespace(stdout="")
        page = Path(command[1]).stem.rsplit("-", 1)[-1]
        return SimpleNamespace(stdout=f"OCR 第{page}頁")

    pages = extract_pdf_pages(pdf, runner=runner)

    assert pages == ["PDF 第一頁", "OCR 第2頁", "OCR 第3頁"]
    assert [command[0] for command, _kwargs in calls] == ["pdftoppm", "tesseract", "tesseract"]
    assert calls[0][0][1:5] == ["-png", "-r", "200", str(pdf)]
    assert [Path(command[1]).name for command, _kwargs in calls[1:]] == [
        "page-2.png",
        "page-3.png",
    ]
    assert all(
        kwargs["check"] is True
        and kwargs["capture_output"] is True
        and kwargs["text"] is True
        and 0 < int(kwargs["timeout"]) <= 180
        and "shell" not in kwargs
        for _command, kwargs in calls
    )
    assert temporary_directory is not None and not temporary_directory.exists()


@pytest.mark.parametrize(("page_count", "width"), [(12, 2), (101, 3)])
def test_ocr_maps_zero_padded_pdftoppm_outputs_for_large_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    page_count: int,
    width: int,
) -> None:
    pdf = tmp_path / "record.pdf"
    pdf.write_text("fake", encoding="utf-8")
    texts = [f"PDF page {page}" for page in range(1, page_count + 1)]
    texts[1] = ""
    install_reader(monkeypatch, texts)
    tesseract_images: list[str] = []

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "pdftoppm":
            prefix = Path(command[-1])
            for page in range(1, page_count + 1):
                image = prefix.with_name(f"{prefix.name}-{page:0{width}d}.png")
                image.write_text("image", encoding="utf-8")
            return SimpleNamespace(stdout="")
        tesseract_images.append(Path(command[1]).name)
        return SimpleNamespace(stdout="OCR page 2")

    pages = extract_pdf_pages(pdf, runner=runner)

    assert pages[1] == "OCR page 2"
    assert tesseract_images == [f"page-{2:0{width}d}.png"]


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "extra", "malformed", "directory"],
)
def test_ocr_rejects_incomplete_ambiguous_or_extra_pdftoppm_page_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    pdf = tmp_path / "record.pdf"
    pdf.write_text("fake", encoding="utf-8")
    page_count = 12
    texts = [f"PDF page {page}" for page in range(1, page_count + 1)]
    texts[1] = ""
    install_reader(monkeypatch, texts)

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[0] == "tesseract":
            return SimpleNamespace(stdout="must not accept invalid mapping")
        prefix = Path(command[-1])
        pages = range(1, page_count + 1)
        for page in pages:
            if mutation == "missing" and page == 7:
                continue
            prefix.with_name(f"{prefix.name}-{page}.png").write_text(
                "image",
                encoding="utf-8",
            )
        if mutation == "duplicate":
            prefix.with_name(f"{prefix.name}-02.png").write_text("image", encoding="utf-8")
        elif mutation == "extra":
            prefix.with_name(f"{prefix.name}-13.png").write_text("image", encoding="utf-8")
        elif mutation == "malformed":
            prefix.with_name(f"{prefix.name}-x.png").write_text("image", encoding="utf-8")
        elif mutation == "directory":
            image = prefix.with_name(f"{prefix.name}-7.png")
            image.unlink()
            image.mkdir()
        return SimpleNamespace(stdout="")

    with pytest.raises(ExtractionError, match="OCR failed"):
        extract_pdf_pages(pdf, runner=runner)


def test_ocr_error_is_fixed_safe_and_temporary_images_are_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "secret-name.pdf"
    pdf.write_text("top-secret-page", encoding="utf-8")
    install_reader(monkeypatch, [""])
    temporary_directory: Path | None = None

    def runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal temporary_directory
        if command[0] == "pdftoppm":
            prefix = Path(command[-1])
            temporary_directory = prefix.parent
            prefix.with_name(f"{prefix.name}-1.png").write_text("image", encoding="utf-8")
            return SimpleNamespace(stdout="")
        raise subprocess.CalledProcessError(1, command, output="do-not-leak")

    with pytest.raises(ExtractionError, match="OCR failed") as caught:
        extract_pdf_pages(pdf, runner=runner)

    assert "do-not-leak" not in str(caught.value)
    assert "top-secret-page" not in str(caught.value)
    assert temporary_directory is not None and not temporary_directory.exists()


@pytest.mark.parametrize(("texts", "encrypted"), [([], False), (["text"], True)])
def test_zero_page_or_encrypted_pdf_is_rejected_with_safe_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    texts: list[str],
    encrypted: bool,
) -> None:
    pdf = tmp_path / "record.pdf"
    pdf.write_text("fake", encoding="utf-8")
    install_reader(monkeypatch, texts, encrypted=encrypted)

    with pytest.raises(ExtractionError, match="PDF extraction failed"):
        extract_pdf_pages(pdf)


class FakeMessages:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=outcome)])


class FakeClient:
    def __init__(self, outcomes: list[object]) -> None:
        self.messages = FakeMessages(outcomes)


def null_payload_text() -> str:
    return json.dumps({name: null_field() for name in FIELD_NAMES})


def test_extract_file_calls_model_once_and_writes_only_guarded_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "private" / "record.pdf"
    pdf.parent.mkdir()
    pdf.write_text("pdf bytes", encoding="utf-8")
    out = tmp_path / "out" / "record.json"
    page_sentinel = "FULL-PAGE-SENTINEL"
    key_sentinel = "SENTINEL-API-KEY"
    monkeypatch.setattr(
        extraction,
        "extract_pdf_pages",
        lambda _path, runner=subprocess.run: ["地址：北投路一段 " + page_sentinel],
    )
    response = payload_with("address", valid_field())
    client = FakeClient([json.dumps(response, ensure_ascii=False)])
    client.api_key = key_sentinel

    result = extract_file(pdf, out, client, MODEL)

    assert result.case.fields["address"].value == "北投路一段"
    assert result.failures == []
    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    assert call["temperature"] == 0
    assert 0 < int(call["max_tokens"]) <= 4096
    prompt = str(call["messages"])
    assert "one JSON object only" in prompt
    assert "1-based" in prompt
    written = out.read_text(encoding="utf-8")
    assert json.loads(written)["source_pdf"] == "record.pdf"
    assert key_sentinel not in written
    assert str(pdf.resolve()) not in written
    assert page_sentinel not in written


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (RuntimeError("raw-model-secret"), "model_error"),
        ('{"case_number": null} trailing raw-model-secret', "malformed_model_json"),
    ],
)
def test_model_failure_or_malformed_json_writes_all_null_pending_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: object,
    reason: str,
) -> None:
    pdf = tmp_path / "record.pdf"
    pdf.write_text("pdf bytes", encoding="utf-8")
    out = tmp_path / "record.json"
    monkeypatch.setattr(extraction, "extract_pdf_pages", lambda *_args, **_kwargs: ["page text"])

    result = extract_file(pdf, out, FakeClient([outcome]), MODEL)

    assert result.case.review_status == "pending"
    assert all(field == EvidenceField.null() for field in result.case.fields.values())
    assert [failure.reason for failure in result.failures] == [reason]
    assert "raw-model-secret" not in out.read_text(encoding="utf-8")


def test_process_directory_uses_stable_recursive_paths_continues_and_skip_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_dir = tmp_path / "raw"
    out_dir = tmp_path / "extracted"
    (in_dir / "nested").mkdir(parents=True)
    (in_dir / "z.pdf").write_text("z", encoding="utf-8")
    (in_dir / "nested" / "a.PDF").write_text("a", encoding="utf-8")
    seen: list[str] = []

    def fake_pages(path: Path, **_kwargs: object) -> list[str]:
        seen.append(path.name)
        return [path.stem]

    monkeypatch.setattr(extraction, "extract_pdf_pages", fake_pages)
    client = FakeClient([RuntimeError("first"), null_payload_text()])

    first = process_directory(in_dir, out_dir, client, MODEL)

    assert seen == ["a.PDF", "z.pdf"]
    assert first.extracted_files == 2
    assert (out_dir / "nested" / "a.json").exists()
    assert (out_dir / "z.json").exists()
    assert json.loads((out_dir / "nested" / "a.json").read_text(encoding="utf-8"))[
        "source_pdf"
    ] == "nested/a.PDF"
    assert first.failed_fields == 1

    seen.clear()
    skip_client = FakeClient([])
    skipped = process_directory(in_dir, out_dir, skip_client, MODEL)
    assert skipped.extracted_files == 0
    assert seen == ["a.PDF", "z.pdf"]
    assert skip_client.messages.calls == []

    seen.clear()
    forced = process_directory(
        in_dir,
        out_dir,
        FakeClient([null_payload_text()] * 2),
        MODEL,
        force=True,
    )
    assert forced.extracted_files == 2
    assert seen == ["a.PDF", "z.pdf"]


def test_semantically_invalid_existing_output_is_reprocessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    pdf = in_dir / "record.pdf"
    pdf.write_text("record", encoding="utf-8")
    monkeypatch.setattr(extraction, "extract_pdf_pages", lambda *_args, **_kwargs: ["page"])
    process_directory(in_dir, out_dir, FakeClient([null_payload_text()]), MODEL)
    out_path = out_dir / "record.json"
    existing = json.loads(out_path.read_text(encoding="utf-8"))
    existing["fields"]["tree_count"] = {
        "value": -1,
        "page": 1,
        "quote_snippet": "page",
        "confidence": "high",
    }
    out_path.write_text(json.dumps(existing), encoding="utf-8")

    result = process_directory(in_dir, out_dir, FakeClient([null_payload_text()]), MODEL)

    assert result.extracted_files == 1
    assert json.loads(out_path.read_text(encoding="utf-8"))["fields"]["tree_count"] == null_field()


@pytest.mark.parametrize(
    "invalid_evidence",
    [
        {
            "value": "北投路一段",
            "page": 2,
            "quote_snippet": "地址：北投路一段",
            "confidence": "high",
        },
        {
            "value": "北投路一段",
            "page": 1,
            "quote_snippet": "地址：虛構路段",
            "confidence": "high",
        },
    ],
)
def test_existing_output_with_unverified_page_or_quote_is_reprocessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_evidence: dict[str, object],
) -> None:
    in_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    (in_dir / "record.pdf").write_text("record", encoding="utf-8")
    page_text = "案件資料，地址：北投路一段，審議結果另載。"
    monkeypatch.setattr(extraction, "extract_pdf_pages", lambda *_args, **_kwargs: [page_text])
    process_directory(in_dir, out_dir, FakeClient([null_payload_text()]), MODEL)
    out_path = out_dir / "record.json"
    existing = json.loads(out_path.read_text(encoding="utf-8"))
    existing["fields"]["address"] = invalid_evidence
    out_path.write_text(json.dumps(existing), encoding="utf-8")
    client = FakeClient([null_payload_text()])

    result = process_directory(in_dir, out_dir, client, MODEL)

    assert result.extracted_files == 1
    assert len(client.messages.calls) == 1
    assert json.loads(out_path.read_text(encoding="utf-8"))["fields"]["address"] == null_field()


def test_existing_output_duplicate_keys_is_reprocessed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    (in_dir / "record.pdf").write_text("record", encoding="utf-8")
    monkeypatch.setattr(extraction, "extract_pdf_pages", lambda *_args, **_kwargs: ["page"])
    process_directory(in_dir, out_dir, FakeClient([null_payload_text()]), MODEL)
    out_path = out_dir / "record.json"
    existing = out_path.read_text(encoding="utf-8")
    duplicate = existing.replace(
        '"review_status": "pending",',
        '"review_status": "pending",\n  "review_status": "pending",',
        1,
    )
    assert duplicate != existing
    out_path.write_text(duplicate, encoding="utf-8")
    client = FakeClient([null_payload_text()])

    result = process_directory(in_dir, out_dir, client, MODEL)

    assert result.extracted_files == 1
    assert len(client.messages.calls) == 1


def test_ocr_failure_while_validating_existing_output_reenters_safe_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    (in_dir / "record.pdf").write_text("record", encoding="utf-8")
    monkeypatch.setattr(extraction, "extract_pdf_pages", lambda *_args, **_kwargs: ["page"])
    process_directory(in_dir, out_dir, FakeClient([null_payload_text()]), MODEL)
    calls = 0

    def failed_ocr(*_args: object, **_kwargs: object) -> list[str]:
        nonlocal calls
        calls += 1
        raise ExtractionError("OCR failed")

    monkeypatch.setattr(extraction, "extract_pdf_pages", failed_ocr)
    client = FakeClient([])

    result = process_directory(in_dir, out_dir, client, MODEL)

    assert result.extracted_files == 1
    assert result.failed_fields == 1
    assert calls == 2
    assert client.messages.calls == []
    output = json.loads((out_dir / "record.json").read_text(encoding="utf-8"))
    assert output["review_status"] == "pending"
    assert output["fields"] == {name: null_field() for name in FIELD_NAMES}


def test_failure_history_merges_sorts_deduplicates_and_malformed_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    in_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    out_dir.mkdir()
    (in_dir / "b.pdf").write_text("b", encoding="utf-8")
    history = {
        "schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "failures": [
            {"source_pdf": "z.pdf", "field": "address", "reason": "quote_not_exact"},
            {"source_pdf": "z.pdf", "field": "address", "reason": "quote_not_exact"},
        ],
    }
    failure_path = out_dir / "extraction_failures.json"
    failure_path.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(extraction, "extract_pdf_pages", lambda *_args, **_kwargs: ["page"])

    process_directory(in_dir, out_dir, FakeClient([RuntimeError("hidden")]), MODEL)

    merged = json.loads(failure_path.read_text(encoding="utf-8"))
    assert merged["failures"] == [
        {"field": "__root__", "reason": "model_error", "source_pdf": "b.pdf"},
        {"field": "address", "reason": "quote_not_exact", "source_pdf": "z.pdf"},
    ]

    failure_path.write_text('{"secret":"do-not-echo"}', encoding="utf-8")
    with pytest.raises(ExtractionError, match="failure history") as caught:
        process_directory(in_dir, out_dir, FakeClient([]), MODEL)
    assert "do-not-echo" not in str(caught.value)


def test_missing_api_key_exits_zero_without_ocr_or_model_and_writes_github_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    in_dir = tmp_path / "raw"
    out_dir = tmp_path / "out"
    in_dir.mkdir()
    (in_dir / "b.pdf").write_text("b", encoding="utf-8")
    (in_dir / "a.pdf").write_text("a", encoding="utf-8")
    github_output = tmp_path / "github-output"

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("missing API key must not initialize a client or extract a PDF")

    monkeypatch.setattr(extraction, "extract_pdf_pages", forbidden)
    status = main(
        ["--in", str(in_dir), "--out", str(out_dir)],
        environ={"GITHUB_OUTPUT": str(github_output)},
        client_factory=forbidden,
    )

    assert status == 0
    assert capsys.readouterr().out.count("待處理") == 1
    failure_document = json.loads(
        (out_dir / "extraction_failures.json").read_text(encoding="utf-8")
    )
    assert len(failure_document["failures"]) == 2
    assert {failure["reason"] for failure in failure_document["failures"]} == {
        "missing_api_key"
    }
    assert github_output.read_text(encoding="utf-8") == (
        "extracted_files=0\nfailed_fields=2\n"
    )
