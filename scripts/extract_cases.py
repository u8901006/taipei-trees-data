"""Extract evidence-backed tree cases from archived meeting-record PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

from pypdf import PdfReader

if __package__ in {None, ""}:  # Support ``python scripts/extract_cases.py``.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.extraction_schema import (
    FIELD_NAMES,
    SCHEMA_VERSION,
    EvidenceField,
    ExtractedCase,
    Failure,
)


_JSON_FENCE = re.compile(r"\s*```json\s*\n?(.*?)\n?```\s*", re.DOTALL)
_EVIDENCE_KEYS = frozenset({"value", "page", "quote_snippet", "confidence"})
_CONFIDENCE = frozenset({"high", "medium", "low"})
_STRING_FIELDS = frozenset({"case_number", "address", "decision", "meeting_date"})
_DEFAULT_MODEL = "claude-sonnet-4-20250514"
_PDF_ERROR = "PDF extraction failed"
_OCR_ERROR = "OCR failed"
_FAILURE_HISTORY_ERROR = "invalid failure history"
_MODEL_MAX_TOKENS = 2048
_PDF_TIMEOUT_SECONDS = 120
_OCR_TIMEOUT_SECONDS = 60
_QUOTE_MAX_CHARS = 500
_SHA256 = re.compile(r"[0-9a-f]{64}")
_OCR_IMAGE_NAME = re.compile(r"^page-(\d+)\.png$")
_FAILURE_REASONS = frozenset(
    {
        "empty_value",
        "invalid_confidence",
        "invalid_field_set",
        "invalid_field_shape",
        "invalid_meeting_date",
        "invalid_null_contract",
        "invalid_tree_count",
        "invalid_value_type",
        "malformed_model_json",
        "missing_api_key",
        "model_error",
        "page_out_of_range",
        "page_required",
        "pdf_extraction_error",
        "quote_not_exact",
        "quote_required",
        "quote_too_broad",
        "quote_too_long",
    }
)


class ExtractionError(RuntimeError):
    """A fixed, safe extraction error that never embeds source or tool output."""


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    case: ExtractedCase
    failures: list[Failure]


@dataclass(frozen=True, slots=True)
class BatchResult:
    extracted_files: int
    failed_fields: int


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid model JSON")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid model JSON")
        result[key] = value
    return result


def _loads_strict_json(text: str) -> object:
    return json.loads(
        text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )


def parse_model_json(text: str) -> object:
    """Parse exactly one plain JSON value or one outer ``json`` fence."""
    fenced = _JSON_FENCE.fullmatch(text)
    candidate = fenced.group(1) if fenced is not None else text
    try:
        return _loads_strict_json(candidate)
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ValueError("invalid model JSON") from error


def _normalize_evidence(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def _invalid_case(
    source_pdf: str,
    source_sha256: str,
    model: str,
) -> ExtractedCase:
    return ExtractedCase(
        schema_version=SCHEMA_VERSION,
        source_pdf=source_pdf,
        source_sha256=source_sha256,
        model=model,
        review_status="pending",
        fields={name: EvidenceField.null() for name in FIELD_NAMES},
    )


def _valid_date(value: str) -> bool:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _field_failure(
    source_pdf: str,
    field_name: str,
    reason: str,
) -> tuple[EvidenceField, Failure]:
    return EvidenceField.null(), Failure(source_pdf, field_name, reason)


def _validate_field(
    field_name: str,
    raw_field: object,
    pages: Sequence[str],
    source_pdf: str,
) -> tuple[EvidenceField, Failure | None]:
    if not isinstance(raw_field, dict) or set(raw_field) != _EVIDENCE_KEYS:
        return _field_failure(source_pdf, field_name, "invalid_field_shape")

    value = raw_field["value"]
    page = raw_field["page"]
    quote = raw_field["quote_snippet"]
    confidence = raw_field["confidence"]

    if value is None:
        if page is None and quote is None and confidence is None:
            return EvidenceField.null(), None
        return _field_failure(source_pdf, field_name, "invalid_null_contract")

    normalized_value: str | int
    if field_name == "tree_count":
        if type(value) is not int or value < 0:
            return _field_failure(source_pdf, field_name, "invalid_tree_count")
        normalized_value = value
    elif field_name in _STRING_FIELDS:
        if not isinstance(value, str):
            reason = (
                "invalid_meeting_date"
                if field_name == "meeting_date"
                else "invalid_value_type"
            )
            return _field_failure(source_pdf, field_name, reason)
        normalized_value = unicodedata.normalize("NFKC", value).strip()
        if not normalized_value:
            return _field_failure(source_pdf, field_name, "empty_value")
        if field_name == "meeting_date" and not _valid_date(normalized_value):
            return _field_failure(source_pdf, field_name, "invalid_meeting_date")
    else:  # FIELD_NAMES is closed, but fail safely if the schema changes.
        return _field_failure(source_pdf, field_name, "invalid_value_type")

    if type(page) is not int:
        return _field_failure(source_pdf, field_name, "page_required")
    if page < 1 or page > len(pages):
        return _field_failure(source_pdf, field_name, "page_out_of_range")
    if not isinstance(quote, str) or not quote.strip():
        return _field_failure(source_pdf, field_name, "quote_required")
    if not isinstance(confidence, str) or confidence not in _CONFIDENCE:
        return _field_failure(source_pdf, field_name, "invalid_confidence")
    normalized_quote = _normalize_evidence(quote)
    normalized_page = _normalize_evidence(pages[page - 1])
    if max(len(quote), len(normalized_quote)) > _QUOTE_MAX_CHARS:
        return _field_failure(source_pdf, field_name, "quote_too_long")
    if normalized_quote not in normalized_page:
        return _field_failure(source_pdf, field_name, "quote_not_exact")
    if len(normalized_quote) * 10 >= len(normalized_page) * 9:
        return _field_failure(source_pdf, field_name, "quote_too_broad")

    return EvidenceField(normalized_value, page, quote, confidence), None


def validate_case(
    payload: object,
    pages: Sequence[str],
    source_pdf: str,
    source_sha256: str,
    model: str,
) -> tuple[ExtractedCase, list[Failure]]:
    """Validate model output, nulling every field that lacks exact page evidence."""
    case = _invalid_case(source_pdf, source_sha256, model)
    if not isinstance(payload, dict) or set(payload) != set(FIELD_NAMES):
        return case, [Failure(source_pdf, "__root__", "invalid_field_set")]

    fields: dict[str, EvidenceField] = {}
    failures: list[Failure] = []
    for field_name in FIELD_NAMES:
        field, failure = _validate_field(field_name, payload[field_name], pages, source_pdf)
        fields[field_name] = field
        if failure is not None:
            failures.append(failure)
    return (
        ExtractedCase(
            schema_version=SCHEMA_VERSION,
            source_pdf=source_pdf,
            source_sha256=source_sha256,
            model=model,
            review_status="pending",
            fields=fields,
        ),
        failures,
    )


def _extract_pdf_text_pages(pdf_path: Path) -> list[str]:
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted or not reader.pages:
            raise ExtractionError(_PDF_ERROR)
        return [page.extract_text() or "" for page in reader.pages]
    except ExtractionError:
        raise
    except Exception:
        raise ExtractionError(_PDF_ERROR) from None


def extract_pdf_pages(
    pdf_path: Path,
    runner: Callable[..., object] = subprocess.run,
) -> list[str]:
    """Extract ordered PDF pages, applying OCR only where local text is blank."""
    pages = _extract_pdf_text_pages(pdf_path)
    blank_pages = [index for index, text in enumerate(pages) if not text.strip()]
    if not blank_pages:
        return pages

    try:
        with tempfile.TemporaryDirectory(prefix="tree-extraction-ocr-") as directory:
            image_prefix = Path(directory) / "page"
            runner(
                [
                    "pdftoppm",
                    "-png",
                    "-r",
                    "200",
                    str(pdf_path),
                    str(image_prefix),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_PDF_TIMEOUT_SECONDS,
            )
            image_paths: dict[int, Path] = {}
            candidates = list(Path(directory).glob("page-*.png"))
            for candidate in candidates:
                matched = _OCR_IMAGE_NAME.fullmatch(candidate.name)
                if matched is None or not candidate.is_file():
                    raise ExtractionError(_OCR_ERROR)
                page_number = int(matched.group(1))
                if (
                    page_number < 1
                    or page_number > len(pages)
                    or page_number in image_paths
                ):
                    raise ExtractionError(_OCR_ERROR)
                image_paths[page_number] = candidate
            if set(image_paths) != set(range(1, len(pages) + 1)):
                raise ExtractionError(_OCR_ERROR)
            for index in blank_pages:
                image_path = image_paths[index + 1]
                completed = runner(
                    [
                        "tesseract",
                        str(image_path),
                        "stdout",
                        "-l",
                        "chi_tra+eng",
                        "--psm",
                        "6",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_OCR_TIMEOUT_SECONDS,
                )
                output = getattr(completed, "stdout", None)
                if not isinstance(output, str):
                    raise ExtractionError(_OCR_ERROR)
                pages[index] = output
    except ExtractionError:
        raise
    except (
        FileNotFoundError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ):
        raise ExtractionError(_OCR_ERROR) from None
    return pages


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ExtractionError(_PDF_ERROR) from None
    return digest.hexdigest()


def _prompt_for(pages: Sequence[str]) -> str:
    page_text = "\n\n".join(
        f"--- PAGE {index} ---\n{text}" for index, text in enumerate(pages, start=1)
    )
    fields = ", ".join(FIELD_NAMES)
    return (
        "Extract one tree-review case from the supplied PDF pages. "
        "Return one JSON object only, with no Markdown or prose. "
        f"The root must contain exactly these fields: {fields}. "
        "Every field must be an object with exactly value, page, quote_snippet, "
        "and confidence. Never guess. Use all nulls when a value is not directly "
        "supported. Pages are 1-based. Every non-null value requires an exact source "
        "quote of at most 500 Unicode characters from the indicated page. Use the "
        "shortest sufficient quote, never the full or near-full page. confidence must "
        "be high, medium, or low. "
        "tree_count must be a non-negative integer and meeting_date must be YYYY-MM-DD.\n\n"
        f"{page_text}"
    )


def _response_text(response: object) -> str:
    content = getattr(response, "content", None)
    if not isinstance(content, list) or len(content) != 1:
        raise ValueError("invalid model response")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        raise ValueError("invalid model response")
    return text


def _write_json(path: Path, payload: object) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError:
        raise ExtractionError("output write failed") from None


def _extract_one(
    pdf_path: Path,
    out_path: Path,
    client: object,
    model: str,
    source_pdf: str,
    runner: Callable[..., object],
) -> ExtractionResult:
    source_sha256 = _sha256(pdf_path)
    try:
        pages = extract_pdf_pages(pdf_path, runner=runner)
    except ExtractionError:
        case = _invalid_case(source_pdf, source_sha256, model)
        failures = [Failure(source_pdf, "__root__", "pdf_extraction_error")]
        _write_json(out_path, case.to_dict())
        return ExtractionResult(case, failures)

    try:
        messages = getattr(client, "messages")
        response = messages.create(
            model=model,
            max_tokens=_MODEL_MAX_TOKENS,
            temperature=0,
            messages=[{"role": "user", "content": _prompt_for(pages)}],
        )
        model_text = _response_text(response)
    except Exception:
        case = _invalid_case(source_pdf, source_sha256, model)
        failures = [Failure(source_pdf, "__root__", "model_error")]
        _write_json(out_path, case.to_dict())
        return ExtractionResult(case, failures)

    try:
        payload = parse_model_json(model_text)
    except ValueError:
        case = _invalid_case(source_pdf, source_sha256, model)
        failures = [Failure(source_pdf, "__root__", "malformed_model_json")]
        _write_json(out_path, case.to_dict())
        return ExtractionResult(case, failures)

    case, failures = validate_case(
        payload,
        pages,
        source_pdf,
        source_sha256,
        model,
    )
    _write_json(out_path, case.to_dict())
    return ExtractionResult(case, failures)


def extract_file(
    pdf_path: Path,
    out_path: Path,
    client: object,
    model: str,
    runner: Callable[..., object] = subprocess.run,
) -> ExtractionResult:
    """Extract one PDF using a safe basename when no batch-relative path exists."""
    return _extract_one(
        pdf_path,
        out_path,
        client,
        model,
        pdf_path.name,
        runner,
    )


def _relative_pdf_paths(in_dir: Path) -> list[Path]:
    try:
        paths = [
            path
            for path in in_dir.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".pdf"
        ]
    except OSError:
        raise ExtractionError(_PDF_ERROR) from None
    return sorted(paths, key=lambda path: path.relative_to(in_dir).as_posix())


def _safe_relative_path(value: str) -> bool:
    path = Path(value)
    return (
        bool(value)
        and not path.is_absolute()
        and not re.match(r"^[A-Za-z]:[/\\]", value)
        and ".." not in path.parts
    )


def _valid_existing_output(
    path: Path,
    source_pdf: str,
    source_sha256: str,
    pages: Sequence[str],
) -> bool:
    try:
        payload = _loads_strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return False
    expected = {
        "schema_version",
        "source_pdf",
        "source_sha256",
        "model",
        "review_status",
        "fields",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        return False
    if (
        payload["schema_version"] != SCHEMA_VERSION
        or payload["source_pdf"] != source_pdf
        or payload["source_sha256"] != source_sha256
        or payload["review_status"] != "pending"
        or not isinstance(payload["model"], str)
        or not payload["model"]
    ):
        return False
    fields = payload["fields"]
    validated, failures = validate_case(
        fields,
        pages,
        source_pdf,
        source_sha256,
        payload["model"],
    )
    return not failures and validated.to_dict()["fields"] == fields


def _validate_failure(value: object) -> Failure:
    if not isinstance(value, dict) or set(value) != {"source_pdf", "field", "reason"}:
        raise ExtractionError(_FAILURE_HISTORY_ERROR)
    source_pdf = value["source_pdf"]
    field = value["field"]
    reason = value["reason"]
    if (
        not isinstance(source_pdf, str)
        or not _safe_relative_path(source_pdf)
        or field not in {*FIELD_NAMES, "__root__"}
        or reason not in _FAILURE_REASONS
    ):
        raise ExtractionError(_FAILURE_HISTORY_ERROR)
    return Failure(source_pdf, field, reason)


def _read_failure_history(path: Path) -> list[Failure]:
    if not path.exists():
        return []
    try:
        payload = _loads_strict_json(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "generated_at",
            "failures",
        }:
            raise ExtractionError(_FAILURE_HISTORY_ERROR)
        if payload["schema_version"] != SCHEMA_VERSION:
            raise ExtractionError(_FAILURE_HISTORY_ERROR)
        generated_at = datetime.fromisoformat(payload["generated_at"])
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ExtractionError(_FAILURE_HISTORY_ERROR)
        if not isinstance(payload["failures"], list):
            raise ExtractionError(_FAILURE_HISTORY_ERROR)
        return [_validate_failure(value) for value in payload["failures"]]
    except ExtractionError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        raise ExtractionError(_FAILURE_HISTORY_ERROR) from None


def _deduplicate_failures(failures: Sequence[Failure]) -> list[Failure]:
    unique = {
        (failure.source_pdf, failure.field, failure.reason): failure for failure in failures
    }
    return [unique[key] for key in sorted(unique)]


def _write_failure_history(
    path: Path,
    failures: Sequence[Failure],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    generated_at = clock()
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ExtractionError("failure history clock must be timezone-aware")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.astimezone(UTC).isoformat(),
        "failures": [failure.to_dict() for failure in _deduplicate_failures(failures)],
    }
    _write_json(path, payload)


def process_directory(
    in_dir: Path,
    out_dir: Path,
    client: object,
    model: str,
    force: bool = False,
    runner: Callable[..., object] = subprocess.run,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BatchResult:
    """Process PDFs in stable relative-path order and merge safe failure history."""
    failure_path = out_dir / "extraction_failures.json"
    history = _read_failure_history(failure_path)
    current_failures: list[Failure] = []
    extracted_files = 0
    for pdf_path in _relative_pdf_paths(in_dir):
        source_pdf = pdf_path.relative_to(in_dir).as_posix()
        out_path = out_dir / Path(source_pdf).with_suffix(".json")
        source_sha256 = _sha256(pdf_path)
        if not force and out_path.is_file():
            try:
                existing_pages = extract_pdf_pages(pdf_path, runner=runner)
            except ExtractionError:
                existing_pages = None
            if existing_pages is not None and _valid_existing_output(
                out_path,
                source_pdf,
                source_sha256,
                existing_pages,
            ):
                continue
        result = _extract_one(
            pdf_path,
            out_path,
            client,
            model,
            source_pdf,
            runner,
        )
        extracted_files += 1
        current_failures.extend(result.failures)
    _write_failure_history(failure_path, [*history, *current_failures], clock)
    return BatchResult(extracted_files, len(current_failures))


def _missing_key_result(
    in_dir: Path,
    out_dir: Path,
    model: str,
    force: bool,
) -> BatchResult:
    failure_path = out_dir / "extraction_failures.json"
    history = _read_failure_history(failure_path)
    current: list[Failure] = []
    for pdf_path in _relative_pdf_paths(in_dir):
        source_pdf = pdf_path.relative_to(in_dir).as_posix()
        out_path = out_dir / Path(source_pdf).with_suffix(".json")
        source_sha256 = _sha256(pdf_path)
        existing_pages: list[str] | None = None
        if not force and out_path.is_file():
            try:
                text_pages = _extract_pdf_text_pages(pdf_path)
                if all(page.strip() for page in text_pages):
                    existing_pages = text_pages
            except ExtractionError:
                pass
        if existing_pages is not None and _valid_existing_output(
            out_path,
            source_pdf,
            source_sha256,
            existing_pages,
        ):
            continue
        current.append(Failure(source_pdf, "__root__", "missing_api_key"))
    _write_failure_history(failure_path, [*history, *current])
    return BatchResult(0, len(current))


def _default_client_factory(*, api_key: str) -> object:
    from anthropic import Anthropic

    return Anthropic(api_key=api_key)


def _write_github_output(result: BatchResult, environ: Mapping[str, str]) -> None:
    output_path = environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    try:
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"extracted_files={result.extracted_files}\n")
            output.write(f"failed_fields={result.failed_fields}\n")
    except OSError:
        raise ExtractionError("GitHub output write failed") from None


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., object] = _default_client_factory,
    runner: Callable[..., object] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_dir", required=True, type=Path)
    parser.add_argument("--out", dest="out_dir", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args(argv)
    environment = os.environ if environ is None else environ
    model = environment.get("ANTHROPIC_MODEL", _DEFAULT_MODEL)
    api_key = environment.get("ANTHROPIC_API_KEY")
    try:
        if not api_key:
            result = _missing_key_result(
                arguments.in_dir,
                arguments.out_dir,
                model,
                arguments.force,
            )
            _write_github_output(result, environment)
            print("缺少 ANTHROPIC_API_KEY，PDF 擷取工作保留為待處理。")
            return 0
        client = client_factory(api_key=api_key)
        result = process_directory(
            arguments.in_dir,
            arguments.out_dir,
            client,
            model,
            force=arguments.force,
            runner=runner,
        )
        _write_github_output(result, environment)
    except ExtractionError:
        print("擷取作業安全中止，請檢查本機輸入與工具設定。")
        return 1
    print(
        f"完成 {result.extracted_files} 份待審核擷取，"
        f"{result.failed_fields} 個欄位保留待處理。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
