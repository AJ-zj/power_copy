"""Manual pipeline runner; offline is default, live requires explicit approval."""
import argparse
import json
from pathlib import Path
from powercopy.data.pipeline import IngestionPipeline
from powercopy.data.versioning import VersionedPolicyStore


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='storage/policies_versioned.db')
    ap.add_argument('--profiles', default='config/parser_profiles.json')
    ap.add_argument('--index-selectors')
    ap.add_argument('--raw-dir', default='storage/raw')
    ap.add_argument('--run-dir', default='storage/ingestion_runs')
    sub = ap.add_subparsers(dest='mode', required=True)
    off = sub.add_parser('replay')
    off.add_argument('paths', nargs='+')
    live = sub.add_parser('live')
    live.add_argument('--approval', required=True)
    live.add_argument('targets', nargs='+', help='source_key/target_key')
    args = ap.parse_args()
    profiles = read_json(args.profiles)
    selectors = read_json(args.index_selectors)
    kwargs = dict(raw_dir=args.raw_dir, run_dir=args.run_dir)
    if args.mode == 'live':
        from powercopy.data.sources import build_source_registry
        from powercopy.data.fetcher import HttpFetcher
        registry = build_source_registry()
        kwargs.update(registry=registry, fetcher=HttpFetcher(registry), approvals=read_json(args.approval))
    pipeline = IngestionPipeline(VersionedPolicyStore(args.db), profiles, **kwargs)
    report = (pipeline.replay(args.paths, selectors) if args.mode == 'replay' else
              pipeline.live([tuple(t.split('/', 1)) for t in args.targets], selectors))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())
