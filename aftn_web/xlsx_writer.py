"""Minimal pure-Python xlsx writer (no external dependencies)."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape as xmlescape


def make_xlsx(sheets: list[dict]) -> io.BytesIO:
    """
    sheets = [
        {"name": "Sheet1", "rows": [["col1", "col2"], ["val1", "val2"]]},
        ...
    ]
    Returns BytesIO with the .xlsx content.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", _make_workbook(sheets))
        z.writestr("xl/_rels/workbook.xml.rels", _make_wb_rels(sheets))
        z.writestr("xl/styles.xml", _STYLES)
        for i, sheet in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _make_sheet(sheet["rows"]))
    buf.seek(0)
    return buf


def _make_workbook(sheets: list[dict]) -> str:
    sheets_xml = "\n".join(
        f'    <sheet name="{xmlescape(s["name"])}" sheetId="{i+1}" r:id="rId{i+1}"/>'
        for i, s in enumerate(sheets)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"\n'
        '          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        "  <sheets>\n"
        f"{sheets_xml}\n"
        "  </sheets>\n"
        "</workbook>"
    )


def _make_wb_rels(sheets: list[dict]) -> str:
    template = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    )
    for i in range(len(sheets)):
        template += (
            f'    <Relationship Id="rId{i+1}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{i+1}.xml"/>\n'
        )
    template += (
        '    <Relationship Id="rId0"'
        ' Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"'
        ' Target="styles.xml"/>\n'
        '</Relationships>'
    )
    return template


def _make_sheet(rows: list[list]) -> str:
    col_letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def col_str(i: int) -> str:
        return col_letters[i] if i < 26 else col_letters[i // 26 - 1] + col_letters[i % 26]

    row_xmls = []
    for r_idx, row in enumerate(rows):
        cells = []
        for c_idx, val in enumerate(row):
            ref = f"{col_str(c_idx)}{r_idx + 1}"
            if isinstance(val, (int, float)):
                cells.append(f'<c r="{ref}"><v>{val}</v></c>')
            elif isinstance(val, str) and val:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{xmlescape(val)}</t></is></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t/></is></c>')
        row_xmls.append(f'<row r="{r_idx+1}">{"".join(cells)}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n'
        "  <sheetData>\n"
        f'    {"".join(row_xmls)}\n'
        "  </sheetData>\n"
        "</worksheet>"
    )


_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="1"><font><sz val="11"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs>
  <cellXfs count="1"><xf numFmtId="0" fontId="0"/></cellXfs>
</styleSheet>"""
