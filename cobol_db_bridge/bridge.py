"""
COBOL DB Bridge
Reads COBOL flat files using COPYBOOK definitions → loads into relational DB.

Supports:
- Fixed-length flat files (EBCDIC / ASCII)
- COPYBOOK parsing (PIC clauses → column definitions)
- Output: PostgreSQL, SQLite, CSV, JSON
- EBCDIC → UTF-8 conversion
- Packed decimal (COMP-3) decoding
- Signed numeric (COMP) handling
"""

import re
import struct
import csv
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional
import io


# ── COPYBOOK PARSER ────────────────────────────────────────────────────────

@dataclass
class CopyField:
    level: int
    name: str
    pic: str
    cobol_type: str        # 'numeric', 'decimal', 'alphanumeric'
    usage: str             # 'DISPLAY', 'COMP', 'COMP-3'
    length: int            # bytes in file
    decimals: int
    signed: bool
    python_type: str       # 'int', 'float', 'str'
    offset: int = 0        # byte offset in record (computed)


def parse_copybook(source: str) -> list[CopyField]:
    """
    Parse COBOL COPYBOOK and return list of fields with byte offsets.

    Example COPYBOOK:
        01  EMPLOYEE-RECORD.
            05  EMP-ID       PIC 9(6).
            05  EMP-NAME     PIC X(30).
            05  EMP-SALARY   PIC 9(7)V99  COMP-3.
    """
    fields: list[CopyField] = []
    offset = 0

    # Normalize: remove sequence numbers (cols 1-6), join continuation lines
    lines = []
    for raw in source.splitlines():
        if len(raw) >= 7 and raw[6] == '*':
            continue
        content = raw[7:72].strip() if len(raw) >= 7 else raw.strip()
        if content:
            lines.append(content)

    # Join continued lines (no period yet)
    joined = []
    buf = ''
    for line in lines:
        buf = (buf + ' ' + line).strip() if buf else line
        if '.' in buf:
            joined.append(buf.rstrip('.'))
            buf = ''
    if buf:
        joined.append(buf)

    for stmt in joined:
        stmt = stmt.strip()

        # Skip group items (no PIC)
        pic_match = re.search(r'\bPIC(?:TURE)?\b', stmt, re.IGNORECASE)
        if not pic_match:
            continue

        # Level and name
        header = re.match(r'^(\d{2})\s+(\S+)', stmt)
        if not header:
            continue
        level = int(header.group(1))
        name  = header.group(2).replace('-', '_').lower()

        # Skip 88-level conditions
        if level == 88:
            continue

        # USAGE clause
        usage = 'DISPLAY'
        if re.search(r'\bCOMP-3\b|\bCOMPUTATIONAL-3\b', stmt, re.IGNORECASE):
            usage = 'COMP-3'
        elif re.search(r'\bCOMP\b|\bCOMPUTATIONAL\b', stmt, re.IGNORECASE):
            usage = 'COMP'

        # PIC string
        pic_str_match = re.search(
            r'\bPIC(?:TURE)?\s+(?:IS\s+)?([S9XAV()\d]+)', stmt, re.IGNORECASE
        )
        if not pic_str_match:
            continue
        pic = pic_str_match.group(1).upper()

        # Parse PIC
        signed = pic.startswith('S')
        if signed:
            pic = pic[1:]

        # Expand repetition: 9(6) → 999999
        def expand(m):
            return m.group(1) * int(m.group(2))
        pic_expanded = re.sub(r'([9XAV])\((\d+)\)', expand, pic)

        decimals = 0
        if 'V' in pic_expanded:
            parts = pic_expanded.split('V')
            integer_chars = parts[0]
            decimal_chars = parts[1] if len(parts) > 1 else ''
            decimals = len(decimal_chars)
            digit_count = len(integer_chars) + decimals
        else:
            digit_count = len(pic_expanded)

        # Determine types
        alpha_count = pic_expanded.count('X') + pic_expanded.count('A')
        num_count   = pic_expanded.count('9')

        if alpha_count > 0:
            cobol_type  = 'alphanumeric'
            python_type = 'str'
        elif decimals > 0:
            cobol_type  = 'decimal'
            python_type = 'float'
        else:
            cobol_type  = 'numeric'
            python_type = 'int'

        # Byte length in file
        if usage == 'COMP-3':
            # Packed decimal: (digits + 1) / 2 bytes
            byte_len = (digit_count + 1) // 2
        elif usage == 'COMP':
            # Binary: 2 bytes ≤4 digits, 4 bytes ≤9, 8 bytes ≤18
            if digit_count <= 4:
                byte_len = 2
            elif digit_count <= 9:
                byte_len = 4
            else:
                byte_len = 8
        else:
            # DISPLAY: one char per digit/letter
            byte_len = digit_count + (1 if signed else 0)
            if 'V' in pic:
                byte_len = digit_count  # V is implied decimal, no extra byte

        cf = CopyField(
            level=level,
            name=name,
            pic=pic,
            cobol_type=cobol_type,
            usage=usage,
            length=byte_len,
            decimals=decimals,
            signed=signed,
            python_type=python_type,
            offset=offset,
        )
        fields.append(cf)
        offset += byte_len

    # Assign offsets
    current_offset = 0
    for f in fields:
        f.offset = current_offset
        current_offset += f.length

    return fields


