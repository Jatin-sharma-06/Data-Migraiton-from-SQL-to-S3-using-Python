#!/usr/bin/env python3
"""
SQL Server + Local Ticket Attachments -> Amazon S3 Migration

FINAL S3 STRUCTURE
------------------
tpci-zendesk-attachment/
├── 10001/
│   ├── file1.pdf
│   └── image.jpg
├── 12345/
│   ├── file2.pdf
│   └── video.mp4
├── 12346/
│   └── document.docx
└── sql-export/
    └── poc/
        └── dbo/
            └── sales/
                └── YYYYMMDDTHHMMSSZ.csv

The media/attachment folders are NOT placed under a "media/" prefix.

Required environment variables:
    SQL_SERVER
    SQL_DATABASE
    SQL_USERNAME
    SQL_PASSWORD
    S3_BUCKET

Optional:
    AWS_REGION (default: us-east-1)

Python packages:
    pip install boto3 pyodbc

Expected local attachment structure:
    media_files/
        10001/
            file1.pdf
            image.jpg
        12345/
            file2.pdf
            video.mp4
        12346/
            document.docx

Usage:
    python sql_and_media_to_s3.py --mode sql --tables sales
    python sql_and_media_to_s3.py --mode media --media-folder .\media_files
    python sql_and_media_to_s3.py --mode both --media-folder .\media_files
    python sql_and_media_to_s3.py --mode both --media-folder .\media_files --dry-run
"""

from __future__ import annotations

import argparse
import csv
import io
import mimetypes
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


# ============================================================
# CONFIGURATION
# ============================================================

SQL_SERVER = os.getenv("SQL_SERVER", "localhost,1433")
SQL_DATABASE = os.getenv("SQL_DATABASE", "poc")
SQL_USERNAME = os.getenv("SQL_USERNAME", "sa")
SQL_PASSWORD = os.getenv("SQL_PASSWORD")

S3_BUCKET = os.getenv("S3_BUCKET")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".svg", ".heic",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".csv", ".txt", ".json",
}


@dataclass
class MigrationResult:
    succeeded: int = 0
    failed: int = 0


# ============================================================
# VALIDATION
# ============================================================

def validate_s3_config() -> None:
    if not S3_BUCKET:
        raise RuntimeError(
            "S3_BUCKET is not set.\n"
            'PowerShell example: $env:S3_BUCKET="tpci-zendesk-attachment"'
        )


def validate_sql_config() -> None:
    if not SQL_PASSWORD:
        raise RuntimeError(
            "SQL_PASSWORD is not set.\n"
            'PowerShell example: $env:SQL_PASSWORD="your-password"'
        )


# ============================================================
# AWS S3
# ============================================================

def get_s3_client():
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is not installed. Run: pip install boto3"
        ) from exc

    try:
        return boto3.client("s3", region_name=AWS_REGION)
    except Exception as exc:
        raise RuntimeError(
            "Unable to create the S3 client. Check your AWS credentials/profile."
        ) from exc


def upload_bytes(
    s3,
    key: str,
    data: bytes,
    content_type: str,
    dry_run: bool,
) -> None:
    if dry_run:
        print(f"    [DRY RUN] -> s3://{S3_BUCKET}/{key}")
        return

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=data,
        ContentType=content_type,
    )

    print(f"    OK -> s3://{S3_BUCKET}/{key}")


def upload_file(
    s3,
    key: str,
    file_path: Path,
    dry_run: bool,
) -> None:
    content_type, _ = mimetypes.guess_type(file_path.name)
    content_type = content_type or "application/octet-stream"

    size_mb = file_path.stat().st_size / (1024 * 1024)

    if dry_run:
        print(
            f"    [DRY RUN] {file_path.name} "
            f"({size_mb:.1f} MB) -> s3://{S3_BUCKET}/{key}"
        )
        return

    # boto3 manages the upload without requiring the complete file
    # to be loaded into memory.
    s3.upload_file(
        str(file_path),
        S3_BUCKET,
        key,
        ExtraArgs={"ContentType": content_type},
    )

    print(
        f"    OK {file_path.name} "
        f"({size_mb:.1f} MB) -> s3://{S3_BUCKET}/{key}"
    )


# ============================================================
# SQL SERVER
# ============================================================

