"""Small S3-compatible client for object-storage health checks.

Uses AWS Signature Version 4 with stdlib only so the live runner does not need
an additional dependency just to PUT/HEAD/DELETE one probe object.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class S3Response:
    status: int
    body: bytes
    headers: Mapping[str, str]


class S3Error(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class S3Client:
    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        access_key: str,
        secret_key: str,
        timeout: int = 60,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout

    def head_bucket(self, bucket: str) -> S3Response:
        return self.request("HEAD", bucket, "")

    def put_object(self, bucket: str, key: str, body: bytes) -> S3Response:
        return self.request("PUT", bucket, key, body=body)

    def head_object(self, bucket: str, key: str) -> S3Response:
        return self.request("HEAD", bucket, key)

    def delete_object(self, bucket: str, key: str) -> S3Response:
        return self.request("DELETE", bucket, key)

    def request(self, method: str, bucket: str, key: str, *, body: bytes = b"") -> S3Response:
        url = self._url(bucket, key)
        parsed = urllib.parse.urlparse(url)
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_uri = parsed.path or "/"
        canonical_query = parsed.query
        host = parsed.netloc
        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        canonical_request = "\n".join(
            [
                method,
                canonical_uri,
                canonical_query,
                canonical_headers,
                signed_headers,
                payload_hash,
            ]
        )
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signing_key = self._signing_key(date_stamp)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        request_headers = {
            "Authorization": authorization,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        request = urllib.request.Request(url, data=body if method != "HEAD" else None, method=method)
        for name, value in request_headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return S3Response(
                    status=int(response.status),
                    body=response.read(),
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            body_bytes = exc.read()
            raise S3Error(
                f"S3 {method} {self._safe_path(bucket, key)} failed with HTTP {exc.code}",
                status=int(exc.code),
                body=body_bytes,
            ) from exc
        except urllib.error.URLError as exc:
            raise S3Error(
                f"S3 {method} {self._safe_path(bucket, key)} failed: {exc.reason}"
            ) from exc

    def _url(self, bucket: str, key: str) -> str:
        quoted_bucket = urllib.parse.quote(bucket.strip("/"), safe="")
        quoted_key = "/".join(urllib.parse.quote(part, safe="") for part in key.split("/") if part)
        if quoted_key:
            return f"{self.endpoint}/{quoted_bucket}/{quoted_key}"
        return f"{self.endpoint}/{quoted_bucket}"

    def _safe_path(self, bucket: str, key: str) -> str:
        return f"/{bucket}/{key}" if key else f"/{bucket}"

    def _signing_key(self, date_stamp: str) -> bytes:
        key = ("AWS4" + self.secret_key).encode("utf-8")
        date_key = hmac.new(key, date_stamp.encode("utf-8"), hashlib.sha256).digest()
        region_key = hmac.new(date_key, self.region.encode("utf-8"), hashlib.sha256).digest()
        service_key = hmac.new(region_key, b"s3", hashlib.sha256).digest()
        return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()
