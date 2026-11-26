"""Offline build; use a dedicated output path (not an existing business file)."""
import argparse
import json
from pathlib import Path
import tempfile
from powercopy.rag.chunking import ChunkConfig, build_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default='storage/policies_versioned.db')
    parser.add_argument('--raw-pdf-dir', default='storage/raw_pdf')
    parser.add_argument('--out', default='storage/rag/chunks.json')
    parser.add_argument('--max-chars', type=int, default=500)
    parser.add_argument('--overlap-chars', type=int, default=80)
    args = parser.parse_args()
    out = Path(args.out).resolve()
    db = Path(args.db).resolve()
    if out == db or out.suffix.lower() != '.json':
        parser.error('output must be a separate JSON manifest')
    manifest = build_manifest(db, ChunkConfig(args.max_chars,args.overlap_chars), raw_pdf_dir=args.raw_pdf_dir)
    # Never overwrite an unrelated raw JSON or another file.
    if out.exists():
        try:
            previous = json.loads(out.read_text(encoding='utf-8'))
            if previous.get('schema') != 'policy-chunk-manifest-v1':
                raise ValueError()
        except (ValueError, AttributeError, OSError):
            parser.error('refusing to overwrite a non-manifest file')
    out.parent.mkdir(parents=True,exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8', dir=out.parent,delete=False) as f:
            tmp = Path(f.name)
            json.dump(manifest,f,ensure_ascii=False,indent=2)
        tmp.replace(out)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    print(json.dumps(dict(documents=len(manifest['documents']), chunks=len(manifest['chunks']),
        manifest_sha256=manifest['manifest_sha256'], output=str(out)),ensure_ascii=False))

if __name__ == '__main__':
    main()