def record_length(fields: list[CopyField]) -> int:
    if not fields:
        return 0
    last = fields[-1]
    return last.offset + last.length


# ── ENCODINGS ──────────────────────────────────────────────────────────────

# IBM EBCDIC Code Page 037 → Python codec
EBCDIC_CODEC = 'cp037'


def decode_comp3(raw: bytes, decimals: int = 0) -> float | int:
    """
    Decode IBM packed decimal (COMP-3).
    Last nibble: C=positive, D=negative, F=unsigned.
    """
    hex_str = raw.hex()
    sign_nibble = hex_str[-1].upper()
    digits = hex_str[:-1]
    value = int(digits)
    if sign_nibble == 'D':
        value = -value
    if decimals:
        return value / (10 ** decimals)
    return value


def decode_comp(raw: bytes, signed: bool = True) -> int:
    """Decode COMP (binary) field."""
    n = len(raw)
    if n == 2:
        fmt = '>h' if signed else '>H'
    elif n == 4:
        fmt = '>i' if signed else '>I'
    else:
        fmt = '>q' if signed else '>Q'
    return struct.unpack(fmt, raw)[0]


def decode_display_numeric(raw: bytes, decimals: int, encoding: str = 'ascii') -> float | int:
    """Decode DISPLAY numeric field."""
    text = raw.decode(encoding, errors='replace').strip()
    # Handle overpunch sign (last byte encodes sign in EBCDIC)
    text = re.sub(r'[^0-9\-\.]', '', text) or '0'
    try:
        value = int(text)
        if decimals:
            return value / (10 ** decimals)
        return value
    except ValueError:
        return 0


# ── RECORD READER ──────────────────────────────────────────────────────────

class CobolFlatFileReader:
    """
    Read a COBOL fixed-length flat file record by record.

    Args:
        filepath: path to flat file
        fields: parsed CopyField list from parse_copybook()
        encoding: 'ascii', 'cp037' (EBCDIC), 'latin-1'
        newline: True if file has newlines between records
    """

    def __init__(
        self,
        filepath: str | Path,
        fields: list[CopyField],
        encoding: str = 'ascii',
        newline: bool = True,
    ):
        self.filepath  = Path(filepath)
        self.fields    = fields
        self.encoding  = encoding
        self.newline   = newline
        self.rec_len   = record_length(fields)

    def decode_field(self, raw: bytes, field: CopyField) -> Any:
        """Decode raw bytes for one field."""
        if field.usage == 'COMP-3':
            return decode_comp3(raw, field.decimals)

        if field.usage == 'COMP':
            val = decode_comp(raw, field.signed)
            if field.decimals:
                return val / (10 ** field.decimals)
            return val

        # DISPLAY
        if field.cobol_type == 'alphanumeric':
            return raw.decode(self.encoding, errors='replace').strip()

        return decode_display_numeric(raw, field.decimals, self.encoding)

    def records(self) -> Iterator[dict[str, Any]]:
        """Yield records as dicts."""
        data = self.filepath.read_bytes()

        if self.newline:
            # Records separated by \n or \r\n
            lines = data.splitlines()
            for line in lines:
                if not line.strip():
                    continue
                if len(line) < self.rec_len:
                    line = line.ljust(self.rec_len)
                yield self._decode_record(line[:self.rec_len])
        else:
            # Fixed-width, no separators
            pos = 0
            while pos + self.rec_len <= len(data):
                yield self._decode_record(data[pos:pos + self.rec_len])
                pos += self.rec_len

    def _decode_record(self, raw: bytes) -> dict[str, Any]:
        record = {}
        for f in self.fields:
            chunk = raw[f.offset:f.offset + f.length]
            record[f.name] = self.decode_field(chunk, f)
        return record

    def to_list(self) -> list[dict]:
        return list(self.records())


