#!/usr/bin/env python3
"""
S3 / Salesforce Zendesk file migration utility.

Architecture:
    Local file
       |
       | POST /files/upload
       v
    API Gateway -> uploadFile Lambda
       |
       | returns a presigned S3 PUT URL
       v
    PUT file bytes to S3
       |
       v
    S3 object key: <zendesk_id>/<file_name>

This script is designed to work with the API Gateway + Lambda setup
described in the supplied Salesforce/S3 documentation.

Required environment variables:
    S3_API_BASE_URL
    S3_API_KEY

Optional:
    S3_API_KEY_HEADER   (default: x-api-key)

Two supported input modes:

1) Folder mode
   Arrange files like:
       migration_data/
           12345/
               invoice.pdf
               screenshot.png
           12346/
               recording.mp4

   The immediate folder name is treated as Zendesk_Id__c.

   Example:
       python s3_zendesk_migration.py folder ./migration_data

2) Manifest mode
   CSV columns:
       zendesk_id,file_path,zendesk_ticket_create_date

   Example:
       12345,C:\\data\\invoice.pdf,2024-02-15
       12345,C:\\data\\image.png,2024-02-15
       12346,C:\\data\\recording.mp4,2024-03-01

   Example:
       python s3_zendesk_migration.py manifest migration_manifest.csv

The script requests a presigned URL from:
    POST <S3_API_BASE_URL>/files/upload

Expected response:
    {
        "uploadUrl": "...",
        "s3Key": "12345/invoice.pdf",
        "fileName": "invoice.pdf"
    }

Then it uploads the actual file with HTTP PUT to the presigned URL and
sends S3 metadata compatible with the existing LWC:
    x-amz-meta-zendesk-id
    x-amz-meta-zendesk-ticket-create-date
"""

from __future__ import annotations

import argparse
import csv
import mimetypes
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests


DEFAULT_TIMEOUT = (15, 120)  # connect timeout, read timeout
DEFAULT_RETRIES = 3


@dataclass
class MigrationItem:
    zendesk_id: str
    file_path: Path
    zendesk_ticket_create_date: str = ""


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing environment variable: {name}\n"
            f"Set it before running the migration."
        )
    return value


def get_config() -> tuple[str, str, str]:
    base_url = required_env("S3_API_BASE_URL").rstrip("/")
    api_key = required_env("S3_API_KEY")
    api_key_header = os.getenv("S3_API_KEY_HEADER", "x-api-key").strip()

    return base_url, api_key, api_key_header


def content_type_for(path: Path) -> str:
    content_type, _ = mimetypes.guess_type(path.name)
    return content_type or "application/octet-stream"


def safe_file_name(name: str) -> str:
    # Mirrors the existing Apex/Lambda behavior conceptually:
    # path separators must never become additional S3 path components.
    return name.replace("/", "_").replace("\\", "_").strip()


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    *,
    retries: int = DEFAULT_RETRIES,
    **kwargs,
) -> requests.Response:
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            response = session.request(
                method,
                url,
                timeout=DEFAULT_TIMEOUT,
                **kwargs,
            )

            # Retry common transient server/gateway errors.
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt < retries:
                    delay = 2 ** (attempt - 1)
                    print(
                        f"  Transient HTTP {response.status_code}; "
                        f"retrying in {delay}s..."
                    )
                    time.sleep(delay)
                    continue

            return response

        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                delay = 2 ** (attempt - 1)
                print(f"  Network error; retrying in {delay}s...")
                time.sleep(delay)
            else:
                raise

    raise RuntimeError(f"Request failed: {last_error}")


