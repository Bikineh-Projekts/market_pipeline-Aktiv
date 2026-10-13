#!/usr/bin/env python3
"""
Refuse to publish an export that contains a credential.

    python tools/check_export_safe.py export/

Exit 0 when clean, 1 when something looks like a live secret.

WHY A TOOL AND NOT A grep
-------------------------
The obvious check is `grep -rEI` over the folder, and it does not work:
`-I` skips binary files, and almost everything in an export is binary. The
CSVs are gzipped and the workbook is a zip archive, so a key sitting in a
`log_api_call.error_msg` cell passes a grep untouched and is then published.

That is not a hypothetical cell. `requests` puts the whole request URL into
the string form of a connection error, and two of the three providers carry
the API key in the query string -- see pipeline/redact.py, which strips it
on the way INTO the database. This is the backstop for rows written before
that existed, and for anything the scrubber does not recognise.

The export branch is public on a public repository, and a force-push cannot
be taken back, so the check runs on the decompressed bytes.
"""

from __future__ import annotations

import gzip
import pathlib
import re
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# One definition of "that is obviously a placeholder", shared with the
# check that guards the source tree. Two definitions would drift, and the
# cost of drift here is a false alarm that blocks every export -- or, worse,
# a real key waved through because only one of the two knew about it.
from check_no_credentials import is_placeholder  # noqa: E402

# A key=value pair carrying something secret.
SECRET_PARAM = re.compile(
    r"(api[-_]?key|access[-_]?token|auth[-_]?token|apikey|token|"
    r"password|passwd|pwd|secret)"
    r"\s*[=:]\s*"
    r"([^&\s'\"<>|,;)\]}]+)",
    re.IGNORECASE,
)

# scheme://user:password@host
DSN = re.compile(
    r"postgres(?:ql)?://(?P<user>[^:/@\s]+):(?P<password>[^@\s]+)@",
    re.IGNORECASE,
)

MAX_BYTES = 200 * 1024 * 1024  # do not read an unbounded file into memory


def findings_in(text: str, where: str) -> list[str]:
    """
    Every credential-shaped thing in `text` that is not plainly a stand-in.

    `***` is what redact.py leaves behind, and `PASSWORD` is what the
    documentation uses -- is_placeholder() recognises both, along with
    ALL_CAPS names, angle brackets and anything under eight characters.
    """
    out: list[str] = []

    for match in DSN.finditer(text):
        if is_placeholder(match.group("user"), match.group("password")):
            continue
        out.append(f"{where}: connection string for user "
                   f"'{match.group('user')}' with what looks like a real "
                   f"password")

    for match in SECRET_PARAM.finditer(text):
        name, value = match.group(1), match.group(2)
        # The parameter name stands in for the user: `apikey=apikey` is
        # as much a stand-in as `password=PASSWORD`.
        if is_placeholder(name, value):
            continue
        out.append(f"{where}: {name}= followed by {len(value)} "
                   f"characters that look like a real secret")
    return out


def _text_of(path: pathlib.Path) -> list[tuple[str, str]]:
    """(label, text) for everything readable inside this file."""
    if path.stat().st_size > MAX_BYTES:
        return [(str(path), "")]

    name = path.name.lower()
    try:
        if name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return [(f"{path} (decompressed)", fh.read())]

        if name.endswith((".xlsx", ".zip")):
            # A workbook is a zip of XML. The cell values live in
            # sharedStrings.xml and the sheet parts, so reading every XML
            # member covers all of them without parsing the format.
            out = []
            with zipfile.ZipFile(path) as zf:
                for member in zf.namelist():
                    if not member.endswith((".xml", ".rels")):
                        continue
                    with zf.open(member) as fh:
                        out.append((f"{path}!{member}",
                                    fh.read().decode("utf-8", "replace")))
            return out

        return [(str(path), path.read_text(encoding="utf-8", errors="replace"))]
    except (OSError, zipfile.BadZipFile, gzip.BadGzipFile, EOFError) as exc:
        print(f"::warning::could not read {path}: {exc}")
        return []


def main(argv: list[str]) -> int:
    root = pathlib.Path(argv[1] if len(argv) > 1 else "export")
    if not root.exists():
        print(f"::error::{root} does not exist; nothing to check.")
        return 1

    findings: list[str] = []
    checked = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        checked += 1
        for label, text in _text_of(path):
            findings.extend(findings_in(text, label))

    for finding in findings[:40]:
        print(f"::error title=Credential in the export::{finding}")

    if findings:
        print(f"\n{len(findings)} possible credential(s) in {checked} file(s). "
              f"Nothing was published.")
        print("Rotate the affected key at the provider, then clear the "
              "offending rows:")
        print("  UPDATE log_api_call     SET error_msg = NULL "
              "WHERE error_msg LIKE '%apikey=%' OR error_msg LIKE '%token=%';")
        print("  UPDATE log_pipeline_run SET error_msg = NULL "
              "WHERE error_msg LIKE '%://%:%@%';")
        print("pipeline/redact.py prevents new ones from being written.")
        return 1

    print(f"No credentials found in {checked} exported file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