def get_sql_connection():
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError(
            "pyodbc is not installed. Run: pip install pyodbc"
        ) from exc

    drivers = [
        driver
        for driver in pyodbc.drivers()
        if "SQL Server" in driver
    ]

    if not drivers:
        raise RuntimeError(
            "Microsoft ODBC Driver for SQL Server was not found."
        )

    driver = drivers[-1]

    connection_string = (
        f"DRIVER={{{driver}}};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"UID={SQL_USERNAME};"
        f"PWD={SQL_PASSWORD};"
        "TrustServerCertificate=yes;"
        "Encrypt=no;"
    )

    try:
        return pyodbc.connect(connection_string, timeout=15)
    except pyodbc.Error as exc:
        raise RuntimeError(
            "SQL Server connection failed.\n"
            f"Server: {SQL_SERVER}\n"
            f"Database: {SQL_DATABASE}\n"
            f"Username: {SQL_USERNAME}\n"
            f"Driver: {driver}\n"
            f"Original error: {exc}"
        ) from exc


def list_tables(conn) -> list[str]:
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
    )

    return [f"{row[0]}.{row[1]}" for row in cursor.fetchall()]


def split_table_name(table_name: str) -> tuple[str, str]:
    if "." in table_name:
        schema, table = table_name.split(".", 1)
        return schema, table

    return "dbo", table_name


def quote_sql_identifier(name: str) -> str:
    escaped = name.replace("]", "]]")
    return f"[{escaped}]"


def table_to_csv(
    conn,
    schema: str,
    table: str,
) -> tuple[bytes, int]:
    qualified_name = (
        f"{quote_sql_identifier(schema)}."
        f"{quote_sql_identifier(table)}"
    )

    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {qualified_name}")

    columns = [column[0] for column in cursor.description]

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)

    row_count = 0

    for row in cursor:
        writer.writerow(row)
        row_count += 1

    return buffer.getvalue().encode("utf-8"), row_count


# ============================================================
# SQL MIGRATION
# ============================================================

def run_sql_migration(
    requested_tables: Iterable[str],
    dry_run: bool,
) -> MigrationResult:
    validate_sql_config()
    validate_s3_config()

    print("\n" + "-" * 60)
    print("PART 1 - SQL SERVER -> S3")
    print("-" * 60)
    print(f"  Server : {SQL_SERVER}")
    print(f"  DB     : {SQL_DATABASE}")
    print(f"  Target : s3://{S3_BUCKET}/sql-export/")
    print()

    conn = get_sql_connection()
    s3 = None if dry_run else get_s3_client()

    result = MigrationResult()

    try:
        all_tables = list_tables(conn)

        if not all_tables:
            print("  No tables found.")
            return result

        requested = list(requested_tables)

        if requested:
            available = set(all_tables)
            targets = [table for table in requested if table in available]

            missing = [table for table in requested if table not in available]
            if missing:
                print(
                    "  WARNING: table(s) not found: "
                    + ", ".join(missing)
                )
        else:
            targets = all_tables

        if not targets:
            print("  No matching tables found.")
            return result

        print(f"  Tables: {targets}")
        print()

        timestamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )

        for table_name in targets:
            print(f"  [{table_name}]")

            try:
                schema, table = split_table_name(table_name)

                data, row_count = table_to_csv(
                    conn,
                    schema,
                    table,
                )

                key = (
                    f"sql-export/"
                    f"{SQL_DATABASE}/"
                    f"{schema}/"
                    f"{table}/"
                    f"{timestamp}.csv"
                )

                upload_bytes(
                    s3,
                    key,
                    data,
                    "text/csv; charset=utf-8",
                    dry_run,
                )

                print(f"    Rows exported: {row_count}")
                result.succeeded += 1

            except Exception as exc:
                print(f"    FAILED: {exc}")
                result.failed += 1

    finally:
        conn.close()

    return result


# ============================================================
# LOCAL TICKET ATTACHMENTS
# ============================================================

