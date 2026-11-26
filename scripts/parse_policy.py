import argparse
import json
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError

from powercopy.data.normalizer import ParseError
from powercopy.data.parser import (
    ParseProfile, inspect_candidates, load_raw, parse_article, to_policy_document,
)
from powercopy.data.repository import PolicyRepository


def main():
    parser = argparse.ArgumentParser(description="Offline policy parser")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("raw_file", type=Path)
    parse = sub.add_parser("parse")
    parse.add_argument("raw_file", type=Path)
    parse.add_argument("--profiles", type=Path, default=Path("config/parser_profiles.json"))
    parse.add_argument("--db", type=Path, default=Path("storage/policies.db"))
    parse.add_argument("--show-body", action="store_true")
    parse.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        raw = load_raw(args.raw_file)
        if args.command == "inspect":
            print(json.dumps(inspect_candidates(raw), ensure_ascii=False, indent=2))
            return
        profiles = json.loads(args.profiles.read_text(encoding="utf-8"))
        key = f"{raw.source_key}/{raw.target_key}"
        if key not in profiles:
            raise ParseError(f"MISSING_PROFILE:{key}")
        profile = ParseProfile.model_validate(profiles[key])
        article = parse_article(raw, profile)
        output = asdict(article)
        body = output.pop("body_text")
        output.update(fetch_id=raw.fetch_id, source_url=str(raw.source_url),
                      content_kind=raw.content_kind, body_chars=len(body),
                      retrieved_at=raw.retrieved_at.isoformat(),
                      body_head=body[:180], body_tail=body[-180:])
        if args.show_body:
            output["body_text"] = body
        if args.commit:
            document = to_policy_document(raw, article)
            repo = PolicyRepository(args.db)
            document_id = repo.add(document)
            if repo.get(document_id) != document:
                raise ParseError("ROUND_TRIP_FAILED")
            output["document_id"] = str(document_id)
            output["saved_to"] = str(args.db)
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    except (ParseError, OSError, ValidationError, json.JSONDecodeError) as exc:
        parser.exit(1, f"解析失败: {exc}\n")


if __name__ == "__main__":
    main()
