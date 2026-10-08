"""Round-trip tests for redline.py.  Run:  python3 tests/test_bridge.py

Needs python-docx (pip) to make the test documents; the bridge itself
needs only lxml.  Each test makes a .docx, imports it, edits the org the
way cm-mode would, exports, and checks what came back.
"""
import os
import re
import subprocess
import sys
import tempfile
import zipfile

from docx import Document
from docx.oxml import parse_xml
from docx.shared import Pt
from lxml import etree

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "..", "redline.py")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
ME = 'SLW="Stephen Lloyd Webber"'


def run(*args):
    r = subprocess.run([sys.executable, BRIDGE, *args], capture_output=True, text=True)
    if r.returncode:
        raise AssertionError(r.stderr)
    return r


def body(path):
    root = etree.fromstring(zipfile.ZipFile(path).read("word/document.xml"))
    return [x for x in root.find(W + "body")]


def make_stress(path):
    doc = Document()
    p = doc.add_paragraph()
    # Word often splits one word across runs; same formatting each time.
    for piece in ["Frag", "men", "ted ", "para", "graph stays as it was."]:
        r = p.add_run(piece)
        r.font.name = "Garamond"
        r.font.size = Pt(13)
    doc.add_paragraph("")                                   # empty paragraph
    t = doc.add_table(rows=1, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text = "tool", "count"
    p = doc.add_paragraph()
    r = p.add_run("Garamond line to edit, with ")
    r.font.name = "Garamond"
    r.bold = True
    r2 = p.add_run("bold")
    r2.font.name = "Garamond"
    r2.bold = True
    p.add_run(" and a break").font.name = "Garamond"
    p.add_run().add_break()
    p.add_run("after it.").font.name = "Garamond"
    link = doc.add_paragraph("See ")
    link._p.append(parse_xml(f'<w:hyperlink {NS} w:anchor="x"><w:r><w:t>this link</w:t></w:r></w:hyperlink>'))
    doc.add_paragraph("")
    doc.add_paragraph("Last paragraph.")
    doc.save(path)


def test_stress(tmp):
    src = os.path.join(tmp, "stress.docx")
    make_stress(src)
    run("import", src, "--me", ME)
    org = open(os.path.join(tmp, "stress.org")).read()
    assert "#+docx_block:" in org, "table and link should be placeholders"
    assert "*Garamond line to edit, with bold*" in org, org
    assert "a break\\\\\nafter it." in org, "line break should survive as \\\\"
    edited = org.replace("to edit,", "to edit{++ right here++}{>>@SLW<<},")
    open(os.path.join(tmp, "stress.org"), "w").write(edited)
    out = os.path.join(tmp, "stress-edited.docx")
    run("export", os.path.join(tmp, "stress.org"), "-o", out, "--me", ME)
    a, b = body(src), body(out)
    assert len(a) == len(b), (len(a), len(b))
    same = [etree.tostring(x) == etree.tostring(y) for x, y in zip(a, b)]
    assert same.count(False) == 1, same                     # only the edited paragraph
    edited_p = b[same.index(False)]
    xml = etree.tostring(edited_p).decode()
    assert 'w:ascii="Garamond"' in xml and "<w:b/>" in xml, "formatting should carry over"
    assert "<w:ins " in xml and "right here" in xml
    assert "<w:br/>" in xml
    # And back again, unchanged.
    run("import", out, "-o", os.path.join(tmp, "again.org"), "--me", ME, "--force")
    again = open(os.path.join(tmp, "again.org")).read()
    assert same_meaning(again, edited), "re-import should match the edited org"


def same_meaning(org_a, org_b):
    """Equal text, changes, authors, and emphasis, however the markers fall.

    (Typing inside bold text gives a bold insertion, which comes back as
    *x*{++*y*++}*z* rather than *x{++y++}z*: same thing, different spelling.)
    """
    sys.path.insert(0, os.path.join(HERE, ".."))
    import redline
    def ir(org):
        blocks = redline.org_blocks(org.split("\n", 4)[4])
        vis = lambda c, f: f and not c.ch.isspace()        # bold spaces look like plain ones
        return [[(c.ch, c.chg, c.author, vis(c, c.i), vis(c, c.b)) for c in redline.parse_para(b, None).chars]
                for b in blocks]
    return ir(org_a) == ir(org_b)


def test_fixture(tmp):
    """The LibreOffice-saved client fixture, with a full round of edits."""
    fixture = os.path.join(HERE, "fixtures", "client.docx")
    if not os.path.exists(fixture):
        print("  (skipped: tests/client.docx not present)")
        return
    src = os.path.join(tmp, "client.docx")
    with open(fixture, "rb") as f, open(src, "wb") as g:
        g.write(f.read())
    run("import", src, "--me", ME)
    org_path = os.path.join(tmp, "client.org")
    org = open(org_path).read()
    assert "{~~so very much like him~>like him~~}{>>@Dana<<}" in org
    org = org.replace("feels flat to me.<<}", "feels flat to me.<<}{>>@SLW Let's revisit.<<}")
    org = org.replace("{~~so very much like him~>like him~~}{>>@Dana<<}", "like him")
    org = org.replace("written INVENTORY", "written /INVENTORY/")
    open(org_path, "w").write(org)
    out = os.path.join(tmp, "client-edited.docx")
    run("export", org_path, "-o", out, "--me", ME)
    zf = zipfile.ZipFile(out)
    ex = zf.read("word/commentsExtended.xml").decode()
    assert "paraIdParent" in ex, "the reply should be threaded"
    doc = zf.read("word/document.xml").decode()
    assert "so very much" not in doc, "accepted change should be gone"
    assert "rPrChange" in doc, "italic switch should be a tracked formatting change"
    run("import", out, "-o", os.path.join(tmp, "back.org"), "--me", ME, "--force")
    back = open(os.path.join(tmp, "back.org")).read()
    strip = lambda s: s.split("\n", 4)[4]
    assert strip(back) == strip(org)


def test_never_overwrites(tmp):
    src = os.path.join(tmp, "n.docx")
    Document().save(src)
    run("import", src)
    r = subprocess.run([sys.executable, BRIDGE, "import", src], capture_output=True, text=True)
    assert r.returncode and "exists" in r.stderr
    org = os.path.join(tmp, "n.org")
    r = subprocess.run([sys.executable, BRIDGE, "export", org, "-o", src], capture_output=True, text=True)
    assert r.returncode and "refusing" in r.stderr


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    fn(tmp)
                    print(f"ok    {name}")
                except AssertionError as e:
                    failed += 1
                    print(f"FAIL  {name}: {e}")
    sys.exit(1 if failed else 0)
