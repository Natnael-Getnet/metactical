#!/usr/bin/env python3
"""
Reliable backup -> self-hosted S3 uploader for Frappe/ERPNext.

Why this exists:
    Frappe's built-in S3 Backup (frappe/integrations/.../s3_backup_settings.py)
    uses boto3's default upload_file(), which fires ~10 concurrent UploadPart
    streams. Our self-hosted S3 (s3.vm01.metactical.com) throttles that with
    "SlowDownRead / please reduce your request rate" and the whole upload dies
    after 4 quick retries.

    This script replaces it with a throttling-friendly uploader:
      * serial part uploads (max_concurrency = 1)
      * large 100MB chunks so there are far fewer requests
      * botocore adaptive retry mode (client-side rate limiting)
      * an outer exponential-backoff retry loop per file
      * post-upload size verification
      * local + remote retention pruning and orphan multipart cleanup

Run it from the bench root (or anywhere) with the bench's python, e.g.:

    cd ~/frappe-bench
    ./env/bin/python s3_backup.py erp.metactical.com

Credentials, bucket, endpoint and notify email are read from the existing
"S3 Backup Settings" single doctype, so there is nothing new to configure.
"""

import os
import sys
import time
import random
import logging
import datetime

# ---------------------------------------------------------------------------
# Tunables. The defaults are chosen to be gentle on a self-hosted S3 endpoint.
# ---------------------------------------------------------------------------
MULTIPART_THRESHOLD = 200 * 1024 * 1024   # <= this -> single PUT, no multipart
MULTIPART_CHUNKSIZE = 100 * 1024 * 1024   # size of each part when multipart
MAX_CONCURRENCY = 1                        # serial parts -> no SlowDownRead
BOTO_MAX_ATTEMPTS = 10                     # botocore internal retries (adaptive)

OUTER_RETRIES = 5                          # whole-file retry attempts
OUTER_BACKOFF_BASE = 5                     # seconds; grows exponentially

KEEP_REMOTE_BACKUP_SETS = 10               # dated folders to keep in the bucket
LOCAL_BACKUP_OLDER_THAN_HOURS = 72         # frappe deletes local backups older

# On start, reuse the newest existing backup (e.g. the native scheduler's) if it
# is at most this many hours old; otherwise generate a fresh backup and upload it.
MAX_BACKUP_AGE_HOURS = 3


def _resolve_paths():
    """Figure out the bench root / sites path from this file's location."""
    bench_root = os.path.dirname(os.path.abspath(__file__))
    sites_path = os.path.join(bench_root, "sites")
    return bench_root, sites_path


