"""Strict, schema-only PostgreSQL DDL parsing for the public alpha.

Input is treated as untrusted text and is never passed to a database. The
parser deliberately supports a small CREATE TABLE subset and rejects every
unknown statement. It is not a complete PostgreSQL grammar or dump importer.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from opsgraph.domain.models import stable_hash


class ColumnSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    data_type: str
    nullable: bool = True
    default: str | None = None
    primary_key: bool = False


class TableSchema(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_name: str
    table_name: str
    columns: tuple[ColumnSchema, ...]


class SchemaSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    dialect: str = "postgresql"
    tables: tuple[TableSchema, ...]
    fingerprint: str


class SchemaParseError(ValueError):
    """Raised when untrusted DDL is unsupported or unsafe."""


_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|COPY|CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION|"
    r"CREATE\s+TRIGGER|CREATE\s+EXTENSION|DO|CALL|ALTER|DROP|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)
_CREATE_TABLE = re.compile(
    r'^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>(?:"[^"]+"|[A-Za-z_]\w*)'
    r'(?:\.(?:"[^"]+"|[A-Za-z_]\w*))?)\s*\((?P<body>.*)\)$',
    re.IGNORECASE | re.DOTALL,
)
_COLUMN = re.compile(r'^(?P<name>"[^"]+"|[A-Za-z_]\w*)\s+(?P<rest>.+)$', re.DOTALL)
_CONSTRAINT_PREFIX = re.compile(
    r"^(?:CONSTRAINT\s+\S+\s+)?(?:PRIMARY\s+KEY|FOREIGN\s+KEY|UNIQUE|CHECK)\b",
    re.IGNORECASE,
)
_TYPE = re.compile(
    r"^(?P<type>(?:(?:timestamp|time)(?:\s+(?:with|without)\s+time\s+zone)?|"
    r"double\s+precision|character\s+varying(?:\s*\([^)]*\))?|"
    r"(?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*(?:\s*\([^)]*\))?)(?:\[\])?)"
    r"(?P<attrs>.*)$",
    re.IGNORECASE | re.DOTALL,
)


class PostgresSchemaParser:
    """Parse only CREATE TABLE statements and return a stable snapshot."""

    def parse(self, ddl: str) -> SchemaSnapshot:
        if not ddl or not ddl.strip():
            raise SchemaParseError("DDL input is empty")
        if "$$" in ddl or "\\copy" in ddl.lower():
            raise SchemaParseError("procedural and psql commands are not supported")
        cleaned = self._strip_comments(ddl)
        if _FORBIDDEN.search(cleaned):
            raise SchemaParseError("DDL contains a forbidden statement")
        statements = self._split_statements(cleaned)
        if not statements:
            raise SchemaParseError("DDL contains no statements")
        tables = tuple(self._parse_table(statement) for statement in statements)
        identities = [(table.schema_name, table.table_name) for table in tables]
        if len(identities) != len(set(identities)):
            raise SchemaParseError("duplicate table definitions are not allowed")
        ordered = tuple(sorted(tables, key=lambda table: (table.schema_name, table.table_name)))
        payload = [table.model_dump(mode="json") for table in ordered]
        return SchemaSnapshot(tables=ordered, fingerprint=stable_hash(payload))

    def inspect(self, ddl: str) -> SchemaSnapshot:
        """Native `core.schema.inspect` handler; parsing only, never execution."""

        return self.parse(ddl)

    @staticmethod
    def _strip_comments(text: str) -> str:
        if "/*" in text or "*/" in text:
            # Avoid accepting malformed/nested block comments in the alpha grammar.
            raise SchemaParseError("block comments are not supported")
        return "\n".join(line.split("--", 1)[0] for line in text.splitlines())

    @staticmethod
    def _split_statements(text: str) -> list[str]:
        statements: list[str] = []
        start = 0
        depth = 0
        quote: str | None = None
        for index, char in enumerate(text):
            if quote:
                if char == quote:
                    if index + 1 < len(text) and text[index + 1] == quote:
                        continue
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    raise SchemaParseError("unbalanced parentheses")
            elif char == ";" and depth == 0:
                statement = text[start:index].strip()
                if statement:
                    statements.append(statement)
                start = index + 1
        if quote or depth:
            raise SchemaParseError("unterminated quote or parentheses")
        tail = text[start:].strip()
        if tail:
            statements.append(tail)
        return statements

    def _parse_table(self, statement: str) -> TableSchema:
        match = _CREATE_TABLE.fullmatch(statement.strip())
        if not match:
            raise SchemaParseError("only CREATE TABLE statements are supported")
        schema_name, table_name = self._qualified_name(match.group("name"))
        parts = self._split_commas(match.group("body"))
        table_primary_keys: set[str] = set()
        columns: list[ColumnSchema] = []
        for part in parts:
            if _CONSTRAINT_PREFIX.match(part):
                primary = re.search(r"PRIMARY\s+KEY\s*\(([^)]*)\)", part, re.IGNORECASE)
                if primary:
                    table_primary_keys.update(
                        self._identifier(item.strip()) for item in primary.group(1).split(",")
                    )
                continue
            columns.append(self._parse_column(part))
        if not columns:
            raise SchemaParseError(f"table {schema_name}.{table_name} has no columns")
        names = [column.name for column in columns]
        if len(names) != len(set(names)):
            raise SchemaParseError(f"table {schema_name}.{table_name} has duplicate columns")
        unknown_keys = table_primary_keys.difference(names)
        if unknown_keys:
            raise SchemaParseError("primary key references an unknown column")
        columns = [
            column.model_copy(update={"primary_key": True})
            if column.name in table_primary_keys
            else column
            for column in columns
        ]
        return TableSchema(schema_name=schema_name, table_name=table_name, columns=tuple(columns))

    def _parse_column(self, definition: str) -> ColumnSchema:
        match = _COLUMN.fullmatch(definition.strip())
        if not match:
            raise SchemaParseError(f"invalid column definition: {definition[:80]}")
        name = self._identifier(match.group("name"))
        type_match = _TYPE.fullmatch(match.group("rest").strip())
        if not type_match:
            raise SchemaParseError(f"unsupported type for column {name}")
        data_type = " ".join(type_match.group("type").split()).lower()
        attrs = type_match.group("attrs").strip()
        if attrs and not re.fullmatch(
            r"(?:(?:NOT\s+NULL|NULL|PRIMARY\s+KEY|UNIQUE|DEFAULT\s+(?:'[^']*'|"
            r"[-+]?\d+(?:\.\d+)?|TRUE|FALSE|NULL|CURRENT_TIMESTAMP))(?:\s+|$))*",
            attrs,
            re.IGNORECASE,
        ):
            raise SchemaParseError(f"unsupported attributes for column {name}")
        default_match = re.search(
            r"\bDEFAULT\s+('(?:[^']|'')*'|[-+]?\d+(?:\.\d+)?|TRUE|FALSE|NULL|CURRENT_TIMESTAMP)",
            attrs,
            re.IGNORECASE,
        )
        return ColumnSchema(
            name=name,
            data_type=data_type,
            nullable=not bool(re.search(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", attrs, re.I)),
            default=default_match.group(1) if default_match else None,
            primary_key=bool(re.search(r"\bPRIMARY\s+KEY\b", attrs, re.I)),
        )

    @staticmethod
    def _split_commas(body: str) -> list[str]:
        values: list[str] = []
        start = 0
        depth = 0
        quote: str | None = None
        for index, char in enumerate(body):
            if quote:
                if char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                values.append(body[start:index].strip())
                start = index + 1
        values.append(body[start:].strip())
        if any(not value for value in values):
            raise SchemaParseError("empty table element")
        return values

    @classmethod
    def _qualified_name(cls, value: str) -> tuple[str, str]:
        parts = [cls._identifier(part) for part in value.split(".")]
        return ("public", parts[0]) if len(parts) == 1 else (parts[0], parts[1])

    @staticmethod
    def _identifier(value: str) -> str:
        if value.startswith('"'):
            value = value[1:-1].replace('""', '"')
        if not value or len(value) > 128:
            raise SchemaParseError("identifier must contain 1-128 characters")
        return value
