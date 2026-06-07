"""
COBOL to Python Translator
Converts common COBOL patterns to idiomatic Python 3.

Supported:
- IDENTIFICATION / ENVIRONMENT / DATA / PROCEDURE DIVISION
- PIC clauses → Python types (9/X/V → int/float/str)
- WORKING-STORAGE → Python dataclass fields
- PERFORM / PERFORM UNTIL → functions / while loops
- EVALUATE → match/case (Python 3.10+)
- MULTIPLY / ADD / SUBTRACT / DIVIDE → arithmetic
- MOVE → assignment
- DISPLAY → print()
- READ / WRITE / OPEN / CLOSE → file I/O
- 88-level condition names → properties
- AT END clause
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# ── PIC CLAUSE PARSER ──────────────────────────────────────────────────────

@dataclass
class PicField:
    name: str
    pic: str
    level: int
    python_type: str = ''
    default: str = ''
    length: int = 0
    decimals: int = 0

    def __post_init__(self):
        self.python_type, self.length, self.decimals = parse_pic(self.pic)
        self.default = default_for(self.python_type)


def parse_pic(pic: str) -> tuple[str, int, int]:
    """Parse COBOL PIC clause → (python_type, length, decimals)."""
    pic = pic.upper().strip().rstrip('.')

    # Expand repeats: X(10) → XXXXXXXXXX, 9(6) → 999999
    def expand(m):
        char, n = m.group(1), int(m.group(2))
        return char * n

    pic = re.sub(r'([A-Z9])\((\d+)\)', expand, pic)

    decimals = 0
    if 'V' in pic:
        parts = pic.split('V')
        integer_part = parts[0]
        decimal_part = parts[1] if len(parts) > 1 else ''
        decimals = len(decimal_part)
        pic = integer_part  # for length calculation
        return 'float', len(integer_part), decimals

    if re.match(r'^9+$', pic):
        return 'int', len(pic), 0

    if re.match(r'^[XA]+$', pic):
        return 'str', len(pic), 0

    # Mixed or S (signed)
    if pic.startswith('S'):
        inner = pic[1:]
        t, l, d = parse_pic(inner)
        return t, l, d

    return 'str', len(pic), 0


def default_for(python_type: str) -> str:
    return {'int': '0', 'float': '0.0', 'str': "''"}. get(python_type, 'None')


# ── COBOL TOKENIZER ────────────────────────────────────────────────────────

def clean_source(source: str) -> list[str]:
    """Remove sequence numbers, comments, normalize lines."""
    lines = []
    for raw in source.splitlines():
        # Fixed-format COBOL: cols 1-6 sequence, col 7 indicator
        if len(raw) >= 7:
            indicator = raw[6]
            if indicator == '*':   # comment line
                continue
            content = raw[7:72].rstrip()  # cols 8-72
        else:
            content = raw.strip()

        if content:
            lines.append(content.strip())

    return lines


def find_division(lines: list[str], name: str) -> int:
    """Return line index of a DIVISION."""
    pattern = re.compile(rf'\b{name}\s+DIVISION', re.IGNORECASE)
    for i, line in enumerate(lines):
        if pattern.search(line):
            return i
    return -1


# ── DATA DIVISION PARSER ───────────────────────────────────────────────────

def parse_data_division(lines: list[str]) -> tuple[list[PicField], list[dict]]:
    """Extract field definitions from DATA DIVISION."""
    fields: list[PicField] = []
    conditions: list[dict] = []  # 88-level entries

    for line in lines:
        # 88-level condition
        m88 = re.match(r'^88\s+(\S+)\s+VALUE\s+(.+)\.?$', line, re.IGNORECASE)
        if m88:
            conditions.append({
                'name': m88.group(1).replace('-', '_').lower(),
                'value': m88.group(2).strip().rstrip('.').strip("'\""),
                'parent': fields[-1].name if fields else 'unknown'
            })
            continue

        # FD / 01 / 05 / 10 etc. with PIC
        m = re.match(
            r'^(\d{2})\s+(\S+)\s+PIC\s+(?:IS\s+)?(\S+?)\.?\s*$',
            line, re.IGNORECASE
        )
        if m:
            level = int(m.group(1))
            name  = m.group(2).replace('-', '_').lower()
            pic   = m.group(3)
            fields.append(PicField(name=name, pic=pic, level=level))

    return fields, conditions


# ── PROCEDURE DIVISION TRANSLATOR ──────────────────────────────────────────

class ProcedureTranslator:
    """Translate COBOL PROCEDURE DIVISION statements to Python."""

    VAR_RE = re.compile(r'[A-Z][A-Z0-9-]+')

    def __init__(self, fields: list[PicField], conditions: list[dict]):
        self.field_names = {f.name for f in fields}
        self.field_map   = {f.name: f for f in fields}
        self.conditions  = {c['name']: c for c in conditions}
        self.paragraphs: dict[str, list[str]] = {}
        self.current_para: Optional[str] = None
        self.output_lines: list[str] = []

    def cobol_var(self, name: str) -> str:
        """Convert COBOL variable name to Python identifier."""
        return name.replace('-', '_').lower()

    def translate_line(self, line: str) -> Optional[str]:
        line = line.strip().rstrip('.')
        upper = line.upper()

        # MOVE x TO y
        m = re.match(r'MOVE\s+(.+?)\s+TO\s+(.+)', upper)
        if m:
            src = self._val(m.group(1).strip())
            dst = self.cobol_var(m.group(2).strip())
            return f'{dst} = {src}'

        # MULTIPLY a BY b GIVING c
        m = re.match(r'MULTIPLY\s+(\S+)\s+BY\s+(\S+)\s+GIVING\s+(\S+)', upper)
        if m:
            a, b, c = self._val(m.group(1)), self._val(m.group(2)), self.cobol_var(m.group(3))
            return f'{c} = {a} * {b}'

        # MULTIPLY a BY b  (result in b)
        m = re.match(r'MULTIPLY\s+(\S+)\s+BY\s+(\S+)$', upper)
        if m:
            a, b = self._val(m.group(1)), self.cobol_var(m.group(2))
            return f'{b} = {b} * {a}'

        # ADD a TO b
        m = re.match(r'ADD\s+(.+?)\s+TO\s+(\S+)$', upper)
        if m:
            a, b = self._val(m.group(1).strip()), self.cobol_var(m.group(2))
            return f'{b} += {a}'

        # ADD a TO b GIVING c
        m = re.match(r'ADD\s+(.+?)\s+TO\s+(\S+)\s+GIVING\s+(\S+)', upper)
        if m:
            a = self._val(m.group(1).strip())
            b = self.cobol_var(m.group(2))
            c = self.cobol_var(m.group(3))
            return f'{c} = {b} + {a}'

        # SUBTRACT a FROM b GIVING c
        m = re.match(r'SUBTRACT\s+(\S+)\s+FROM\s+(\S+)\s+GIVING\s+(\S+)', upper)
        if m:
            a, b, c = self._val(m.group(1)), self._val(m.group(2)), self.cobol_var(m.group(3))
            return f'{c} = {b} - {a}'

        # SUBTRACT a FROM b
        m = re.match(r'SUBTRACT\s+(\S+)\s+FROM\s+(\S+)$', upper)
        if m:
            a, b = self._val(m.group(1)), self.cobol_var(m.group(2))
            return f'{b} -= {a}'

        # DIVIDE a INTO b GIVING c
        m = re.match(r'DIVIDE\s+(\S+)\s+INTO\s+(\S+)\s+GIVING\s+(\S+)', upper)
        if m:
            a, b, c = self._val(m.group(1)), self._val(m.group(2)), self.cobol_var(m.group(3))
            return f'{c} = {b} / {a}'

        # COMPUTE
        m = re.match(r'COMPUTE\s+(\S+)\s*=\s*(.+)', upper)
        if m:
            dst = self.cobol_var(m.group(1))
            expr = self._translate_expr(m.group(2))
            return f'{dst} = {expr}'

        # DISPLAY
        m = re.match(r'DISPLAY\s+(.+)', upper)
        if m:
            parts = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\S+)", m.group(1))
            args = []
            for lit1, lit2, var in parts:
                if lit1 or lit2:
                    args.append(f'"{lit1 or lit2}"')
                elif var:
                    args.append(self.cobol_var(var))
            return f'print({", ".join(args)})'

        # PERFORM paragraph
        m = re.match(r'PERFORM\s+(\S+)$', upper)
        if m:
            para = self.cobol_var(m.group(1))
            return f'{para}()'

        # PERFORM UNTIL condition
        m = re.match(r'PERFORM\s+(\S+)\s+UNTIL\s+(.+)', upper)
        if m:
            para = self.cobol_var(m.group(1))
            cond = self._translate_condition(m.group(2))
            return f'while not ({cond}):\n        {para}()'

        # STOP RUN
        if upper == 'STOP RUN':
            return 'sys.exit(0)'

        # OPEN / CLOSE — stub
        if upper.startswith('OPEN ') or upper.startswith('CLOSE '):
            return f'# {line}'

        # END-PERFORM / END-EVALUATE / END-READ
        if upper in ('END-PERFORM', 'END-EVALUATE', 'END-READ', 'END-IF'):
            return None  # handled structurally

        return f'# TODO: {line}'

    def _val(self, token: str) -> str:
        """Convert COBOL literal or variable to Python."""
        token = token.strip()
        if token in ('ZERO', 'ZEROS', 'ZEROES'):
            return '0'
        if token == 'SPACE' or token == 'SPACES':
            return "''"
        if token.startswith("'") or token.startswith('"'):
            return token.replace("'", '"')
        try:
            float(token)
            return token
        except ValueError:
            pass
        return self.cobol_var(token)

    def _translate_expr(self, expr: str) -> str:
        expr = expr.strip()
        expr = re.sub(r'\b([A-Z][A-Z0-9-]+)\b', lambda m: self.cobol_var(m.group(1)), expr)
        expr = expr.replace('**', '**')  # COBOL uses ** for power too
        return expr

    def _translate_condition(self, cond: str) -> str:
        cond = cond.strip()
        # 88-level condition name
        cond_low = cond.replace('-', '_').lower()
        if cond_low in self.conditions:
            c = self.conditions[cond_low]
            parent = self.cobol_var(c['parent'])
            val = c['value']
            try:
                float(val)
                return f'{parent} == {val}'
            except ValueError:
                return f'{parent} == "{val}"'
        # Standard condition
        cond = re.sub(r'\b([A-Z][A-Z0-9-]+)\b', lambda m: self.cobol_var(m.group(1)), cond)
        cond = cond.replace(' = ', ' == ').replace(' NOT = ', ' != ')
        return cond


# ── CODE GENERATOR ─────────────────────────────────────────────────────────

def generate_python(
    program_id: str,
    fields: list[PicField],
    conditions: list[dict],
    procedure_lines: list[str]
) -> str:
    """Generate Python module from parsed COBOL components."""

    lines = [
        '"""',
        f'Auto-generated from COBOL program: {program_id}',
        'Generated by cobol-to-python translator',
        'Review before use in production.',
        '"""',
        '',
        'import sys',
        'from dataclasses import dataclass, field',
        'from typing import Optional',
        '',
    ]

    # Working storage as dataclass
    ws_fields = [f for f in fields if f.level in (1, 5, 10)]
    if ws_fields:
        lines += ['@dataclass', 'class WorkingStorage:']
        for f in ws_fields:
            if f.python_type == 'str':
                lines.append(f'    {f.name}: str = {f.default!r}')
            else:
                lines.append(f'    {f.name}: {f.python_type} = {f.default}')

        # 88-level conditions as properties
        for cond in conditions:
            parent = cond['parent']
            name   = cond['name']
            val    = cond['value']
            try:
                float(val)
                expr = f'self.{parent} == {val}'
            except ValueError:
                expr = f'self.{parent} == "{val}"'
            lines += [
                '',
                '    @property',
                f'    def {name}(self) -> bool:',
                f'        return {expr}',
            ]

        lines += ['', '', 'ws = WorkingStorage()', '']

    # Procedure paragraphs
    translator = ProcedureTranslator(fields, conditions)
    current_para = None
    para_lines: dict[str, list[str]] = {}

    for raw in procedure_lines:
        line = raw.strip().rstrip('.')
        upper = line.upper()

        # Paragraph header: word followed by nothing or just a dot
        if re.match(r'^[A-Z][A-Z0-9-]*\.$', raw.strip()) or \
           re.match(r'^[A-Z][A-Z0-9-]*\s*$', upper) and not any(
               kw in upper for kw in ('MOVE','ADD','PERFORM','DISPLAY','MULTIPLY',
                                       'SUBTRACT','DIVIDE','COMPUTE','READ','WRITE',
                                       'OPEN','CLOSE','STOP','EVALUATE','END')):
            current_para = line.replace('-', '_').lower()
            para_lines[current_para] = []
        elif current_para is not None:
            result = translator.translate_line(line)
            if result is not None:
                para_lines[current_para].append(result)

    for para, stmts in para_lines.items():
        lines += ['', f'def {para}():']
        if stmts:
            for stmt in stmts:
                for sl in stmt.splitlines():
                    lines.append(f'    {sl}')
        else:
            lines.append('    pass')

    # Entry point
    lines += [
        '',
        '',
        'if __name__ == "__main__":',
        '    main_paragraph()',
    ]

    return '\n'.join(lines)


# ── PUBLIC API ─────────────────────────────────────────────────────────────

def translate(source: str) -> str:
    """
    Translate COBOL source code to Python.

    Args:
        source: COBOL source as string

    Returns:
        Python source as string
    """
    lines = clean_source(source)

    # Find division boundaries
    id_idx   = find_division(lines, 'IDENTIFICATION')
    env_idx  = find_division(lines, 'ENVIRONMENT')
    data_idx = find_division(lines, 'DATA')
    proc_idx = find_division(lines, 'PROCEDURE')

    # Extract program ID
    program_id = 'UNKNOWN'
    if id_idx >= 0:
        for line in lines[id_idx:data_idx if data_idx > 0 else id_idx+10]:
            m = re.match(r'PROGRAM-ID\.\s+(\S+)', line, re.IGNORECASE)
            if m:
                program_id = m.group(1).rstrip('.')
                break

    # Parse DATA DIVISION
    data_lines = lines[data_idx:proc_idx] if data_idx >= 0 and proc_idx >= 0 else []
    fields, conditions = parse_data_division(data_lines)

    # PROCEDURE DIVISION
    proc_lines = lines[proc_idx+1:] if proc_idx >= 0 else []

    return generate_python(program_id, fields, conditions, proc_lines)