def request_presigned_upload_url(
    session: requests.Session,
    base_url: str,
    api_key: str,
    api_key_header: str,
    item: MigrationItem,
) -> dict:
    endpoint = f"{base_url}/files/upload"

    payload = {
        "zendeskId": item.zendesk_id.strip(),
        "fileName": safe_file_name(item.file_path.name),
        "contentType": content_type_for(item.file_path),
        "zendeskTicketCreateDate": item.zendesk_ticket_create_date.strip(),
    }

    headers = {
        api_key_header: api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    response = request_with_retry(
        session,
        "POST",
        endpoint,
        headers=headers,
        json=payload,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Upload URL request failed: HTTP {response.status_code}\n"
            f"Response: {response.text}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Upload URL endpoint returned non-JSON response:\n{response.text}"
        ) from exc

    upload_url = data.get("uploadUrl")
    s3_key = data.get("s3Key")
    returned_file_name = data.get("fileName")

    if not upload_url:
        raise RuntimeError(f"No uploadUrl returned: {data}")

    if not s3_key:
        raise RuntimeError(f"No s3Key returned: {data}")

    return {
        "uploadUrl": upload_url,
        "s3Key": s3_key,
        "fileName": returned_file_name or safe_file_name(item.file_path.name),
    }


def upload_file_to_presigned_url(
    session: requests.Session,
    upload_url: str,
    item: MigrationItem,
) -> None:
    metadata_headers = {
        "x-amz-meta-zendesk-id": item.zendesk_id.strip(),
    }

    if item.zendesk_ticket_create_date.strip():
        metadata_headers[
            "x-amz-meta-zendesk-ticket-create-date"
        ] = item.zendesk_ticket_create_date.strip()

    # Important:
    # Do NOT send the API key to the presigned S3 URL.
    # The API key is only used when requesting the presigned URL.
    headers = {
        "Content-Type": content_type_for(item.file_path),
        **metadata_headers,
    }

    with item.file_path.open("rb") as file_handle:
        response = request_with_retry(
            session,
            "PUT",
            upload_url,
            headers=headers,
            data=file_handle,
        )

    if not (200 <= response.status_code < 300):
        raise RuntimeError(
            f"S3 PUT failed: HTTP {response.status_code}\n"
            f"Response: {response.text}"
        )


def upload_item(
    session: requests.Session,
    base_url: str,
    api_key: str,
    api_key_header: str,
    item: MigrationItem,
    dry_run: bool = False,
) -> str:
    if not item.file_path.exists():
        raise FileNotFoundError(f"File not found: {item.file_path}")

    if not item.file_path.is_file():
        raise ValueError(f"Not a file: {item.file_path}")

    if not item.zendesk_id.strip():
        raise ValueError("Zendesk ID is blank")

    if dry_run:
        expected_key = (
            f"{item.zendesk_id.strip()}/"
            f"{safe_file_name(item.file_path.name)}"
        )
        return expected_key

    upload_info = request_presigned_upload_url(
        session,
        base_url,
        api_key,
        api_key_header,
        item,
    )

    upload_file_to_presigned_url(
        session,
        upload_info["uploadUrl"],
        item,
    )

    return upload_info["s3Key"]


def discover_folder_items(root: Path) -> list[MigrationItem]:
    """
    Folder structure:
        root/<zendesk_id>/<files...>

    Any nested folders below a Zendesk ID are traversed, but only the
    actual filename is uploaded. The Lambda/API creates the final S3 key
    under the Zendesk ID folder.
    """
    if not root.exists():
        raise FileNotFoundError(f"Migration directory not found: {root}")

    if not root.is_dir():
        raise ValueError(f"Migration path is not a directory: {root}")

    items: list[MigrationItem] = []

    for zendesk_folder in sorted(root.iterdir()):
        if not zendesk_folder.is_dir():
            print(
                f"WARNING: skipping file directly under migration root: "
                f"{zendesk_folder}"
            )
            continue

        zendesk_id = zendesk_folder.name.strip()

        for file_path in sorted(zendesk_folder.rglob("*")):
            if file_path.is_file():
                items.append(
                    MigrationItem(
                        zendesk_id=zendesk_id,
                        file_path=file_path,
                    )
                )

    return items


def read_manifest(manifest_path: Path) -> list[MigrationItem]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")

    items: list[MigrationItem] = []

    with manifest_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)

        if not reader.fieldnames:
            raise ValueError("Manifest CSV has no header row.")

        required_columns = {"zendesk_id", "file_path"}
        missing = required_columns - set(reader.fieldnames)

        if missing:
            raise ValueError(
                "Manifest is missing required columns: "
                + ", ".join(sorted(missing))
            )

        for row_number, row in enumerate(reader, start=2):
            zendesk_id = (row.get("zendesk_id") or "").strip()
            file_path_text = (row.get("file_path") or "").strip()
            create_date = (
                row.get("zendesk_ticket_create_date") or ""
            ).strip()

            if not zendesk_id:
                print(f"WARNING: row {row_number}: blank zendesk_id; skipping")
                continue

            if not file_path_text:
                print(f"WARNING: row {row_number}: blank file_path; skipping")
                continue

            items.append(
                MigrationItem(
                    zendesk_id=zendesk_id,
                    file_path=Path(file_path_text),
                    zendesk_ticket_create_date=create_date,
                )
            )

    return items


