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
    """

    def __init__(self) -> None:
        self._exchanges: list[CapturedExchange] = []
        self._seen_digests: set[str] = set()

    def add_exchange(self, exchange: CapturedExchange) -> None:
        """Record an HTTP exchange for later WARC writing."""
        self._exchanges.append(exchange)

    @beartype
    def finalize(self, output_path: Path) -> int:
        """Write all collected exchanges to a .warc.gz file.

        Returns the file size in bytes.
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        buf = io.BytesIO()
        writer = WARCWriter(buf, gzip=True)

        # Write warcinfo record
        info_payload = (
            b"software: unstoppable-archive\r\n"
            b"format: WARC File Format 1.1\r\n"
        )
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
