#!/usr/bin/env python3
"""
cobol-bridge — COBOL flat file → database / CSV / JSON loader

Usage:
    cobol-bridge data.dat schema.cpy --to sqlite:///output.db
    cobol-bridge data.dat schema.cpy --to csv:///output.csv
    cobol-bridge data.dat schema.cpy --to json:///output.json
    cobol-bridge data.dat schema.cpy --to postgresql://user:pass@host/db
    cobol-bridge data.dat schema.cpy --inspect
"""

import argparse
import sys
from pathlib import Path
from bridge import parse_copybook, CobolFlatFileReader, bridge, record_length


def main():
    parser = argparse.ArgumentParser(
        prog='cobol-bridge',
        description='Load COBOL flat files into a database or export format'
    )
    parser.add_argument('flat_file', help='COBOL flat file (.dat / .txt / binary)')
    parser.add_argument('copybook',  help='COPYBOOK file (.cpy) or inline schema')
    parser.add_argument('--to',      help='Target: sqlite:///db.sqlite | postgresql://... | csv:///out.csv | json:///out.json')
    parser.add_argument('--table',   default='cobol_data', help='Table name (default: cobol_data)')
    parser.add_argument('--encoding',default='ascii', help='File encoding: ascii, cp037 (EBCDIC), latin-1')
    parser.add_argument('--no-newline', action='store_true', help='File has no newlines between records')
    parser.add_argument('--inspect', action='store_true', help='Show schema and first 5 records, do not load')

    args = parser.parse_args()

    cb_path = Path(args.copybook)
    copybook_src = cb_path.read_text() if cb_path.exists() else args.copybook

    fields = parse_copybook(copybook_src)

    if args.inspect:
        print(f'\nSchema ({len(fields)} fields, {record_length(fields)} bytes/record):')
        print(f'{"NAME":<30} {"TYPE":<8} {"OFFSET":>6} {"LEN":>4} {"DEC":>4} {"USAGE":<8}')
        print('-' * 65)
        for f in fields:
            print(f'{f.name:<30} {f.python_type:<8} {f.offset:>6} {f.length:>4} {f.decimals:>4} {f.usage:<8}')

        print('\nFirst 5 records:')
        reader = CobolFlatFileReader(
            args.flat_file, fields,
            encoding=args.encoding,
            newline=not args.no_newline
        )
        for i, rec in enumerate(reader.records()):
            if i >= 5:
                break
            print(f'  [{i+1}] {rec}')
        return

    if not args.to:
        print('Error: --to required (or use --inspect)', file=sys.stderr)
        sys.exit(1)

    count = bridge(
        flat_file=args.flat_file,
        copybook=copybook_src,
        target=args.to,
        encoding=args.encoding,
        table=args.table,
        newline=not args.no_newline,
    )
    print(f'✓ Loaded {count} records → {args.to}')


if __name__ == '__main__':
    main()
