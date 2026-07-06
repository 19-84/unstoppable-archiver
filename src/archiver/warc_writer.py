# ABOUTME: WARC file writer that collects HTTP exchanges from Playwright
# ABOUTME: Intercepts requests/responses during capture and writes WARC records via warcio
# pyright: reportUnknownMemberType=false, reportMissingTypeStubs=false
"""WARC writing via Playwright request/response collection."""

from __future__ import annotations

import gzip
import hashlib
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import structlog
from beartype import beartype
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

log = structlog.get_logger()


@dataclass
class CapturedExchange:
    """A single HTTP request/response pair."""

    url: str
    method: str
    request_headers: dict[str, str]
    status: int
    response_headers: dict[str, str]
    body: bytes
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


class PlaywrightWARCWriter:
    """Collects HTTP exchanges and writes them as a WARC file.

    Dedup level 4: SHA-256 each response body. If a digest has been
    seen before in this capture, a revisit record is written instead.

    Bodies are buffered in RAM until finalize(), so two caps bound the
    exposure: `max_body_bytes` per exchange and `max_total_bytes` per
    capture. Exchanges over either cap are dropped from the WARC
    (logged + counted in archiver_warc_bodies_dropped_total) rather
    than truncated — a silently truncated record is worse for archive
    consumers than an absent one. 0 disables a cap.
    """

    def __init__(
        self,
        max_body_bytes: int = 0,
        max_total_bytes: int = 0,
    ) -> None:
        self._exchanges: list[CapturedExchange] = []
        self._seen_digests: set[str] = set()
        self._max_body_bytes = max_body_bytes
        self._max_total_bytes = max_total_bytes
        self._total_body_bytes = 0

    def accepts_body(self, size: int) -> bool:
        """True if a body of `size` bytes fits within both caps.

        Callers can pre-check a declared Content-Length here to avoid
        pulling an obviously oversized body into memory at all.
        """
        if 0 < self._max_body_bytes < size:
            return False
        return not (
            self._max_total_bytes > 0
            and self._total_body_bytes + size > self._max_total_bytes
        )

    def record_drop(self, url: str, size: int) -> None:
        """Log + count an exchange excluded by the size caps.

        Also used by the capture hook for Content-Length prechecks that
        skip reading the body at all.
        """
        from archiver.metrics import warc_bodies_dropped_total

        reason = (
            "body_too_large"
            if 0 < self._max_body_bytes < size
            else "capture_budget_exhausted"
        )
        warc_bodies_dropped_total.labels(reason=reason).inc()
        log.warning(
            "warc.body_dropped", url=url, size=size, reason=reason,
        )

    def add_exchange(self, exchange: CapturedExchange) -> bool:
        """Record an HTTP exchange for later WARC writing.

        Returns False (and drops the exchange) when the body busts a cap.
        """
        if not self.accepts_body(len(exchange.body)):
            self.record_drop(exchange.url, len(exchange.body))
            return False
        self._total_body_bytes += len(exchange.body)
        self._exchanges.append(exchange)
        return True

    @beartype
    def finalize(
        self, output_path: Path, original_url: str | None = None,
    ) -> int:
        """Write all collected exchanges to a .warc.gz file.

        Returns the file size in bytes.

        `original_url` (optional) records the user's ORIGINAL submission
        URL in the warcinfo header when it differs from what we actually
        fetched. Privacy-frontend captures fetch from
        nitter.tiekoetter.com/... but represent an archive of
        twitter.com/...; without this header a downstream consumer
        holding only the WARC can't recover that mapping.

        Field name `X-Archiver-Original-URI` (X- prefix marks it as
        non-WARC-spec; consumers expecting strict WARC 1.1 ignore it).
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        buf = io.BytesIO()
        writer = WARCWriter(buf, gzip=True)

        # Write warcinfo record
        info_lines = [
            b"software: unstoppable-archive",
            b"format: WARC File Format 1.1",
        ]
        if original_url:
            info_lines.append(
                f"X-Archiver-Original-URI: {original_url}".encode(),
            )
        info_payload = b"\r\n".join(info_lines) + b"\r\n"
        info_record = writer.create_warc_record(
            uri="urn:unstoppable-archive",
            record_type="warcinfo",
            warc_content_type="application/warc-fields",
            payload=io.BytesIO(info_payload),
            length=len(info_payload),
        )
        writer.write_record(info_record)

        for exchange in self._exchanges:
            self._write_exchange(writer, exchange)

        warc_bytes = buf.getvalue()
        output_path.write_bytes(warc_bytes)

        log.info(
            "warc.written",
            path=str(output_path),
            exchanges=len(self._exchanges),
            size=len(warc_bytes),
        )
        return len(warc_bytes)

    def _write_exchange(
        self,
        writer: WARCWriter,
        exchange: CapturedExchange,
    ) -> None:
        """Write a single request/response pair as WARC records."""
        warc_date = exchange.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Request record
        req_headers = StatusAndHeaders(
            f"{exchange.method} {exchange.url} HTTP/1.1",
            list(exchange.request_headers.items()),
            protocol="HTTP/1.1",
            is_http_request=True,
        )
        request_record = writer.create_warc_record(
            uri=exchange.url,
            record_type="request",
            http_headers=req_headers,
            warc_headers_dict={"WARC-Date": warc_date},
        )
        writer.write_record(request_record)

        # Check for dedup
        digest = "sha256:" + hashlib.sha256(exchange.body).hexdigest()

        if digest in self._seen_digests and len(exchange.body) > 0:
            # Write revisit record instead of full response
            revisit_record = writer.create_warc_record(
                uri=exchange.url,
                record_type="revisit",
                warc_headers_dict={
                    "WARC-Date": warc_date,
                    "WARC-Payload-Digest": digest,
                    "WARC-Profile": (
                        "http://netpreserve.org/warc/1.1/"
                        "revisit/identical-payload-digest"
                    ),
                },
            )
            writer.write_record(revisit_record)
        else:
            # Full response record
            try:
                reason = HTTPStatus(exchange.status).phrase
            except ValueError:
                reason = "Unknown"
            resp_headers = StatusAndHeaders(
                f"{exchange.status} {reason}",
                list(exchange.response_headers.items()),
                protocol="HTTP/1.1",
            )
            payload = io.BytesIO(exchange.body)
            response_record = writer.create_warc_record(
                uri=exchange.url,
                record_type="response",
                http_headers=resp_headers,
                payload=payload,
                length=len(exchange.body),
                warc_headers_dict={
                    "WARC-Date": warc_date,
                    "WARC-Payload-Digest": digest,
                },
            )
            writer.write_record(response_record)
            self._seen_digests.add(digest)

    @property
    def exchange_count(self) -> int:
        """Number of collected exchanges."""
        return len(self._exchanges)


@beartype
def is_valid_warc(path: Path) -> bool:
    """Quick validation: check file exists and starts with WARC magic."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    with gzip.open(path, "rb") as f:
        header = f.read(len(b"WARC/"))
    return header == b"WARC/"
