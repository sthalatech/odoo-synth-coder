"""S3 staging for uploaded dump files. Uploaded .sql/.zip are streamed to the
configured bucket, then a short-lived presigned URL is handed to the restore
task (so the large file never passes through the Fargate task def)."""
from __future__ import annotations
import uuid
from typing import BinaryIO

import boto3

from . import config


def stage_upload(fileobj: BinaryIO, filename: str) -> str:
    """Upload a file object to S3 and return a presigned GET URL."""
    bucket = config.dump_s3_bucket()
    if not bucket:
        raise RuntimeError(
            "no S3 bucket configured (set aws.dump_s3_bucket_env -> env var in config.yml)"
        )
    prefix = config.dump_s3_prefix().rstrip("/")
    safe = filename.replace("/", "_")
    key = f"{prefix}/{uuid.uuid4().hex[:12]}/{safe}"
    s3 = boto3.client("s3", region_name=config.require("AWS_REGION"))
    s3.upload_fileobj(fileobj, bucket, key)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=6 * 3600,
    )
    return url
