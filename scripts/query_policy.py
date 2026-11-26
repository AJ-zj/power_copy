import argparse
import json
from pydantic import ValidationError
from powercopy.data.query import PolicyQueryService, PolicyQueryError


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--db', default='storage/policies_versioned.db')
    commands=parser.add_subparsers(dest='command',required=True)
    search=commands.add_parser('search')
    search.add_argument('keyword');search.add_argument('--region',default='');search.add_argument('--limit',type=int,default=5)
    detail=commands.add_parser('detail');detail.add_argument('policy_id');detail.add_argument('--version',type=int);detail.add_argument('--offset',type=int,default=0)
    args=parser.parse_args();service=PolicyQueryService(args.db)
    try:
        result=service.search_policy(args.region,args.keyword,args.limit) if args.command=='search' else service.get_policy_detail(args.policy_id,args.version,args.offset)
    except PolicyQueryError as exc:
        print(json.dumps({'error_code':exc.code},ensure_ascii=False));return 1
    except ValidationError:
        print(json.dumps({'error_code':'INVALID_ARGUMENTS'}));return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0

if __name__=='__main__':
    raise SystemExit(main())
