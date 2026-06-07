#!/usr/bin/env python3
"""
cobol2py — COBOL to Python translator CLI

Usage:
    cobol2py input.cbl                  # prints Python to stdout
    cobol2py input.cbl -o output.py     # writes to file
    cobol2py input.cbl --validate       # check syntax only
"""

import argparse
import sys
from pathlib import Path
from translator import translate


def main():
    parser = argparse.ArgumentParser(
        prog='cobol2py',
        description='Translate COBOL source to Python 3'
    )
    parser.add_argument('input', help='COBOL source file (.cbl / .cob)')
    parser.add_argument('-o', '--output', help='Output Python file (default: stdout)')
    parser.add_argument('--validate', action='store_true',
                        help='Validate output syntax without writing')
    parser.add_argument('--encoding', default='utf-8',
                        help='Source file encoding (default: utf-8)')

    args = parser.parse_args()

    src_path = Path(args.input)
    if not src_path.exists():
        print(f'Error: {src_path} not found', file=sys.stderr)
        sys.exit(1)

    source = src_path.read_text(encoding=args.encoding)
    python_code = translate(source)

    if args.validate:
        try:
            compile(python_code, '<translated>', 'exec')
            print(f'✓ Syntax valid — {len(python_code.splitlines())} lines generated')
        except SyntaxError as e:
            print(f'✗ Syntax error in generated code: {e}', file=sys.stderr)
            sys.exit(1)
        return

    if args.output:
        out_path = Path(args.output)
        out_path.write_text(python_code, encoding='utf-8')
        print(f'✓ Written to {out_path}')
    else:
        print(python_code)


if __name__ == '__main__':
    main()
