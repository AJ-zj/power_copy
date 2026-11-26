"""Offline ingest of a previously captured policy JSON; no network calls."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from powercopy.data.parser import load_raw, ParseProfile
from powercopy.data.normalizer import ParseError
from powercopy.data.versioning import VersionedPolicyStore, VersionError
from pydantic import ValidationError


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='storage/policies_versioned.db')
    sub = ap.add_subparsers(dest='command', required=True)
    ingest = sub.add_parser('ingest')
    ingest.add_argument('raw_path')
    ingest.add_argument('--profiles', default='config/parser_profiles.json')
    history = sub.add_parser('history')
    history.add_argument('policy_id')
    sub.add_parser('counts')
    args = ap.parse_args()
    try:
        if args.command == 'ingest':
            raw = load_raw(args.raw_path)
            profiles = json.loads(Path(args.profiles).read_text(encoding='utf-8'))
            profile = ParseProfile.model_validate(profiles[f'{raw.source_key}/{raw.target_key}'])
            store = VersionedPolicyStore(args.db)
            output = asdict(store.ingest(raw, profile))
        else:
            store = VersionedPolicyStore(args.db)
            output = store.counts() if args.command == 'counts' else {
                'versions': store.history(args.policy_id),
                'observations': store.observations(args.policy_id)}
    except (VersionError, ParseError, ValidationError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
        ap.exit(1, f'{type(exc).__name__}: {exc}\n')
    print(json.dumps(output, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