def _setup_logging(bench_root):
    log_dir = os.path.join(bench_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "s3_backup.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("s3_backup")


class RunLog:
    """Wraps the stdlib logger so every message is both written to the log file
    and collected as a structured step, and persists one "S3 Backup Log" doc
    per run (date + status + JSON steps). All doc operations are best-effort:
    if the doctype is missing the backup still proceeds normally."""

    def __init__(self, logger, frappe, site):
        self._logger = logger
        self._frappe = frappe
        self.site = site
        self.steps = []
        self.doc = None

    def _record(self, level, msg, args):
        text = (msg % args) if args else msg
        self.steps.append({
            "ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": text,
        })
        return text

    def info(self, msg, *args):
        self._logger.info(self._record("INFO", msg, args))

    def warning(self, msg, *args):
        self._logger.warning(self._record("WARNING", msg, args))

    def error(self, msg, *args):
        self._logger.error(self._record("ERROR", msg, args))

    def start(self):
        try:
            self.doc = self._frappe.get_doc({
                "doctype": "S3 Backup Log",
                "log_datetime": self._frappe.utils.now_datetime(),
                "status": "Started",
                "site": self.site,
                "steps": "[]",
            }).insert(ignore_permissions=True)
            self._frappe.db.commit()
        except Exception as e:
            self._logger.warning("Could not create S3 Backup Log doc: %s", e)
            self.doc = None

    def finish(self, status, summary):
        if self.doc is None:
            return
        import json
        try:
            self.doc.status = status
            self.doc.summary = summary
            self.doc.steps = json.dumps(self.steps, indent=2)
            self.doc.save(ignore_permissions=True)
            self._frappe.db.commit()
        except Exception as e:
            self._logger.warning("Could not update S3 Backup Log doc: %s", e)


def _make_client(settings):
    import boto3
    from botocore.config import Config

    cfg = Config(
        retries={"max_attempts": BOTO_MAX_ATTEMPTS, "mode": "adaptive"},
        # keep a single long-lived connection; serial uploads don't need a pool
        max_pool_connections=4,
    )
    return boto3.client(
        "s3",
        aws_access_key_id=settings["access_key_id"],
        aws_secret_access_key=settings["secret_access_key"],
        endpoint_url=settings["endpoint_url"],
        config=cfg,
    )


def _abort_orphan_multipart_uploads(conn, bucket, prefix, log):
    """Self-hosted S3 keeps paying for half-finished multipart uploads.
    Clear any left over from previous SlowDownRead failures."""
    try:
        resp = conn.list_multipart_uploads(Bucket=bucket, Prefix=prefix)
    except Exception as e:  # listing may be unsupported; not fatal
        log.warning("Could not list multipart uploads: %s", e)
        return
    for up in resp.get("Uploads", []) or []:
        try:
            conn.abort_multipart_upload(
                Bucket=bucket, Key=up["Key"], UploadId=up["UploadId"]
            )
            log.info("Aborted orphan multipart upload: %s", up["Key"])
        except Exception as e:
            log.warning("Failed to abort multipart %s: %s", up["Key"], e)


def _upload_one(conn, bucket, filename, destpath, log):
    """Upload a single file with tuned transfer config + outer retry loop."""
    from boto3.s3.transfer import TransferConfig
    from botocore.exceptions import ClientError

    transfer = TransferConfig(
        multipart_threshold=MULTIPART_THRESHOLD,
        multipart_chunksize=MULTIPART_CHUNKSIZE,
        max_concurrency=MAX_CONCURRENCY,
        use_threads=False,
    )

    local_size = os.path.getsize(filename)
    log.info("Uploading %s (%.1f MB) -> s3://%s/%s",
             os.path.basename(filename), local_size / 1024 / 1024,
             bucket, destpath)

    last_err = None
    for attempt in range(1, OUTER_RETRIES + 1):
        try:
            conn.upload_file(filename, bucket, destpath, Config=transfer)
            # verify
            head = conn.head_object(Bucket=bucket, Key=destpath)
            remote_size = head["ContentLength"]
            if remote_size != local_size:
                raise ValueError(
                    f"size mismatch: local={local_size} remote={remote_size}"
                )
            log.info("OK %s verified (%d bytes)", destpath, remote_size)
            return
        except Exception as e:
            last_err = e
            log.warning("Attempt %d/%d failed for %s: %s",
                        attempt, OUTER_RETRIES, destpath, e)
            # clear any partial multipart left behind before retrying
            _abort_orphan_multipart_uploads(conn, bucket, destpath, log)
            if attempt < OUTER_RETRIES:
                delay = OUTER_BACKOFF_BASE * (2 ** (attempt - 1))
                delay += random.uniform(0, OUTER_BACKOFF_BASE)
                log.info("Backing off %.1fs before retry", delay)
                time.sleep(delay)

    raise RuntimeError(f"Upload failed after {OUTER_RETRIES} attempts: "
                       f"{destpath}: {last_err}")


def _prune_remote(conn, bucket, base_prefix, log):
    """Keep only the most recent KEEP_REMOTE_BACKUP_SETS dated folders."""
    try:
        paginator = conn.get_paginator("list_objects_v2")
        folders = set()
        for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix,
                                        Delimiter="/"):
            for cp in page.get("CommonPrefixes", []) or []:
                folders.add(cp["Prefix"])
        # dated folders look like <base>YYYYMMDD_HHMMSS/ -> sortable as strings
        ordered = sorted(folders)
        to_delete = ordered[:-KEEP_REMOTE_BACKUP_SETS] if \
            len(ordered) > KEEP_REMOTE_BACKUP_SETS else []
        for folder in to_delete:
            objs = []
            for page in paginator.paginate(Bucket=bucket, Prefix=folder):
                for o in page.get("Contents", []) or []:
                    objs.append({"Key": o["Key"]})
            # delete in batches of 1000 (S3 limit)
            for i in range(0, len(objs), 1000):
                conn.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": objs[i:i + 1000]},
                )
            log.info("Pruned old remote backup set: %s (%d objects)",
                     folder, len(objs))
    except Exception as e:
        # pruning must never fail the backup
        log.warning("Remote prune skipped: %s", e)


def _recent_backup_files(settings, log):
    """Return (db_filename, [files...]) for the newest existing backup set if it
    is at most MAX_BACKUP_AGE_HOURS old, otherwise None so the caller generates
    a fresh one. This lets us reuse the native scheduler's recent backup."""
    from frappe.integrations.offsite_backup_utils import get_latest_backup_file

    with_files = bool(settings["backup_files"])
    if with_files:
        db_filename, site_config, files_filename, private_files = \
            get_latest_backup_file(with_files=True)
        candidates = [db_filename, site_config, files_filename, private_files]
    else:
        db_filename, site_config = get_latest_backup_file()
        candidates = [db_filename, site_config]

    if not db_filename or not os.path.exists(db_filename):
        log.info("No existing backup found; will generate a new one.")
        return None

    # If files are expected but the latest set is missing them, it is not a
    # complete set to reuse -> generate a fresh backup instead.
    if with_files and not (files_filename and private_files
                           and os.path.exists(files_filename)
                           and os.path.exists(private_files)):
        log.info("Latest backup has no file archives; will generate a new one.")
        return None

    age_h = (time.time() - os.path.getmtime(db_filename)) / 3600.0
    if age_h > MAX_BACKUP_AGE_HOURS:
        log.info("Newest backup is %.1f h old (> %s h); will generate a new one.",
                 age_h, MAX_BACKUP_AGE_HOURS)
        return None

    log.info("Reusing recent backup (%.1f h old): %s",
             age_h, os.path.basename(db_filename))
    files = [f for f in candidates if f and os.path.exists(f)]
    return db_filename, files