def run_media_migration(
    media_folder: Path,
    dry_run: bool,
) -> MigrationResult:
    validate_s3_config()

    print("\n" + "-" * 60)
    print("PART 2 - LOCAL TICKET ATTACHMENTS -> S3")
    print("-" * 60)
    print(f"  Source : {media_folder}")
    print(f"  Target : s3://{S3_BUCKET}/<ticket_id>/")
    print()

    if not media_folder.exists():
        raise RuntimeError(
            f"Folder not found: {media_folder}"
        )

    if not media_folder.is_dir():
        raise RuntimeError(
            f"Not a directory: {media_folder}"
        )

    s3 = None if dry_run else get_s3_client()
    result = MigrationResult()

    found_any = False

    # IMPORTANT:
    # The immediate folder name is the Ticket/Zendesk ID.
    #
    # media_files/
    #   12345/
    #       invoice.pdf
    #
    # becomes:
    #
    # S3:
    #   12345/invoice.pdf
    #
    # There is NO "media/" prefix.

    for ticket_folder in sorted(media_folder.iterdir()):
        if not ticket_folder.is_dir():
            print(
                f"  SKIP: {ticket_folder.name} "
                "(expected a ticket-ID folder)"
            )
            continue

        ticket_id = ticket_folder.name.strip()

        if not ticket_id:
            print("  SKIP: blank ticket-ID folder")
            continue

        for file_path in sorted(ticket_folder.rglob("*")):
            if not file_path.is_file():
                continue

            if file_path.suffix.lower() not in MEDIA_EXTENSIONS:
                print(
                    f"  SKIP unsupported type: {file_path.name}"
                )
                continue

            found_any = True

            # Preserve nested paths below the ticket folder.
            relative_path = (
                file_path
                .relative_to(ticket_folder)
                .as_posix()
            )

            s3_key = f"{ticket_id}/{relative_path}"

            print(
                f"  {ticket_id} -> {relative_path}"
            )

            try:
                upload_file(
                    s3,
                    s3_key,
                    file_path,
                    dry_run,
                )
                result.succeeded += 1

            except Exception as exc:
                print(f"    FAILED: {exc}")
                result.failed += 1

    if not found_any:
        print("  No supported files found.")

    return result


# ============================================================
# COMMAND LINE
# ============================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate SQL Server tables and ticket attachments "
            "to Amazon S3."
        )
    )

    parser.add_argument(
        "--mode",
        choices=("sql", "media", "both"),
        default="both",
        help="Migration mode. Default: both.",
    )

    parser.add_argument(
        "--tables",
        nargs="*",
        default=[],
        help=(
            "SQL tables to migrate. "
            "Example: sales customers. "
            "Default: all tables."
        ),
    )

    parser.add_argument(
        "--media-folder",
        type=Path,
        default=None,
        help=(
            "Folder containing <ticket_id>/<attachments>."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show the migration plan without uploading to S3."
        ),
    )

    return parser.parse_args()


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    args = parse_arguments()

    print("=" * 60)
    print("SQL SERVER + TICKET ATTACHMENTS -> AMAZON S3")
    print("=" * 60)
    print(f"  Mode       : {args.mode}")
    print(f"  SQL Server : {SQL_SERVER}")
    print(f"  Database   : {SQL_DATABASE}")
    print(f"  S3 Bucket  : {S3_BUCKET or '<NOT SET>'}")
    print(f"  AWS Region : {AWS_REGION}")
    print(f"  Dry run    : {args.dry_run}")

    sql_result = MigrationResult()
    media_result = MigrationResult()

    try:
        if args.mode in ("sql", "both"):
            sql_result = run_sql_migration(
                requested_tables=args.tables,
                dry_run=args.dry_run,
            )

        if args.mode in ("media", "both"):
            if args.media_folder is None:
                raise RuntimeError(
                    "--media-folder is required for media/both mode.\n"
                    "Example:\n"
                    '  python sql_and_media_to_s3.py --mode media '
                    '--media-folder ".\\media_files"'
                )

            media_result = run_media_migration(
                media_folder=args.media_folder,
                dry_run=args.dry_run,
            )

    except Exception as exc:
        print("\nERROR:")
        print(f"  {exc}")
        return 2

    total_success = (
        sql_result.succeeded + media_result.succeeded
    )
    total_failed = (
        sql_result.failed + media_result.failed
    )

    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    print(
        f"  SQL tables  : "
        f"OK {sql_result.succeeded} | FAILED {sql_result.failed}"
    )
    print(
        f"  Attachments : "
        f"OK {media_result.succeeded} | FAILED {media_result.failed}"
    )
    print(
        f"  TOTAL       : "
        f"OK {total_success} | FAILED {total_failed}"
    )

    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