# ── DB WRITERS ─────────────────────────────────────────────────────────────

def _create_table_sql(table: str, fields: list[CopyField]) -> str:
    type_map = {'int': 'INTEGER', 'float': 'REAL', 'str': 'TEXT'}
    cols = ',\n    '.join(
        f'{f.name} {type_map.get(f.python_type, "TEXT")}'
        for f in fields
    )
    return f'CREATE TABLE IF NOT EXISTS {table} (\n    {cols}\n);'


def load_sqlite(
    records: list[dict],
    fields: list[CopyField],
    db_path: str,
    table: str = 'cobol_data',
) -> int:
    """Load records into SQLite database."""
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()
    cur.execute(_create_table_sql(table, fields))

    col_names = [f.name for f in fields]
    placeholders = ', '.join(['?'] * len(col_names))
    sql = f'INSERT INTO {table} ({", ".join(col_names)}) VALUES ({placeholders})'

    rows = [[r.get(c) for c in col_names] for r in records]
    cur.executemany(sql, rows)
    conn.commit()
    conn.close()
    return len(rows)


def load_postgres(
    records: list[dict],
    fields: list[CopyField],
    dsn: str,
    table: str = 'cobol_data',
) -> int:
    """
    Load records into PostgreSQL.
    Requires: pip install psycopg2-binary

    dsn format: 'postgresql://user:password@host:5432/dbname'
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        raise ImportError('pip install psycopg2-binary')

    type_map = {'int': 'INTEGER', 'float': 'NUMERIC', 'str': 'TEXT'}

    conn = psycopg2.connect(dsn)
    cur  = conn.cursor()

    # Create table
    cols_def = ', '.join(
        f'{f.name} {type_map.get(f.python_type, "TEXT")}'
        for f in fields
    )
    cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ({cols_def})')

    col_names = [f.name for f in fields]
    sql = f'INSERT INTO {table} ({", ".join(col_names)}) VALUES %s'
    rows = [tuple(r.get(c) for c in col_names) for r in records]
    psycopg2.extras.execute_values(cur, sql, rows)

    conn.commit()
    cur.close()
    conn.close()
    return len(rows)


def to_csv(records: list[dict], output_path: str) -> int:
    """Write records to CSV."""
    if not records:
        return 0
    path = Path(output_path)
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)
    return len(records)


def to_json(records: list[dict], output_path: str) -> int:
    """Write records to JSON."""
    Path(output_path).write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )
    return len(records)


# ── PUBLIC API ─────────────────────────────────────────────────────────────

def bridge(
    flat_file: str,
    copybook: str,
    target: str,
    encoding: str = 'ascii',
    table: str = 'cobol_data',
    newline: bool = True,
) -> int:
    """
    Main entry point: read COBOL flat file → write to target.

    Args:
        flat_file:  path to COBOL flat file
        copybook:   COPYBOOK source as string (or path to .cpy file)
        target:     output destination:
                    - 'sqlite:///path/to/db.sqlite'
                    - 'postgresql://user:pass@host/db'
                    - 'csv:///path/to/output.csv'
                    - 'json:///path/to/output.json'
        encoding:   file encoding ('ascii', 'cp037' for EBCDIC, 'latin-1')
        table:      database table name
        newline:    True if records are newline-separated

    Returns:
        Number of records loaded
    """
    # Load copybook from file if path given
    cb_path = Path(copybook)
    if cb_path.exists():
        copybook_src = cb_path.read_text()
    else:
        copybook_src = copybook

    fields  = parse_copybook(copybook_src)
    reader  = CobolFlatFileReader(flat_file, fields, encoding=encoding, newline=newline)
    records = reader.to_list()

    if target.startswith('sqlite:///'):
        db_path = target[len('sqlite:///'):]
        return load_sqlite(records, fields, db_path, table)

    if target.startswith('postgresql://') or target.startswith('postgres://'):
        return load_postgres(records, fields, target, table)

    if target.startswith('csv:///'):
        return to_csv(records, target[len('csv:///'):])

    if target.startswith('json:///'):
        return to_json(records, target[len('json:///'):])

    raise ValueError(f'Unknown target format: {target}')