def run(site):
    bench_root, sites_path = _resolve_paths()
    stdlog = _setup_logging(bench_root)
    started = datetime.datetime.now()
    stdlog.info("=== S3 backup run started for site %s ===", site)

    import frappe
    from frappe.utils.backups import new_backup
    from frappe.utils import get_backups_path, cint

    # Frappe builds log/file paths relative to cwd, so it must run from the
    # sites directory (this is what `bench` does under the hood).
    os.chdir(sites_path)
    frappe.init(site=site, sites_path=".")
    frappe.connect()

    # From here on `log` both writes the log file and records an S3 Backup Log doc.
    log = RunLog(stdlog, frappe, site)
    log.start()

    emailed = False
    try:
        doc = frappe.get_single("S3 Backup Settings")
        if not cint(doc.enabled):
            log.warning("S3 Backup Settings is disabled; running anyway.")

        settings = {
            "access_key_id": doc.access_key_id,
            "secret_access_key": doc.get_password("secret_access_key"),
            "endpoint_url": doc.endpoint_url or "https://s3.amazonaws.com",
            "bucket": doc.bucket,
            "path": (doc.backup_path or ""),
            "backup_files": cint(doc.backup_files),
            "notify_email": doc.notify_email,
        }
        if settings["path"] and not settings["path"].endswith("/"):
            settings["path"] += "/"

        # 1) reuse a recent (<= MAX_BACKUP_AGE_HOURS) backup if one exists,
        #    otherwise generate a fresh one.
        recent = _recent_backup_files(settings, log)
        if recent:
            db_filename, files = recent
        else:
            log.info("Generating backup (with_files=%s)...",
                     bool(settings["backup_files"]))
            backup = new_backup(
                older_than=LOCAL_BACKUP_OLDER_THAN_HOURS,
                ignore_files=not settings["backup_files"],
                force=True,
            )
            db_filename = backup.backup_path_db
            files = [backup.backup_path_db, backup.backup_path_conf]
            if settings["backup_files"]:
                files += [backup.backup_path_files, backup.backup_path_private_files]
            files = [f for f in files if f and os.path.exists(f)]

        # dated folder name, same convention as frappe's built-in uploader
        folder = settings["path"] + os.path.basename(db_filename)[:15] + "/"

        conn = _make_client(settings)
        bucket = settings["bucket"]

        _abort_orphan_multipart_uploads(conn, bucket, settings["path"], log)

        for f in files:
            destpath = os.path.join(folder, os.path.basename(f))
            _upload_one(conn, bucket, f, destpath, log)

        _prune_remote(conn, bucket, settings["path"], log)

        elapsed = (datetime.datetime.now() - started).total_seconds()
        msg = (f"S3 backup for {site} completed successfully in "
               f"{elapsed:.0f}s. Uploaded {len(files)} files to "
               f"s3://{bucket}/{folder}")
        log.info(msg)
        log.finish("Success", msg)

        if cint(getattr(doc, "send_email_for_successful_backup", 0)) \
                and settings["notify_email"]:
            _send_email(frappe, settings["notify_email"], True, msg, site)
            emailed = True

    except Exception:
        tb = frappe.get_traceback()
        log.error("S3 backup FAILED:\n%s", tb)
        summary = (tb.strip().splitlines() or ["Unknown error"])[-1][:500]
        log.finish("Failed", summary)
        try:
            doc = frappe.get_single("S3 Backup Settings")
            if doc.notify_email:
                _send_email(frappe, doc.notify_email, False, tb, site)
                emailed = True
        except Exception as e:
            stdlog.error("Could not send failure email: %s", e)
        raise
    finally:
        try:
            frappe.db.commit()
        except Exception:
            pass
        frappe.destroy()
        stdlog.info("=== S3 backup run finished (email_sent=%s) ===", emailed)


def _send_email(frappe, recipients, success, body, site):
    subject = (f"[{site}] S3 backup {'succeeded' if success else 'FAILED'}")
    frappe.sendmail(
        recipients=recipients.split(",") if isinstance(recipients, str) else recipients,
        subject=subject,
        message=f"<pre>{frappe.utils.escape_html(body)}</pre>",
        now=True,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python s3_backup.py <site-name>\n")
        sys.exit(2)
    try:
        run(sys.argv[1])
    except Exception:
        sys.exit(1)
