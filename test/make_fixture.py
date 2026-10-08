"""Make a client-style .docx to test the bridge against.

Standard manuscript format (Times New Roman 12, double-spaced, first-line
indent), a title and heading, italic notebook entries, two comments from
the client, and one tracked change of her own.  Resave the result through
LibreOffice so the bridge meets XML a word processor wrote, not ours.

Needs python-docx (pip) -- only for making this test file.
"""
import sys
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Pt, Inches

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

PARAS = [
    ("Title", [("The Inventory", "")]),
    ("Heading 1", [("Chapter Three", "")]),
    ("Normal", [("The workshop key was still on the nail by the back door, where Walt had hung it every evening for thirty-one years, and for the first week after the funeral Ruth walked past it the way you walk past a dog that might bite. She knew what was behind the door. Sawdust, mostly. The smell of linseed and burnt coffee. Forty years of a man's attention, sorted into coffee cans.", "")]),
    ("Normal", [("On the eighth day her daughter called from Spokane and asked, in the careful voice she had started using, whether Ruth had thought about the tools. There was a man from the woodworking guild who would come out and make an offer on the lot. It would be one less thing. It would be one less thing, she said again, as if Ruth had not heard it the first time.", "")]),
    ("Normal", [("Ruth said she would think about it. Then she took the key off the nail.", "")]),
    ("Normal", [("The door stuck the way it always had, low on the hinge side, and she had to put her shoulder to it. Inside, the light came in gray through the one window, which Walt had never cleaned because he said it made the light honest.", "")]),
    ("Normal", [("The workbench ran the length of the north wall. Above it, on a pegboard he had painted white and then outlined, tool by tool, in black marker, every chisel and square and handsaw hung inside its own silhouette. ", ""), ("Three of the outlines were empty.", "comment")]),
    ("Normal", "TRACKED"),
    ("Normal", [("She found the notebook in the second drawer, under a box of brass screws sorted by gauge. It was a composition book, the black-and-white marbled kind the grandchildren used for school, and on the cover he had written INVENTORY in the block capitals he used for everything that mattered and for nothing else.", "")]),
    ("Normal", [("The first page was dated March 4, 1987. ", ""), ("Stanley No. 4 smoothing plane, purchased Hardesty's Hardware, $22.50. Needs new tote.", "i"), (" The second entry, a week later, recorded that the tote had been replaced with one he had turned from a piece of cherry that had come from the Ellisons' fallen tree.", "")]),
    ("Normal", [("She sat down on the stool. It was his stool and it was too tall for her, and her feet hung. She turned the pages.", "")]),
]


def main(out):
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    pf = normal.paragraph_format
    pf.line_spacing = 2.0
    pf.first_line_indent = Inches(0.5)
    pf.space_after = Pt(0)
    for name in ("Title", "Heading 1"):
        st = doc.styles[name]
        st.font.name = "Times New Roman"
        st.paragraph_format.first_line_indent = Inches(0)

    title_para = None
    comment_run = None
    for style, runs in PARAS:
        p = doc.add_paragraph(style=style)
        if runs == "TRACKED":
            p.add_run("She stood a long time looking at those three empty shapes. It was ")
            p._p.append(parse_xml(
                f'<w:del {W} w:id="901" w:author="Dana Whitcomb" w:date="2026-09-30T10:12:00Z">'
                '<w:r><w:delText xml:space="preserve">so very much like him</w:delText></w:r></w:del>'))
            p._p.append(parse_xml(
                f'<w:ins {W} w:id="902" w:author="Dana Whitcomb" w:date="2026-09-30T10:12:00Z">'
                '<w:r><w:t>like him</w:t></w:r></w:ins>'))
            p.add_run(" to leave a puzzle and call it tidiness.")
            continue
        for text, kind in runs:
            r = p.add_run(text)
            if kind == "i":
                r.italic = True
            if kind == "comment":
                comment_run = r
        if style == "Title":
            title_para = p

    doc.add_comment(title_para.runs, text="Stephen, I'm not sure about the title. \"The Inventory\" feels flat to me.",
                    author="Dana Whitcomb", initials="DW")
    doc.add_comment([comment_run], text="Should I say which three? I had them as a coping saw, a block plane, and the marking gauge.",
                    author="Dana Whitcomb", initials="DW")
    doc.save(out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "fixture.docx")
