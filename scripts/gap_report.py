"""Build a safe public report of source and local-artifact transparency gaps."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from scripts.crawl_review_records import _eligible_title
from scripts.extract_cases import (
    _FAILURE_REASONS as _EXTRACTION_FAILURE_REASONS,
    _validate_field,
)
from scripts.extraction_schema import FIELD_NAMES
from scripts.health_check import _validate_safe_url


SCHEMA_VERSION = "1.0"
STALE_AFTER_DAYS = 30
_HEALTH_STATUSES = frozenset({"available", "unavailable", "not_configured"})
_HEALTH_REASONS = frozenset(
    {
        "probe_failed",
        "source_not_configured",
        "redirect_rejected",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_TAIPEI = ZoneInfo("Asia/Taipei")
_HEALTH_KEYS = frozenset({"schema_version", "generated_at", "sources"})
_HEALTH_SOURCE_KEYS = frozenset(
    {
        "name",
        "kind",
        "required",
        "status",
        "checked_at",
        "reason",
        "unavailable_since",
    }
)


class GapInputError(ValueError):
    """Raised when local input cannot be safely trusted."""


@dataclass(frozen=True, slots=True)
class _Evidence:
    paths: tuple[str, ...]
    day: date


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise GapInputError("gap input is invalid")
        document[key] = value
    return document


def _loads_json(text: str) -> object:
    try:
        return json.loads(
            text,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                GapInputError("gap input is invalid")
            ),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise GapInputError("gap input is invalid") from error


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _validate_health(health: object, now: datetime) -> list[dict[str, object]]:
    if not isinstance(health, dict) or set(health) != _HEALTH_KEYS:
        raise GapInputError("gap input is invalid")
    if health["schema_version"] != SCHEMA_VERSION:
        raise GapInputError("gap input is invalid")
    generated_at = _aware_datetime(health["generated_at"])
    if generated_at is None or generated_at > now:
        raise GapInputError("gap input is invalid")
    sources = health["sources"]
    if not isinstance(sources, list):
        raise GapInputError("gap input is invalid")

    validated: list[dict[str, object]] = []
    names: list[str] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != _HEALTH_SOURCE_KEYS:
            raise GapInputError("gap input is invalid")
        name = source["name"]
        kind = source["kind"]
        required = source["required"]
        status = source["status"]
        checked_at = _aware_datetime(source["checked_at"])
        reason = source["reason"]
        unavailable = _aware_datetime(source["unavailable_since"])
        if (
            not isinstance(name, str)
            or not name
            or kind not in {"dataset", "url"}
            or not isinstance(required, bool)
            or status not in _HEALTH_STATUSES
            or checked_at is None
            or checked_at > generated_at
            or (reason is not None and reason not in _HEALTH_REASONS)
        ):
            raise GapInputError("gap input is invalid")
        if status == "available":
            valid_contract = reason is None and source["unavailable_since"] is None
        elif status == "not_configured":
            valid_contract = (
                reason == "source_not_configured" and source["unavailable_since"] is None
            )
        else:
            valid_contract = (
                reason in _HEALTH_REASONS - {"source_not_configured"}
                and unavailable is not None
                and unavailable <= checked_at
            )
        if not valid_contract:
            raise GapInputError("gap input is invalid")
        names.append(name)
        validated.append(source)
    if names != sorted(names) or len(names) != len(set(names)):
        raise GapInputError("gap input is invalid")
    return validated


def _relative_path(path: Path, base_dir: Path) -> str | None:
    try:
        path.resolve().relative_to(base_dir.resolve())
        relative = path.relative_to(base_dir).as_posix()
    except (OSError, ValueError):
        return None
    return relative if relative and ".." not in Path(relative).parts else None


def _read_local_json(path: Path) -> object | None:
    try:
        if not path.is_file():
            return None
    except OSError:
        raise GapInputError("gap input is invalid") from None
    try:
        return _loads_json(path.read_text(encoding="utf-8"))
    except OSError:
        raise GapInputError("gap input is invalid") from None
    except (UnicodeError, GapInputError):
        return None


def _safe_official_url(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        _validate_safe_url(value)
    except ValueError:
        return False
    return True


def _read_bounded_bytes(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > _MAX_ARTIFACT_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        raise GapInputError("gap input is invalid") from None


def _read_gzip_payload(path: Path) -> bytes | None:
    compressed = _read_bounded_bytes(path)
    if compressed is None:
        return None
    output = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as archive:
            while True:
                chunk = archive.read(min(1024 * 1024, _MAX_ARTIFACT_BYTES + 1 - len(output)))
                if not chunk:
                    return bytes(output)
                output.extend(chunk)
                if len(output) > _MAX_ARTIFACT_BYTES:
                    return None
    except (gzip.BadGzipFile, EOFError, zlib.error):
        return None
    except OSError:
        raise GapInputError("gap input is invalid") from None


def _date_partition_is_valid(
    partition: date,
    retrieved_at: datetime,
    now: datetime,
) -> bool:
    return (
        partition <= now.astimezone(_TAIPEI).date()
        and retrieved_at <= now
        and retrieved_at.astimezone(_TAIPEI).date() == partition
    )


def _glob_paths(root: Path, pattern: str, *, recursive: bool = False) -> list[Path]:
    try:
        return list(root.rglob(pattern) if recursive else root.glob(pattern))
    except OSError:
        raise GapInputError("gap input is invalid") from None


def _open_data_evidence(
    base_dir: Path,
    source_name: str,
    now: datetime,
) -> _Evidence | None:
    root = base_dir / "raw" / "open_data" / source_name
    candidates: list[tuple[date, tuple[str, ...]]] = []
    if not root.is_dir():
        return None
    for snapshot in _glob_paths(root, "*.csv.gz"):
        try:
            snapshot_day = date.fromisoformat(snapshot.name.removesuffix(".csv.gz"))
        except ValueError:
            continue
        manifest = snapshot.with_suffix("").with_suffix(".json")
        document = _read_local_json(manifest)
        if not isinstance(document, dict) or set(document) != {
            "source_name",
            "dataset_id",
            "resource_id",
            "original_url",
            "retrieved_at",
            "uncompressed_byte_length",
            "sha256",
        }:
            continue
        retrieved_at = _aware_datetime(document["retrieved_at"])
        payload = _read_gzip_payload(snapshot)
        if (
            document["source_name"] != source_name
            or not _safe_official_url(document["original_url"])
            or retrieved_at is None
            or not _date_partition_is_valid(snapshot_day, retrieved_at, now)
            or type(document["uncompressed_byte_length"]) is not int
            or document["uncompressed_byte_length"] < 0
            or not isinstance(document["sha256"], str)
            or _SHA256.fullmatch(document["sha256"]) is None
            or payload is None
            or len(payload) != document["uncompressed_byte_length"]
            or hashlib.sha256(payload).hexdigest() != document["sha256"]
        ):
            continue
        paths = tuple(
            sorted(
                path
                for path in (
                    _relative_path(snapshot, base_dir),
                    _relative_path(manifest, base_dir),
                )
                if path is not None
            )
        )
        if len(paths) == 2:
            candidates.append((snapshot_day, paths))
    if not candidates:
        return None
    snapshot_day, paths = max(candidates, key=lambda item: (item[0], item[1]))
    return _Evidence(paths, snapshot_day)


def _schedule_manifest_for(snapshot: Path) -> tuple[Path, object] | None:
    candidates = (
        snapshot.with_suffix(".manifest.json"),
        Path(f"{snapshot}.manifest.json"),
    )
    for manifest in candidates:
        if manifest.is_file():
            return manifest, _read_local_json(manifest)
    return None


def _schedule_evidence(base_dir: Path, now: datetime) -> _Evidence | None:
    root = base_dir / "raw" / "pruning_schedules"
    if not root.is_dir():
        return None
    candidates: list[tuple[datetime, tuple[str, ...]]] = []
    for snapshot in _glob_paths(root, "*", recursive=True):
        if not snapshot.is_file() or snapshot.name.endswith(".manifest.json"):
            continue
        manifest_result = _schedule_manifest_for(snapshot)
        if manifest_result is None:
            continue
        manifest, document = manifest_result
        if not isinstance(document, dict):
            continue
        try:
            partition = date.fromisoformat(snapshot.parent.name)
        except ValueError:
            continue
        retrieved_at = _aware_datetime(document.get("retrieved_at"))
        sha256 = document.get("sha256")
        byte_length = document.get("byte_length")
        content_type = document.get("content_type")
        payload = _read_bounded_bytes(snapshot)
        if (
            retrieved_at is None
            or not _date_partition_is_valid(partition, retrieved_at, now)
            or not _safe_official_url(document.get("source_url"))
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
            or type(byte_length) is not int
            or byte_length < 0
            or not isinstance(content_type, str)
            or not content_type
            or payload is None
            or len(payload) != byte_length
            or hashlib.sha256(payload).hexdigest() != sha256
        ):
            continue
        paths = tuple(
            sorted(
                path
                for path in (
                    _relative_path(snapshot, base_dir),
                    _relative_path(manifest, base_dir),
                )
                if path is not None
            )
        )
        if len(paths) == 2:
            candidates.append((retrieved_at, paths))
    if not candidates:
        return None
    retrieved_at, paths = max(candidates, key=lambda item: (item[0], item[1]))
    return _Evidence(paths, retrieved_at.astimezone(UTC).date())


def _review_evidence(base_dir: Path, now: datetime, kind: str) -> _Evidence | None:
    root = base_dir / "raw" / "review_meetings"
    if not root.is_dir():
        return None
    candidates: list[tuple[datetime, tuple[str, ...]]] = []
    manifest_keys = {
        "schema_version",
        "title",
        "published_date",
        "detail_url",
        "attachment_url",
        "sha256",
        "byte_length",
        "retrieved_at",
    }
    for pdf in _glob_paths(root, "*.pdf", recursive=True):
        manifest = pdf.with_suffix(".manifest.json")
        document = _read_local_json(manifest)
        if not isinstance(document, dict) or set(document) != manifest_keys:
            continue
        retrieved_at = _aware_datetime(document["retrieved_at"])
        payload = _read_bounded_bytes(pdf)
        try:
            published_date = date.fromisoformat(document["published_date"])
        except (TypeError, ValueError):
            continue
        if (
            document["schema_version"] != 1
            or not isinstance(document["title"], str)
            or not document["title"]
            or not _eligible_title(document["title"], kind)  # type: ignore[arg-type]
            or published_date.isoformat() != document["published_date"]
            or pdf.parent.name != published_date.strftime("%Y-%m")
            or retrieved_at is None
            or published_date > now.astimezone(_TAIPEI).date()
            or retrieved_at > now
            or retrieved_at.astimezone(_TAIPEI).date() < published_date
            or not _safe_official_url(document["detail_url"])
            or not _safe_official_url(document["attachment_url"])
            or not isinstance(document["sha256"], str)
            or _SHA256.fullmatch(document["sha256"]) is None
            or type(document["byte_length"]) is not int
            or document["byte_length"] < 0
            or payload is None
            or not payload.startswith(b"%PDF-")
            or len(payload) != document["byte_length"]
            or hashlib.sha256(payload).hexdigest() != document["sha256"]
        ):
            continue
        paths = tuple(
            sorted(
                path
                for path in (
                    _relative_path(pdf, base_dir),
                    _relative_path(manifest, base_dir),
                )
                if path is not None
            )
        )
        if len(paths) == 2:
            candidates.append((retrieved_at, paths))
    if not candidates:
        return None
    retrieved_at, paths = max(candidates, key=lambda item: (item[0], item[1]))
    return _Evidence(paths, retrieved_at.astimezone(UTC).date())


def _source_evidence(
    base_dir: Path,
    source_name: str,
    now: datetime,
) -> _Evidence | None:
    if source_name in {"street_trees", "protected_trees"}:
        return _open_data_evidence(base_dir, source_name, now)
    if source_name == "pruning_schedule":
        return _schedule_evidence(base_dir, now)
    if source_name in {"review_records", "committee_records"}:
        kind = "review" if source_name == "review_records" else "committee"
        return _review_evidence(base_dir, now, kind)
    return None


def _gap(
    code: str,
    source: str | None,
    message: str,
    *,
    count: int = 1,
    age_days: int | None = None,
    evidence_paths: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "code": code,
        "source": source,
        "count": count,
        "age_days": age_days,
        "evidence_paths": sorted(evidence_paths),
        "message": message,
    }


def _safe_source_pdf(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    path = Path(value)
    if (
        path.is_absolute()
        or re.match(r"^[A-Za-z]:[/\\]", value)
        or ".." in path.parts
        or path.suffix.casefold() != ".pdf"
    ):
        return None
    return path.as_posix()


def _read_text_pdf_pages(path: Path) -> list[str] | None:
    try:
        reader = PdfReader(path)
        if reader.is_encrypted or not reader.pages:
            return None
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        return None
    return pages


def _valid_pending_case(document: object, base_dir: Path) -> bool:
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "source_pdf",
        "source_sha256",
        "model",
        "review_status",
        "fields",
    }:
        return False
    source_pdf = _safe_source_pdf(document["source_pdf"])
    fields = document["fields"]
    if (
        document["schema_version"] != "1.0"
        or source_pdf is None
        or not isinstance(document["source_sha256"], str)
        or _SHA256.fullmatch(document["source_sha256"]) is None
        or not isinstance(document["model"], str)
        or not document["model"]
        or document["review_status"] != "pending"
        or not isinstance(fields, dict)
        or set(fields) != set(FIELD_NAMES)
    ):
        return False
    pdf = base_dir / "raw" / "review_meetings" / source_pdf
    if _relative_path(pdf, base_dir) is None:
        return False
    payload = _read_bounded_bytes(pdf) if pdf.is_file() else None
    if (
        payload is None
        or not payload.startswith(b"%PDF-")
        or hashlib.sha256(payload).hexdigest() != document["source_sha256"]
    ):
        return False
    pages = _read_text_pdf_pages(pdf)
    if pages is None:
        return False
    for name in FIELD_NAMES:
        validated, failure = _validate_field(name, fields[name], pages, source_pdf)
        if failure is not None or validated.to_dict() != fields[name]:
            return False
    return True


def _failure_count(path: Path, now: datetime) -> int | None:
    try:
        if not path.is_file():
            return None
        document = _loads_json(path.read_text(encoding="utf-8"))
    except OSError:
        raise GapInputError("gap input is invalid") from None
    except (UnicodeError, GapInputError):
        raise GapInputError("gap input is invalid") from None
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "generated_at",
        "failures",
    }:
        raise GapInputError("gap input is invalid")
    generated_at = _aware_datetime(document["generated_at"])
    failures = document["failures"]
    if (
        document["schema_version"] != "1.0"
        or generated_at is None
        or generated_at > now
        or not isinstance(failures, list)
    ):
        raise GapInputError("gap input is invalid")
    for failure in failures:
        if not isinstance(failure, dict) or set(failure) != {
            "source_pdf",
            "field",
            "reason",
        }:
            raise GapInputError("gap input is invalid")
        if (
            _safe_source_pdf(failure["source_pdf"]) is None
            or failure["field"] not in {*FIELD_NAMES, "__root__"}
            or failure["reason"] not in _EXTRACTION_FAILURE_REASONS
        ):
            raise GapInputError("gap input is invalid")
    return len(failures)


def _extraction_signals(
    base_dir: Path,
    now: datetime,
) -> list[dict[str, object]]:
    extracted = base_dir / "extracted"
    if not extracted.is_dir():
        return []
    pending: list[str] = []
    failure_path = extracted / "extraction_failures.json"
    for path in _glob_paths(extracted, "*.json", recursive=True):
        if path == failure_path:
            continue
        document = _read_local_json(path)
        relative = _relative_path(path, base_dir)
        if _valid_pending_case(document, base_dir) and relative is not None:
            pending.append(relative)
    gaps: list[dict[str, object]] = []
    if pending:
        gaps.append(
            _gap(
                "pending_extraction_review",
                "review_records",
                f"有 {len(pending)} 份會議資料仍待人工審核。",
                count=len(pending),
                evidence_paths=pending,
            )
        )
    failure_count = _failure_count(failure_path, now)
    if failure_count:
        relative = _relative_path(failure_path, base_dir)
        if relative is not None:
            gaps.append(
                _gap(
                    "extraction_failures",
                    "review_records",
                    f"有 {failure_count} 個擷取欄位需要人工確認。",
                    count=failure_count,
                    evidence_paths=[relative],
                )
            )
    return gaps


def build_gap_report(
    health: object,
    base_dir: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    stale_after_days: int = STALE_AFTER_DAYS,
) -> dict[str, object]:
    """Build the exact report without mutating any source artifact."""
    now = clock()
    if (
        now.tzinfo is None
        or now.utcoffset() is None
        or type(stale_after_days) is not int
        or stale_after_days < 0
    ):
        raise GapInputError("gap input is invalid")
    health_sources = _validate_health(health, now)
    today = now.astimezone(UTC).date()
    sources: list[dict[str, object]] = []
    gaps: list[dict[str, object]] = []
    statuses = {status: 0 for status in _HEALTH_STATUSES}

    for health_source in health_sources:
        name = str(health_source["name"])
        status = str(health_source["status"])
        statuses[status] += 1
        evidence = _source_evidence(base_dir, name, now)
        evidence_paths = list(evidence.paths) if evidence is not None else []
        age_days = max(0, (today - evidence.day).days) if evidence is not None else None
        if status == "unavailable":
            unavailable = _aware_datetime(health_source["unavailable_since"])
            assert unavailable is not None
            message = f"本資料自 {unavailable.date().isoformat()} 起未能更新"
            gaps.append(_gap("source_unavailable", name, message, evidence_paths=evidence_paths))
        elif status == "not_configured":
            message = "此資料來源尚未設定，不能視為目前已有涵蓋。"
            gaps.append(_gap("source_not_configured", name, message, evidence_paths=evidence_paths))
        elif evidence is None:
            message = "資料來源目前可用，但尚無本地快照證據。"
        else:
            message = f"資料來源目前可用，最新本地證據為 {age_days} 天前。"
        if age_days is not None and age_days > stale_after_days:
            gaps.append(
                _gap(
                    "stale_snapshot",
                    name,
                    f"最新本地證據已超過 {stale_after_days} 天。",
                    age_days=age_days,
                    evidence_paths=evidence_paths,
                )
            )
        sources.append(
            {
                "name": name,
                "status": status,
                "required": health_source["required"],
                "evidence_paths": evidence_paths,
                "snapshot_age_days": age_days,
                "message": message,
            }
        )

    if _open_data_evidence(base_dir, "protected_trees", now) is None:
        gaps.append(
            _gap(
                "missing_protected_trees",
                "protected_trees",
                "尚無可驗證的受保護樹木本地資料。",
            )
        )
    if _schedule_evidence(base_dir, now) is None:
        gaps.append(
            _gap(
                "missing_pruning_schedule",
                "pruning_schedule",
                "尚無可驗證的修剪期程本地資料。",
            )
        )
    gaps.extend(_extraction_signals(base_dir, now))
    gaps.sort(
        key=lambda item: (
            str(item["code"]),
            str(item["source"] or ""),
            tuple(item["evidence_paths"]),
        )
    )
    summary = {
        "source_count": len(sources),
        "available_sources": statuses["available"],
        "unavailable_sources": statuses["unavailable"],
        "not_configured_sources": statuses["not_configured"],
        "gap_count": len(gaps),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.astimezone(UTC).isoformat(),
        "stale_after_days": stale_after_days,
        "summary": summary,
        "sources": sources,
        "gaps": gaps,
    }


def _read_health(path: Path) -> object:
    try:
        return _loads_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, GapInputError):
        raise GapInputError("gap input is invalid") from None


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    content = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        raise GapInputError("gap input is invalid") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise GapInputError("gap input is invalid") from None


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--health", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    try:
        health = _read_health(arguments.health)
        report = build_gap_report(health, arguments.base_dir, clock=clock)
        _write_report(arguments.out, report)
    except GapInputError:
        print("無法產生資料缺口報告。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
