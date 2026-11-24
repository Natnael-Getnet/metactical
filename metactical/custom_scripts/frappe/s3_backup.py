import os

import boto3
import frappe
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

# Our self-hosted S3 server (s3.pve04.metactical.com) answers "SlowDownRead"
# when Frappe uploads with boto3's defaults (10 parallel parts, 4 retries).
# Upload fewer parts at once, in bigger chunks, and retry with backoff.
TRANSFER_CONFIG = TransferConfig(max_concurrency=2, multipart_chunksize=64 * 1024 * 1024)
CLIENT_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})


def get_s3_client():
	doc = frappe.get_single("S3 Backup Settings")
	return boto3.client(
		"s3",
		aws_access_key_id=doc.access_key_id,
		aws_secret_access_key=doc.get_password("secret_access_key"),
		endpoint_url=doc.endpoint_url or "https://s3.amazonaws.com",
		config=CLIENT_CONFIG,
	)


def upload_file_to_s3(filename, folder, conn, bucket):
	# `conn` from Frappe is ignored: it was built without retry settings.
	destpath = os.path.join(folder, os.path.basename(filename))
	print("Uploading file:", filename)
	get_s3_client().upload_file(filename, bucket, destpath, Config=TRANSFER_CONFIG)
