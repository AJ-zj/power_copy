"""Offline PDF import, inspect and explicit commit."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from powercopy.data.pdf_ingest import PdfMetadata, import_pdf, load_pdf_artifact, preview_pdf, ingest_pdf
from powercopy.data.versioning import VersionedPolicyStore


def main():
    ap=argparse.ArgumentParser()
    sub=ap.add_subparsers(dest='command', required=True)
    imp=sub.add_parser('import'); imp.add_argument('pdf'); imp.add_argument('--metadata',required=True)
    imp.add_argument('--raw-dir',default='storage/raw_pdf')
    pre=sub.add_parser('preview'); pre.add_argument('raw'); pre.add_argument('--show-body',action='store_true')
    commit=sub.add_parser('commit'); commit.add_argument('raw'); commit.add_argument('--approve-sha256',required=True)
    commit.add_argument('--db',required=True)
    a=ap.parse_args()
    try:
        if a.command=='import':
            m=PdfMetadata.model_validate_json(Path(a.metadata).read_text(encoding='utf-8'))
            out={'raw_path':str(import_pdf(a.pdf,m,a.raw_dir))}
        elif a.command=='preview':
            raw=load_pdf_artifact(a.raw); p=preview_pdf(raw)
            out={'fetch_id':raw.fetch_id,'review_sha256':p.review_sha256,'raw_sha256':p.raw_sha256,
                 'content_sha256':p.content_sha256,'parser_version':p.parser_version,
                 'metadata':raw.metadata.model_dump(mode='json'),
                 'pages':[{'number':v.number,'start':v.start,'end':v.end,
                           **({'text':v.text} if a.show_body else {'head':v.text[:100],'tail':v.text[-100:]})} for v in p.pages]}
        else:
            raw=load_pdf_artifact(a.raw)
            # Check approval before even creating a database.
            if preview_pdf(raw).review_sha256 != a.approve_sha256:
                raise ValueError('REVIEW_REQUIRED_OR_STALE')
            out=asdict(ingest_pdf(VersionedPolicyStore(a.db),raw,approved_sha256=a.approve_sha256))
    except Exception as exc:
        ap.exit(1,f'{type(exc).__name__}: {exc}\n')
    print(json.dumps(out,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