def print_summary(
    successful: list[tuple[MigrationItem, str]],
    failed: list[tuple[MigrationItem, str]],
) -> None:
    print("\n" + "=" * 72)
    print("MIGRATION SUMMARY")
    print("=" * 72)
    print(f"Successful: {len(successful)}")
    print(f"Failed:     {len(failed)}")

    if successful:
        print("\nUploaded:")
        for item, s3_key in successful:
            print(f"  OK  {item.zendesk_id} -> {s3_key}")

    if failed:
        print("\nFailed:")
        for item, error in failed:
            print(f"  FAIL {item.file_path}")
            print(f"       Zendesk ID: {item.zendesk_id}")
            print(f"       Error: {error}")


def save_failure_csv(
    failed: list[tuple[MigrationItem, str]],
    output_path: Path,
) -> None:
    if not failed:
        return

    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(
            [
                "zendesk_id",
                "file_path",
                "zendesk_ticket_create_date",
                "error",
            ]
        )

        for item, error in failed:
            writer.writerow(
                [
                    item.zendesk_id,
                    str(item.file_path),
                    item.zendesk_ticket_create_date,
                    error,
                ]
            )

    print(f"\nFailure report written to: {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Zendesk files to S3 through the existing API Gateway."
    )

    subparsers = parser.add_subparsers(
        dest="mode",
        required=True,
    )

    folder_parser = subparsers.add_parser(
        "folder",
        help="Use <root>/<zendesk_id>/<files...> structure.",
    )
    folder_parser.add_argument(
        "path",
        type=Path,
        help="Root migration directory.",
    )
    folder_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show expected S3 keys without uploading.",
    )

    manifest_parser = subparsers.add_parser(
        "manifest",
        help="Use a CSV migration manifest.",
    )
    manifest_parser.add_argument(
        "path",
        type=Path,
        help="Path to migration_manifest.csv.",
    )
    manifest_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and show expected S3 keys without uploading.",
    )

    parser.add_argument(
        "--failure-report",
        type=Path,
        default=Path("migration_failures.csv"),
        help="CSV file for failed records (default: migration_failures.csv).",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        base_url, api_key, api_key_header = get_config()

        if args.mode == "folder":
            items = discover_folder_items(args.path)
        else:
            items = read_manifest(args.path)

        if not items:
            print("No files found to migrate.")
            return 0

        print("=" * 72)
        print("S3 ZENDESK FILE MIGRATION")
        print("=" * 72)
        print(f"API base URL: {base_url}")
        print(f"Files found:  {len(items)}")
        print(f"Dry run:      {args.dry_run}")
        print()

        session = requests.Session()

        successful: list[tuple[MigrationItem, str]] = []
        failed: list[tuple[MigrationItem, str]] = []

        for index, item in enumerate(items, start=1):
            print(
                f"[{index}/{len(items)}] "
                f"{item.zendesk_id} -> {item.file_path.name}"
            )

            try:
                s3_key = upload_item(
                    session=session,
                    base_url=base_url,
                    api_key=api_key,
                    api_key_header=api_key_header,
                    item=item,
                    dry_run=args.dry_run,
                )

                successful.append((item, s3_key))
                print(f"  SUCCESS: {s3_key}")

            except Exception as exc:
                error = str(exc)
                failed.append((item, error))
                print(f"  FAILED: {error}")

        print_summary(successful, failed)
        save_failure_csv(failed, args.failure_report)

        return 1 if failed else 0

    except Exception as exc:
        print(f"\nFATAL ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
