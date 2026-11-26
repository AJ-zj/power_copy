"""真实政策网页采集脚本。

用法：
    python -m scripts.fetch_policy ndrc market_rules

只发起一次 GET 请求，不重试、不跟随重定向。
"""

import argparse
import base64
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from powercopy.data.sources import (
    build_source_registry,
)
from powercopy.data.fetcher import (
    HttpFetcher,
    FetchError,
)


RAW_DIR = Path("storage/raw")


def main():
    parser = argparse.ArgumentParser(
        description="从已注册的官方数据源采集一个目标页面"
    )
    parser.add_argument("source_key", help="数据源 key，例如 ndrc")
    parser.add_argument("target_key", help="采集目标 key，例如 market_rules")
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="网络超时秒数（默认 10）",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=2 * 1024 * 1024,
        help="最大响应字节数（默认 2 MiB）",
    )
    args = parser.parse_args()

    registry = build_source_registry()
    fetcher = HttpFetcher(
        registry,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
    )

    try:
        result = fetcher.fetch(
            args.source_key,
            args.target_key,
        )
    except FetchError as exc:
        print(
            f"采集失败: {exc.code}, "
            f"HTTP={exc.status_code}"
        )
        raise SystemExit(1) from None
    except ValueError as exc:
        print(f"配置错误: {exc}")
        raise SystemExit(1) from None

    raw_dir = RAW_DIR
    raw_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    fetch_id = uuid4().hex
    data = asdict(result)
    body = data.pop("body")

    data["fetch_id"] = fetch_id
    data["body_size"] = len(body)
    data["retrieved_at"] = (
        result.retrieved_at.isoformat()
    )
    data["body_base64"] = (
        base64.b64encode(body)
        .decode("ascii")
    )

    path = raw_dir / f"{fetch_id}.json"
    temp_path = raw_dir / f"{fetch_id}.tmp"

    try:
        temp_path.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)

    print(
        json.dumps(
            {
                "fetch_id": fetch_id,
                "source_key": result.source_key,
                "source_url": result.source_url,
                "content_kind": result.content_kind,
                "status_code": result.status_code,
                "content_type": result.content_type,
                "charset": result.charset,
                "duration_ms": result.duration_ms,
                "body_size": len(body),
                "saved_to": str(path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
