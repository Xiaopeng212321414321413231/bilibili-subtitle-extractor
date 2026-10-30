"""把本地文件 PUT 到 IMA 的 COS 桶。凭据由 MCP 的 create_media 返回。

MCP 的 create_media 只登记元数据并返回一份临时 COS 凭据，
文件本体要自己用 COS SDK 传到 cos_key 指定的位置，之后 add_knowledge 才会入库。
这个脚本就是中间那一步，从 stdin 读 JSON：

    {"file": "本地路径", "key": "cos_key", "cred": {
        "secret_id": "...", "secret_key": "...", "token": "...",
        "region": "ap-shanghai", "bucket_name": "ima-share-kb-1258344701"}}

凭据故意走 stdin 而不是命令行参数，避免落在 shell history 里。
"""
from __future__ import annotations

import json
import os
import sys

from qcloud_cos import CosConfig, CosS3Client


def main() -> int:
    cfg = json.load(sys.stdin)
    cred = cfg["cred"]
    path, key = cfg["file"], cfg["key"]
    if not os.path.isfile(path):
        print("文件不存在:", path, file=sys.stderr)
        return 2

    cli = CosS3Client(CosConfig(
        Region=cred.get("region", "ap-shanghai"),
        SecretId=cred["secret_id"],
        SecretKey=cred["secret_key"],
        Token=cred.get("token"),
        Scheme="https",
    ))
    ctype = cfg.get("content_type") or "text/html"
    with open(path, "rb") as f:
        r = cli.put_object(
            Bucket=cred.get("bucket_name", "ima-share-kb-1258344701"),
            Body=f, Key=key, ContentType=ctype,
        )
    print("OK %s  %s  ETag=%s" % (os.path.basename(path), key[-24:], r.get("ETag")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
