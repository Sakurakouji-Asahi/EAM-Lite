"""Accept valid XLSX writers that omit the optional sheet dimension hint."""
import io
import re
import zipfile

from apps.imports.services import _load_rows, build_template_workbook, get_template_definition


def test_current_asset_template_parses_without_optional_dimension_hints():
    source = build_template_workbook("asset_initialization")
    output = io.BytesIO()
    removed = 0
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(output, "w") as rewritten:
        for member in original.infolist():
            content = original.read(member.filename)
            if member.filename.startswith("xl/worksheets/") and member.filename.endswith(".xml"):
                content, count = re.subn(rb"<dimension\b[^>]*/>", b"", content, count=1)
                removed += count
            rewritten.writestr(member, content)
    assert removed == 3
    rows, errors = _load_rows(output.getvalue(), get_template_definition("asset_initialization"))
    assert rows == [] and errors == []
