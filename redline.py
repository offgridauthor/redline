#!/usr/bin/env python3
# Copyright (C) 2026 Stephen Lloyd Webber
# SPDX-License-Identifier: GPL-3.0-or-later
# Part of redline (https://github.com/offgridauthor/redline).
"""redline -- move Word tracked changes and comments to and from org.

    redline.py import CLIENT.docx [-o CLIENT.org] [--me 'SLW=Stephen Lloyd Webber']
                                  [--alias 'Stephen Webber' ...]
    redline.py export CLIENT.org  [-o CLIENT-edited.docx] [--me ...]
    redline.py --version

The org side uses CriticMarkup ({++ ++}, {-- --}, {~~ ~> ~~}, {== ==},
{>> <<}), the same markup cm-mode edits.  A change's author rides in the
comment right after it ({++text++}{>>@SLW<<}); further comments after a
change or highlight form a thread (first one is the root, the rest are
replies).

Export never regenerates the document.  It opens the original .docx
named in `#+review_source:` and works paragraph by paragraph:

  - paragraphs whose markup is unchanged are copied byte for byte;
  - changed paragraphs are rebuilt, each character taking the run
    formatting of the character it lines up with in the original, so
    fonts, sizes, and styles come back as the client had them;
  - everything else in the package (styles, headers, numbering,
    settings) is copied untouched.

Neither command ever writes over its input.  Needs python3-lxml.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import difflib
import os
import posixpath
import random
import re
import sys
import zipfile
from dataclasses import dataclass, field
from typing import Optional

try:
    from lxml import etree
except ImportError:
    sys.exit("redline needs the lxml library for Python 3 "
             "(Debian/Ubuntu: sudo apt install python3-lxml; elsewhere: pip install lxml)")

__version__ = "0.1.0"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14_NS = "http://schemas.microsoft.com/office/word/2010/wordml"
W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"

REL_COMMENTS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
REL_COMMENTS_EX = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
CT_COMMENTS = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
CT_COMMENTS_EX = "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml"


def w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


PARA_SEP = " ¶ "          # paragraph break inside a comment
BR = "\n"                  # a w:br inside a paragraph (org: \\ at line end)
OBJ = "\ufffc"             # stands for one inline object in a paragraph
# Private markers for comments whose range spans paragraphs or other
# markup.  They exist only between reading the org and writing the XML.
SEP = "\ue020"             # breaks a comment chain; adds no text
OPEN = "\ue021"            # {>>OPEN key<<}: where a spanning comment starts
SPAN = "\ue022"            # {>>@A SPAN key SPAN text<<}: the comment it belongs to
PMARK = "\ue023"           # PMARK +|- tag PMARK: this paragraph's mark was inserted or deleted
LINK_ANY = re.compile(r"\[\[docx:([0-9]+\.[0-9]+)\](?:\[(.*?)\])?\]", re.S)
LINK = re.compile(r"\[\[docx:([0-9]+\.[0-9]+)\](?:\[(.*?)\])?\]", re.S)


class Opaque(Exception):
    """A paragraph contains something org can't express (field, image, link...)."""


# ---------------------------------------------------------------------------
# Intermediate representation: one paragraph = characters + comments
# ---------------------------------------------------------------------------

@dataclass
class Ch:
    ch: str
    chg: Optional[str] = None      # None, "ins", or "del"
    author: Optional[str] = None   # author tag (short), for changes
    i: bool = False
    b: bool = False
    rpr: object = None             # base side only: the run's w:rPr element
    date: Optional[str] = None     # base side only
    obj: Optional[str] = None      # an inline object kept verbatim (link, field, image...)
    desc: str = ""                 # what the object shows as text
    nodes: list = None             # base side only: the object's XML
    aname: Optional[str] = None    # base side only: the change's author as Word wrote it


@dataclass
class Comment:
    author: Optional[str]
    text: str
    start: int = 0                 # range [start, end) in the paragraph's chars
    end: int = 0
    point: bool = False            # anchored at `start` with no range
    replies: list = field(default_factory=list)
    base_id: Optional[str] = None  # import side: id in the original comments.xml
    opener: Optional[str] = None   # export side: marks where comment `span` starts
    span: Optional[str] = None     # export side: key of this comment's opener


@dataclass
class Para:
    chars: list
    comments: list                 # root comments (replies hang off them)
    level: int = 0                 # heading level, 0 for body text
    opaque: Optional[int] = None   # body index of an element kept verbatim
    preview: str = ""
    mark: Optional[tuple] = None   # export side: (ins|del, tag) for a tracked paragraph mark
    anchors: list = field(default_factory=list)  # base side: (pos, bookmark element)


# ---------------------------------------------------------------------------
# Reading a .docx
# ---------------------------------------------------------------------------

class Docx:
    def __init__(self, path: str):
        self.path = path
        try:
            self.zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            sys.exit(f"{os.path.basename(path)} isn't a Word .docx file inside, whatever its name "
                     f"(it may be an older .doc, or plain text). Open it in Word or LibreOffice "
                     f"and save it as .docx, then try again.")
        if "word/document.xml" not in self.zf.namelist() and "_rels/.rels" not in self.zf.namelist():
            sys.exit(f"{os.path.basename(path)} is a zip file but not a Word document.")
        self.names = self.zf.namelist()
        # The main part is usually word/document.xml, but the package
        # relationships say for sure.
        self.main = "word/document.xml"
        if "_rels/.rels" in self.names:
            for r in etree.fromstring(self.zf.read("_rels/.rels")):
                if r.get("Type", "").endswith("/officeDocument"):
                    self.main = r.get("Target").lstrip("/")
        folder, base = posixpath.split(self.main)
        self.main_rels = posixpath.join(folder, "_rels", base + ".rels")
        self.rels = (etree.fromstring(self.zf.read(self.main_rels)) if self.main_rels in self.names
                     else etree.Element(f"{{{PKG_REL_NS}}}Relationships", nsmap={None: PKG_REL_NS}))
        def part(rel_type, default):
            for r in self.rels:
                if r.get("Type") == rel_type:
                    return posixpath.normpath(posixpath.join(folder, r.get("Target")))
            return posixpath.join(folder, default)
        self.comments_part = part(REL_COMMENTS, "comments.xml")
        self.comments_ex_part = part(REL_COMMENTS_EX, "commentsExtended.xml")
        self.doc = etree.fromstring(self.zf.read(self.main))
        self.body = self.doc.find(w("body"))
        self.comments = {}         # id -> dict(author, initials, date, text, el, para_ids)
        self.parent = {}           # comment id -> parent comment id
        self.done = set()          # ids of comments marked resolved
        self.objects = {}          # inline object id -> list of XML nodes
        if self.comments_part in self.names:
            croot = etree.fromstring(self.zf.read(self.comments_part))
            for c in croot.findall(w("comment")):
                paras = c.findall(w("p"))
                text = PARA_SEP.join(
                    "".join(t.text or "" for t in p.iter(w("t"))) for p in paras).strip()
                self.comments[c.get(w("id"))] = dict(
                    author=c.get(w("author")) or "", initials=c.get(w("initials")),
                    date=c.get(w("date")), text=text, el=c,
                    para_ids=[p.get(f"{{{W14_NS}}}paraId") for p in paras])
        if self.comments_ex_part in self.names:
            ex = etree.fromstring(self.zf.read(self.comments_ex_part))
            by_para = {pid: cid for cid, c in self.comments.items() for pid in c["para_ids"] if pid}
            for e in ex.iter(f"{{{W15_NS}}}commentEx"):
                pid, parent = e.get(f"{{{W15_NS}}}paraId"), e.get(f"{{{W15_NS}}}paraIdParent")
                if pid in by_para and parent in by_para:
                    self.parent[by_para[pid]] = by_para[parent]
                if pid in by_para and e.get(f"{{{W15_NS}}}done") in ("1", "true"):
                    self.done.add(by_para[pid])


def style_level(p) -> int:
    ps = p.find(f"{w('pPr')}/{w('pStyle')}")
    sid = (ps.get(w("val")) if ps is not None else "") or ""
    if sid.lower() == "title":
        return 1
    m = re.fullmatch(r"(?i)heading\s*([1-9])", sid)
    return int(m.group(1)) if m else 0


def on(el, tag) -> bool:
    """Is a toggle property like w:i present and not switched off?"""
    x = el.find(w(tag)) if el is not None else None
    return x is not None and x.get(w("val"), "true") not in ("0", "false", "off")


SKIP_IN_PARA = {"pPr", "proofErr", "permStart", "permEnd", "commentReference"}
# Inline things org can't edit: each becomes one object, shown in org as
# [[docx:ID][text]] and copied back out verbatim.
OBJECT_IN_PARA = {"hyperlink": "link", "fldSimple": "field", "sdt": "control",
                  "smartTag": "tag", "customXml": "xml", "oMath": "math",
                  "oMathPara": "math", "subDoc": "subdoc"}
OBJECT_IN_RUN = {"drawing": "image", "pict": "image", "object": "object", "sym": "symbol",
                 "footnoteReference": "footnote", "endnoteReference": "endnote",
                 "ruby": "ruby", "ptab": "tab"}


def node_text(nodes) -> str:
    return "".join(t.text or "" for n in nodes for t in n.iter(w("t"), w("delText")))


def field_result(nodes) -> str:
    """The text a complex field shows (after its separate mark)."""
    out, depth, showing = [], 0, False
    for n in nodes:
        for k in n.iter(w("fldChar"), w("t"), w("delText")):
            name = etree.QName(k).localname
            if name == "fldChar":
                kind = k.get(w("fldCharType"))
                if kind == "begin":
                    depth += 1
                elif kind == "separate" and depth == 1:
                    showing = True
                elif kind == "end":
                    depth -= 1
                    if depth == 0:
                        showing = False
            elif showing:
                out.append(k.text or "")
    return "".join(out)


def walk_para(p, tags: dict, pid=None):
    """Flatten a w:p into chars plus comment marks and bookmarks.

    Returns (chars, marks, anchors).  marks is a list of (pos, kind,
    comment_id) with kind 'start' or 'end'; anchors is a list of (pos,
    element) for bookmarks, which have no text but must survive a rebuild.
    Inline objects become one OBJ char each.  Raises Opaque for content
    that can't be cut up this way (a field running past the paragraph).
    """
    chars, marks, anchors = [], [], []
    count = [0]
    aname = [None]                 # author name of the change being read

    def obj(nodes, kind, chg, author, date, desc=None):
        oid = f"{pid}.{count[0]}" if pid is not None else f"x.{count[0]}"
        count[0] += 1
        text = desc if desc is not None else node_text(nodes)
        chars.append(Ch(OBJ, chg, author, obj=oid, desc=text.strip() or kind,
                        nodes=nodes, date=date))

    def run(r, chg, author, date):
        rpr = r.find(w("rPr"))
        it, bd = on(rpr, "i"), on(rpr, "b")
        for k in r:
            if not isinstance(k.tag, str):
                continue
            name = etree.QName(k).localname
            if name in ("t", "delText"):
                text = k.text or ""
                if k.get(f"{{{XML_NS}}}space") != "preserve":
                    text = text.strip(" \t\r\n")    # Word drops edge spaces without preserve
                for c in text:
                    chars.append(Ch(c, chg, author, it, bd, rpr, date))
            elif name == "tab":
                chars.append(Ch("\t", chg, author, it, bd, rpr, date))
            elif name in ("br", "cr"):
                if k.get(w("type")) in ("page", "column"):
                    obj([single_run(r, k)], "page break", chg, author, date, "page break")
                else:
                    chars.append(Ch(BR, chg, author, it, bd, rpr, date))
            elif name == "noBreakHyphen":
                chars.append(Ch("\u2011", chg, author, it, bd, rpr, date))
            elif name == "softHyphen":
                chars.append(Ch("\u00ad", chg, author, it, bd, rpr, date))
            elif name == "commentReference":
                marks.append((len(chars), "ref", k.get(w("id"))))
            elif name in OBJECT_IN_RUN:
                desc = {"footnote": "note", "endnote": "note"}.get(OBJECT_IN_RUN[name])
                obj([single_run(r, k)], OBJECT_IN_RUN[name], chg, author, date, desc)
            # rPr, lastRenderedPageBreak, annotationRef: nothing to carry

    def children(el, chg=None, author=None, date=None):
        field_nodes, depth = None, 0
        for k in el:
            if not isinstance(k.tag, str):
                continue
            name = etree.QName(k).localname
            # A complex field runs from a begin mark to its end mark across
            # sibling runs; gather it whole.
            if field_nodes is not None:
                field_nodes.append(k)
                for f in k.iter(w("fldChar")):
                    t = f.get(w("fldCharType"))
                    depth += 1 if t == "begin" else -1 if t == "end" else 0
                if depth == 0:
                    obj(field_nodes, "field", chg, author, date, field_result(field_nodes))
                    field_nodes = None
                continue
            if name == "r" and k.find(w("fldChar")) is not None:
                begins = [f for f in k.iter(w("fldChar")) if f.get(w("fldCharType")) == "begin"]
                if begins:
                    field_nodes, depth = [k], 0
                    for f in k.iter(w("fldChar")):
                        t = f.get(w("fldCharType"))
                        depth += 1 if t == "begin" else -1 if t == "end" else 0
                    if depth == 0:
                        obj(field_nodes, "field", chg, author, date, field_result(field_nodes))
                        field_nodes = None
                    continue
                raise Opaque("field")
            if name == "r":
                run(k, chg, author, date)
            elif name in ("ins", "moveTo", "del", "moveFrom"):
                kind = "ins" if name in ("ins", "moveTo") else "del"
                outer, aname[0] = aname[0], k.get(w("author")) or ""
                first = len(chars)
                children(k, kind, tags.get(aname[0]), k.get(w("date")))
                for c in chars[first:]:
                    c.aname = c.aname or aname[0]
                aname[0] = outer
            elif name == "commentRangeStart":
                marks.append((len(chars), "start", k.get(w("id"))))
            elif name == "commentRangeEnd":
                marks.append((len(chars), "end", k.get(w("id"))))
            elif name in ("bookmarkStart", "bookmarkEnd"):
                anchors.append((len(chars), k))
            elif name in SKIP_IN_PARA or name.endswith("RangeStart") or name.endswith("RangeEnd"):
                continue
            elif name in OBJECT_IN_PARA:
                inner = inner_change(k)            # a link deleted (or added) as a whole
                if chg is None and inner:
                    obj([k], OBJECT_IN_PARA[name], inner[0], tags.get(inner[1] or ""), inner[2])
                else:
                    obj([k], OBJECT_IN_PARA[name], chg, author, date)
            else:
                raise Opaque(name)
        if field_nodes is not None:
            raise Opaque("field")

    children(p)
    return chars, marks, anchors


def inner_change(el):
    """(kind, author, date) if every run inside EL is in one kind of change."""
    runs = [r for r in el.iter(w("r"))]
    if not runs:
        return None
    found = set()
    for r in runs:
        a = r.getparent()
        while a is not None and a is not el and etree.QName(a).localname not in ("ins", "del"):
            a = a.getparent()
        if a is None or a is el:
            return None
        found.add((etree.QName(a).localname, a.get(w("author")), a.get(w("date"))))
    kinds = {f[0] for f in found}
    return sorted(found)[0] if len(kinds) == 1 else None


def single_run(r, child):
    """A copy of run R holding only its properties and CHILD."""
    new = etree.Element(r.tag, attrib=dict(r.attrib), nsmap=None)
    rpr = r.find(w("rPr"))
    if rpr is not None:
        new.append(copy.deepcopy(rpr))
    new.append(copy.deepcopy(child))
    return new


def make_tag(name: str, taken: dict) -> str:
    """A short, space-free author tag: first name, else initials."""
    words = re.findall(r"[A-Za-z0-9]+", name) or ["anon"]
    tag = words[0]
    if tag in taken.values():
        tag = "".join(x[0] for x in words).upper()
    n = 2
    base = tag
    while tag in taken.values():
        tag, n = f"{base}{n}", n + 1
    return tag


def read_paras(d: Docx, tags: dict):
    """Turn every body element into a Para (or an opaque placeholder)."""
    # Where does each comment start and end?  (paragraph index, position)
    raw = []
    d_anchors = {}
    for idx, el in enumerate(d.body):
        if not isinstance(el.tag, str):
            continue
        name = etree.QName(el).localname
        if name == "sectPr":
            continue
        if name in ("bookmarkStart", "bookmarkEnd") or name.endswith(("RangeStart", "RangeEnd")):
            raw.append((idx, el, [], [], None))     # zero-width; stays in place like an empty paragraph
            continue
        if name != "p":
            raw.append((idx, el, None, None, f"{name}"))
            continue
        try:
            chars, marks, anchors = walk_para(el, tags, idx)
            for c in chars:
                if c.obj:
                    d.objects[c.obj] = c.nodes
            raw.append((idx, el, chars, marks, None))
            d_anchors[idx] = anchors
        except Opaque as e:
            text = "".join(t.text or "" for t in el.iter(w("t")))
            raw.append((idx, el, None, None, f"{e}: {text[:50]}"))
    starts, ends, refs = {}, {}, {}
    for n, (_, _, chars, marks, _) in enumerate(raw):
        for pos, kind, cid in marks or []:
            {"start": starts, "end": ends, "ref": refs}[kind].setdefault(cid, (n, pos))

    paras = []
    roots_by_id = {}
    for n, (idx, el, chars, marks, why) in enumerate(raw):
        if chars is None:
            paras.append(Para([], [], opaque=idx, preview=why))
            continue
        para = Para(chars, [], level=style_level(el), anchors=d_anchors.get(idx, []))
        for cid, info in d.comments.items():
            if cid in d.parent:
                continue                      # replies handled below
            s, e = starts.get(cid), ends.get(cid)
            if s is None and e is None:
                s = e = refs.get(cid)              # a comment with only its reference mark
                if s is None:
                    continue
            anchor = e or s
            if anchor[0] != n:
                continue
            c = Comment(tags.get(info["author"] or ""), shown(d, cid), base_id=cid)
            if s and e and s[0] == e[0] and s[1] < e[1]:
                c.start, c.end = s[1], e[1]
            else:                             # empty, or spans paragraphs
                c.start = c.end = anchor[1]
                c.point = True
            para.comments.append(c)
            roots_by_id[cid] = c
        para.comments.sort(key=lambda c: (c.end, c.start))
        paras.append(para)

    # Replies, oldest first, onto their thread's root.
    def root_of(cid):
        seen = set()
        while cid in d.parent and cid not in seen:
            seen.add(cid)
            cid = d.parent[cid]
        return cid
    for cid in sorted(d.parent, key=lambda c: (d.comments[c]["date"] or "", int(c) if c.isdigit() else 0)):
        root = roots_by_id.get(root_of(cid))
        if root:
            info = d.comments[cid]
            root.replies.append(Comment(tags.get(info["author"] or ""), shown(d, cid), base_id=cid))
    return paras


DONE = "\u2713 "             # a resolved comment, as shown in org: "✓ text"


def shown(d: Docx, cid) -> str:
    text = d.comments[cid]["text"]
    return DONE + text if cid in d.done else text


# ---------------------------------------------------------------------------
# Rendering a Para as org + CriticMarkup
# ---------------------------------------------------------------------------

ZW = "\u200b"               # zero-width space: org's way to fence off a marker
SP = set(" \t\n" + ZW)
PRE = set(" \t\n([{'\"-\u2014\u2018\u201c" + ZW)
POST = set(" \t\n.,:;!?)]}'\"-\u2014\u2019\u201d\u2026" + ZW)


def emphasis_plan(chars, plain=False):
    """Where org's /italic/ and *bold* markers go, for a whole paragraph.

    Returns (before, after): strings to emit before / after character k.
    Whitespace at the edge of a styled stretch stays outside the markers,
    and a zero-width space fences off any marker org wouldn't otherwise
    see (italics ending mid-word, a literal slash in a URL, and so on).
    """
    n = len(chars)
    before, after = [""] * n, [""] * n
    style = [None if c.ch.isspace() else ((False, False) if c.obj else (c.i, c.b)) for c in chars]
    spans, cur = [], None
    for k in range(n):
        if style[k] is None:
            continue
        if cur and cur[2] == style[k]:
            cur[1] = k
        else:
            if cur:
                spans.append(cur)
            cur = [k, k, style[k]]
    if cur:
        spans.append(cur)
    state = [(False, False)] * n
    for a, b, (it, bd) in spans:
        if plain:
            break
        for k in range(a, b + 1):
            state[k] = (it, bd)
        if not (it or bd):
            continue
        before[a] += ("*" if bd else "") + ("/" if it else "")
        after[b] = ("/" if it else "") + ("*" if bd else "") + after[b]
        if a > 0 and chars[a - 1].ch not in PRE:
            before[a] = ZW + before[a]
        if b + 1 < n and chars[b + 1].ch not in POST:
            after[b] += ZW
    # Literal slashes and asterisks that a reader of org would take for markers.
    for k, c in enumerate(chars):
        if c.obj or c.ch not in "/*":
            continue
        if plain:
            after[k] += ZW                         # never a marker
            continue
        on_ = state[k][0] if c.ch == "/" else state[k][1]
        prev = chars[k - 1].ch if k else " "
        nxt = chars[k + 1].ch if k + 1 < n else " "
        later = has_closer(chars, k)
        if not on_ and (prev in PRE) and nxt not in SP and later and not before[k]:
            after[k] += ZW
        if on_ and prev not in SP and nxt in POST and not after[k]:
            before[k] = ZW + before[k]
    return before, after


def emph(chars, plan=None, offset=0) -> str:
    """CHARS as org text, with markers from PLAN (indexes start at OFFSET)."""
    if plan is None:
        plan, offset = emphasis_plan(chars), 0
    before, after = plan
    out = []
    for k, c in enumerate(chars):
        g = offset + k
        text = link(c) if c.obj else ("\\\\\n" if c.ch == BR else c.ch)
        out.append(before[g] + text + after[g])
    return "".join(out)


def link(c: Ch) -> str:
    desc = re.sub(r"\s+", " ", c.desc).replace("[", "(").replace("]", ")").strip()
    return f"[[docx:{c.obj}][{desc}]]" if desc else f"[[docx:{c.obj}]]"


def cmt(author, text) -> str:
    who = f"@{author}" if author else ""
    sep = " " if who and text else ""
    return "{>>" + who + sep + text + "<<}"


def thread(c: Comment) -> str:
    return cmt(c.author, c.text) + "".join(cmt(r.author, r.text) for r in c.replies)


def render(para: Para) -> str:
    """PARA as org text.  Checked by reading it back: if org can't spell
    this paragraph's italics and bold, it goes out without them (the export
    keeps the original formatting wherever the org text is unchanged)."""
    # A line break that ends the paragraph shows as a bare \\ (org's own
    # line-break mark), since the org file can't keep a trailing newline.
    text = render_with(para, False).rstrip("\n")
    if para.opaque is None and para.chars:
        back = parse_para(text, None).chars
        same = [c.ch for c in back] == [c.ch for c in para.chars] and \
            [(c.i and not c.ch.isspace(), c.b and not c.ch.isspace()) for c in back] == \
            [(c.i and not c.ch.isspace() and not c.obj, c.b and not c.ch.isspace() and not c.obj)
             for c in para.chars]
        if not same:
            text = render_with(para, True).rstrip("\n")
    return text


def render_with(para: Para, plain: bool) -> str:
    if para.opaque is not None:
        return f"#+docx_block: {para.opaque} {para.preview}".rstrip()
    if not para.chars and not para.comments:
        return ""                                  # empty paragraphs stay in the docx only
    chars = para.chars
    n = len(chars)
    key = lambda k: (chars[k].chg, chars[k].author)

    # Which root comments can ride on a segment, and which become points?
    attached, points = {}, {}
    bounds = {0, n} | {k for k in range(1, n) if key(k) != key(k - 1)}
    ranged = []
    for c in para.comments:
        if not c.point and c.end > c.start and \
                all(key(k) == key(c.start) for k in range(c.start, c.end)):
            ranged.append(c)
            if chars[c.start].chg is None:
                bounds |= {c.start, c.end}
        else:
            points.setdefault(c.end, []).append(c)
    bounds |= set(points)
    bl = sorted(bounds)
    segs = [(bl[x], bl[x + 1]) for x in range(len(bl) - 1)]
    seg_set = set(segs)
    for c in ranged:
        rng = (c.start, c.end)
        if rng in seg_set:
            attached.setdefault(rng, []).append(c)
        else:
            points.setdefault(c.end, []).append(c)

    plan = emphasis_plan(chars, plain)
    # A deletion right before an insertion by the same author reads as a
    # substitution, unless comments hang on one half only.
    out, x = [], 0
    while x < len(segs):
        s, e = segs[x]
        for c in points.pop(s, []):
            out.append(thread(c))
        chg, author = key(s)
        roots = attached.get((s, e), [])
        if chg == "del" and x + 1 < len(segs):
            s2, e2 = segs[x + 1]
            if key(s2) == ("ins", author) and not roots and not attached.get((s2, e2)) \
                    and not points.get(s2):
                # Comments on the whole substitution ride along with it.
                sub_roots = [c for c in points.get(e2, []) if c.start == s and not c.point]
                if sub_roots:
                    points[e2] = [c for c in points[e2] if c not in sub_roots]
                out.append("{~~" + emph(chars[s:e], plan, s) + "~>" + emph(chars[s2:e2], plan, s2) + "~~}"
                           + change_chain(author, sub_roots))
                x += 2
                continue
        body = emph(chars[s:e], plan, s)
        if chg == "ins":
            out.append("{++" + body + "++}" + change_chain(author, roots))
        elif chg == "del":
            out.append("{--" + body + "--}" + change_chain(author, roots))
        elif roots:
            out.append("{==" + body + "==}" + "".join(thread(c) for c in roots))
        else:
            out.append(body)
        x += 1
    for pos in sorted(points):
        for c in points[pos]:
            out.append(thread(c))
    text = "".join(out)
    if para.level:
        return "*" * para.level + " " + text
    if re.match(r"\*+ |#\+", text):
        text = ZW + text                           # a scene break "* * *" isn't a heading
    return text


def change_chain(author, roots) -> str:
    if roots and roots[0].author == author and not roots[0].replies:
        first, rest = roots[0], roots[1:]
        return cmt(author, first.text) + "".join(thread(c) for c in rest)
    return cmt(author, "") + "".join(thread(c) for c in roots)


# ---------------------------------------------------------------------------
# Parsing org + CriticMarkup back into Paras
# ---------------------------------------------------------------------------

TOKEN = re.compile(
    r"\{\+\+(?P<add>.*?)\+\+\}|\{--(?P<del>.*?)--\}|\{~~(?P<old>.*?)~>(?P<new>.*?)~~\}"
    r"|\{==(?P<hl>.*?)==\}|\{>>(?P<cm>.*?)<<\}", re.S)


def strip_markup(s: str) -> str:
    """Text as it stood before any CriticMarkup inside it."""
    return TOKEN.sub(lambda m: m.group("del") or m.group("old") or m.group("hl") or "", s)


def split_comment(body: str):
    m = re.match(r"@(\S+)\s*(.*)\Z", body.strip(), re.S)
    return (m.group(1), m.group(2).strip()) if m else (None, body.strip())


def flat(text: str) -> str:
    return text.replace("\\\\\n", "\x0b").replace("\n", " ").replace("\x0b", BR)


def parse_para(text: str, me: Optional[str]) -> Para:
    level, mark = 0, None
    pm = re.search(PMARK + r"([+-])([^" + PMARK + r"]*)" + PMARK + r"\s*\Z", text)
    if pm:
        mark = ("ins" if pm.group(1) == "+" else "del", pm.group(2) or me)
        text = text[:pm.start()]
    if text.endswith("\\\\"):
        text += "\n"                              # a line break that ends the paragraph
    m = re.match(r"(\*+) ", text)
    if m:
        level, text = len(m.group(1)), text[m.end():]
    chars, comments = [], []
    # Links to kept objects are atomic: shield them before the markup
    # tokenizer can split one (an edit typed inside a link's text).
    links = []
    def shield(m):
        if "{" in (m.group(2) or ""):
            print(f"note: the text of a kept link or object can't be edited "
                  f"({m.group(1)}); it goes back as it was", file=sys.stderr)
        links.append(m)
        return f"\ue000{len(links) - 1}\ue001"
    text = LINK_ANY.sub(shield, text)
    toks, pos = [], 0
    for mt in TOKEN.finditer(text):
        if mt.start() > pos:
            toks.append(("text", text[pos:mt.start()]))
        kind = next(k for k in ("add", "del", "old", "hl", "cm") if mt.group(k) is not None)
        toks.append((kind, mt))
        pos = mt.end()
    if pos < len(text):
        toks.append(("text", text[pos:]))

    def add(s, chg=None, author=None):
        start, pos = len(chars), 0
        for m in re.finditer("\ue000([0-9]+)\ue001", s):
            for c in flat(s[pos:m.start()]).replace(SEP, ""):
                chars.append(Ch(c, chg, author))
            lk = links[int(m.group(1))]
            chars.append(Ch(OBJ, chg, author, obj=lk.group(1), desc=strip_markup(lk.group(2) or "")))
            pos = m.end()
        for c in flat(s[pos:]).replace(SEP, ""):
            chars.append(Ch(c, chg, author))
        return start, len(chars)

    def take_span(c):
        m = re.match(SPAN + r"(\w+)" + SPAN + r"\s*", c.text)
        if m:
            c.span, c.text = m.group(1), c.text[m.end():]

    k = 0
    while k < len(toks):
        kind, val = toks[k]
        k += 1
        if kind == "text":
            add(val)
            continue
        # Gather the comment chain that follows this token.
        chain = []
        while k < len(toks) and toks[k][0] == "cm":
            chain.append(split_comment(toks[k][1].group("cm")))
            k += 1
        if kind == "cm":
            chain.insert(0, split_comment(val.group("cm")))
            c0 = Comment(chain[0][0], chain[0][1], len(chars), len(chars), True)
            c0.replies = [Comment(a, t) for a, t in chain[1:]]
            if c0.text.startswith(OPEN):
                c0.opener, c0.replies = c0.text[1:], []
            take_span(c0)
            comments.append(c0)
            continue
        if kind == "hl":
            s, e = add(val.group("hl"))
            said = chain
        else:
            author = (chain[0][0] if chain else None) or me
            said = chain[1:] if chain and not chain[0][1] else chain
            if kind == "add":
                s, e = add(val.group("add"), "ins", author)
            elif kind == "del":
                s, e = add(val.group("del"), "del", author)
            else:
                s, _ = add(val.group("old"), "del", author)
                _, e = add(val.group("new"), "ins", author)
        if said:
            root = Comment(said[0][0], said[0][1], s, e, s == e)
            root.replies = [Comment(a, t) for a, t in said[1:]]
            take_span(root)
            comments.append(root)

    # Emphasis: toggle on / and * at org-style boundaries, then drop the
    # markers and any zero-width spaces fencing them.
    keep, state = [], {"/": False, "*": False}
    flags, consumed = [], set()
    for idx, c in enumerate(chars):
        if c.ch in state and not c.obj:
            prev = chars[idx - 1].ch if idx else " "
            nxt = chars[idx + 1].ch if idx + 1 < len(chars) else " "
            prev_ok = prev in PRE or (idx - 1) in consumed
            nxt_ok = nxt in POST or (nxt in state and state[nxt] and nxt != c.ch)
            if not state[c.ch] and prev_ok and nxt not in SP and has_closer(chars, idx):
                state[c.ch] = True
                consumed.add(idx)
                continue
            if state[c.ch] and prev not in SP and nxt_ok:
                state[c.ch] = False
                consumed.add(idx)
                continue
        if c.ch == ZW and (idx == 0 or (idx and chars[idx - 1].ch in "/*") or
                           (idx + 1 < len(chars) and chars[idx + 1].ch in "/*")):
            continue
        c.i, c.b = (False, False) if c.obj else (state["/"], state["*"])
        keep.append(idx)
        flags.append(c)
    remap = {old: new for new, old in enumerate(keep)}

    def fix(p):
        while p not in remap and p < len(chars):
            p += 1
        return remap.get(p, len(keep))
    for c in comments:
        c.start, c.end = fix(c.start), fix(c.end)
    return Para(flags, comments, level=level, mark=mark)


def has_closer(chars, idx) -> bool:
    """Is there a matching closing marker later, the way org would see one?"""
    m, n = chars[idx].ch, len(chars)
    for j in range(idx + 2, n):
        if chars[j].ch == m and not chars[j].obj and chars[j - 1].ch not in SP:
            nxt = chars[j + 1].ch if j + 1 < n else " "
            if nxt in POST or nxt in "/*":
                return True
    return False


GAP = re.compile(r"(?:\s|(?m:^\*+[ \t]))*")
MARKUP_OPEN = re.compile(r"\{(?:\+\+|--|~~|==|>>)")


def chain_after(body: str, pos: int) -> int:
    """End of the comment chain that starts at POS (POS if there's none)."""
    while body.startswith("{>>", pos):
        e = body.find("<<}", pos)
        if e < 0:
            break
        pos = e + 3
    return pos


def fragment_highlights(body: str) -> str:
    """Rewrite highlights org can't carry as one span, the way redline does.

    {==A {++x++} B==}{>>c<<} and highlights across paragraphs become runs
    of plain highlights ({==A ==}{++x++}{== B==}{>>c<<}); the comment
    belongs to the whole run.  One with no comment loses its braces.
    """
    out, last = [], 0
    for m in TOKEN.finditer(body):
        inner = m.group("hl")
        if inner is None or not (MARKUP_OPEN.search(inner) or re.search(r"\n[ \t]*\n", inner)):
            continue
        out.append(body[last:m.start()])
        last = m.end()
        if not body.startswith("{>>", m.end()):
            out.append(inner)
            continue
        pieces, pos = [], 0
        for t in TOKEN.finditer(inner):
            pieces += [("text", inner[pos:t.start()]), ("tok", t.group(0))]
            pos = t.end()
        pieces.append(("text", inner[pos:]))
        frag, ends_plain = [], False
        for kind, txt in pieces:
            if kind == "tok":
                frag.append(txt)
                ends_plain = False
                continue
            for i, part in enumerate(re.split(r"(\n[ \t]*\n+(?:\*+[ \t]+)?)", txt)):
                if i % 2 or not part.strip():
                    frag.append(part)
                    ends_plain = ends_plain and not part
                    continue
                lead = len(part) - len(part.lstrip())
                tail = len(part.rstrip())
                frag.append(part[:lead] + "{==" + part[lead:tail] + "==}" + part[tail:])
                ends_plain = not part[tail:]
        if not ends_plain:
            frag.append("{====}")                  # something for the comment to hang on
        out.append("".join(frag))
    out.append(body[last:])
    return "".join(out)


def join_highlights(body: str) -> str:
    """Mark runs of highlights that share one comment.

    A highlight with no comment of its own belongs to the next commented
    highlight, when only markup, blank lines, or heading stars come
    between.  The run's first piece gets an opener; the comment gets
    its key, and the XML writer stretches the comment's range back to
    the opener.
    """
    body = fragment_highlights(body)
    toks = list(TOKEN.finditer(body))
    edits, run, cursor, n = [], [], 0, 0

    def gap_ok(a, b):
        return GAP.fullmatch(TOKEN.sub("", body[a:b])) is not None

    for t in toks:
        if t.group("hl") is None:
            continue
        if run and not gap_ok(cursor, t.start()):
            run = []
        has_chain = body.startswith("{>>", t.end())
        if not has_chain:
            run.append(t)
            cursor = t.end()
            continue
        if run:
            n += 1
            key = f"s{n}"
            first = run[0]
            edits.append((first.start(), first.end(),
                           SEP + "{>>" + OPEN + key + "<<}" + SEP + first.group("hl")))
            for r in run[1:]:
                edits.append((r.start(), r.end(), r.group("hl")))
            c = t.end() + 3                        # inside the first {>>
            am = re.match(r"@\S+[ \t]*", body[c:])
            c += am.end() if am else 0
            edits.append((c, c, SPAN + key + SPAN + " "))
            if t.group("hl") == "":                # the {====} stand-in: a point
                edits.append((t.start(), t.end(), SEP))
        run, cursor = [], chain_after(body, t.end())
    for a, b, txt in sorted(edits, reverse=True):
        body = body[:a] + txt + body[b:]
    return body


def absorb_breaks(body: str) -> str:
    """A backspace that joins paragraphs deletes one of two newlines.

    One.\\n{--\\n--}Two. reads as one paragraph in org, though the original
    had two.  Fold the newline left outside into the deletion, so the
    join is tracked: One.{--\\n\\n--}Two.
    """
    out, last = [], 0
    for m in TOKEN.finditer(body):
        d = m.group("del")
        if d is None or "\n" not in d or d.strip():
            continue
        end = chain_after(body, m.end())
        before = re.search(r"[ \t]*\n?[ \t]*\Z", body[last:m.start()])
        after = re.match(r"[ \t]*\n?[ \t]*", body[end:])
        outside = before.group(0) + after.group(0)
        if (d + outside).count("\n") < 2 or outside.count("\n") >= 2:
            continue
        out.append(body[last:m.start() - len(before.group(0))])
        out.append("{--" + before.group(0) + d + after.group(0) + "--}" + body[m.end():end])
        last = end + after.end()
    out.append(body[last:])
    return "".join(out)


def org_blocks(body: str):
    """Split org body into paragraph strings on blank lines outside markup."""
    body = join_highlights(body)

    def split_sub(m):
        if m.group("old") is None or not re.search(r"\n[ \t]*\n", m.group(0)):
            return m.group(0)
        tag = re.match(r"\{>>@([^\s<]+)<<\}", body[m.end():m.end() + 80])
        glue = "{>>@" + tag.group(1) + "<<}" if tag else ""
        return "{--" + m.group("old") + "--}" + glue + "{++" + m.group("new") + "++}"
    body = TOKEN.sub(split_sub, body)
    body = absorb_breaks(body)
    spans = [(m.start(), m.end(), m) for m in TOKEN.finditer(body)]
    # Comments containing blank lines keep their paragraphs as ¶.
    out, last = [], 0
    for s, e, m in spans:
        seg = m.group(0)
        if re.search(r"\n[ \t]*\n", seg):
            if m.group("cm") is not None:
                seg = re.sub(r"\s*\n[ \t]*\n\s*", PARA_SEP, seg)
            elif m.group("add") is not None or m.group("del") is not None:
                # A tracked paragraph split or join: each paragraph but
                # the last ends with its own paragraph mark inserted or
                # deleted, the way Word records it.
                op = "{++" if m.group("add") is not None else "{--"
                cl = "++}" if op == "{++" else "--}"
                parts = re.split(r"\n[ \t]*\n+", seg[3:-3])
                tag = re.match(r"\{>>@([^\s<]+)", body[e:e + 80])
                glue = ("{>>@" + tag.group(1) + "<<}") if tag else ""
                pmark = PMARK + op[1] + (tag.group(1) if tag else "") + PMARK
                last_i = len(parts) - 1
                seg = ("\n\n").join(
                    (op + p + cl if p or i == last_i else "") +
                    (glue + pmark if i < last_i and p else pmark if i < last_i else "")
                    for i, p in enumerate(parts))
        out.append(body[last:s])
        out.append(seg)
        last = e
    out.append(body[last:])
    body = "".join(out)
    return [b.strip("\n") for b in re.split(r"\n[ \t]*\n+", body) if b.strip()]


def read_org(path: str):
    meta, body_lines = {}, []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"#\+(\w+):\s*(.*)$", line.rstrip("\n"))
            if m and m.group(1).lower() != "docx_block":
                meta[m.group(1).lower()] = m.group(2)
                body_lines.append("\n")
            else:
                body_lines.append(line)
    return meta, org_blocks("".join(body_lines))


def parse_authors(s: str) -> dict:
    return {m.group(1): m.group(2) for m in re.finditer(r'(\S+?)="([^"]*)"', s or "")}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def cmd_import(args):
    src = args.docx
    out = args.output or os.path.splitext(src)[0] + ".org"
    if os.path.abspath(out) == os.path.abspath(src):
        sys.exit("refusing to write over the source")
    if os.path.exists(out) and not args.force:
        sys.exit(f"{out} exists; use --force to replace it")
    d = Docx(src)
    tags = {}                                      # full name -> tag
    me_tag, me_name = None, None
    if args.me:
        me_tag, _, me_name = args.me.partition("=")
        tags[me_name.strip('"')] = me_tag
        for alias in args.alias:
            tags[alias.strip('"')] = me_tag
    names = {c["author"] or "" for c in d.comments.values()}
    names |= {el.get(w("author")) or "" for el in d.body.iter(w("ins"), w("del"), w("moveTo"), w("moveFrom"))}
    for name in sorted(names):
        if name not in tags:
            # Changes saved with no author name still need a tag, or they'd
            # come back as yours.
            tags[name] = make_tag(name, tags) if name else "Anon"
    paras = read_paras(d, tags)
    primary, aliases = {}, []
    for n, t in tags.items():
        if t in primary:
            if n in names:                         # another name the same person used here
                aliases.append(f'{t}="{n}"')
        else:
            primary[t] = n
    authors = " ".join(f'{t}="{n}"' for t, n in primary.items())
    title = os.path.splitext(os.path.basename(src))[0]
    lines = [f"#+title: {title}",
             f"#+review_source: {os.path.relpath(src, os.path.dirname(os.path.abspath(out)))}",
             f"#+review_authors: {authors}",
             *([f"#+review_aliases: {' '.join(aliases)}"] if aliases else []),
             "#+startup: showeverything", ""]
    for p in paras:
        if p.opaque is None and not p.chars and not p.comments:
            continue                               # empty paragraphs stay in the docx
        lines.append(render(p))
        lines.append("")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(out)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def para_id() -> str:
    return f"{random.randrange(0x10000000, 0x7FFFFFFF):08X}"


class Exporter:
    def __init__(self, base: Docx, names: dict, me: Optional[str], tags: dict = None):
        self.base = base
        self.names = names                         # tag -> full name
        self.tags = tags or {v: k for k, v in names.items()}   # every name (aliases too) -> tag
        self.me = me
        self.stamp = now()
        ids = [int(x) for x in base.doc.xpath("//w:*/@w:id", namespaces={"w": W_NS}) if x.lstrip("-").isdigit()]
        self.next_change = max(ids + [0]) + 1000
        self.next_comment = max([int(c) for c in base.comments if c.isdigit()] + [0]) + 1
        self.openers = {}                          # span key -> placeholder range starts
        self.spans = {}                            # span key -> ids of the comment and replies
        self.base_index = {id(el): k for k, el in enumerate(base.body)}
        self.used_base = set()
        self.out_comments = []                     # (id, author, initials, date, text, parent_id, base_el)

    def name(self, tag):
        return self.names.get(tag, tag or self.names.get(self.me, self.me) or "Unknown")

    def comment_id(self, c: Comment, parent_id=None) -> str:
        """Reuse the original comment's id when author and text match.

        A leading check mark (DONE) means resolved; it's a marker in Word,
        not part of the comment's text.
        """
        full = self.name(c.author)
        done = c.text.startswith(DONE.strip())
        text = c.text[len(DONE.strip()):].strip() if done else c.text
        for cid, info in self.base.comments.items():
            same_author = (info["author"] == full or
                           (c.author and self.tags.get(info["author"] or "") == c.author))
            if cid not in self.used_base and same_author and info["text"] == text:
                self.used_base.add(cid)
                self.out_comments.append((cid, info["author"], info["initials"], info["date"], text,
                                          parent_id, info["el"], done))
                return cid
        cid = str(self.next_comment)
        self.next_comment += 1
        initials = "".join(x[0] for x in full.split()).upper() or None
        self.out_comments.append((cid, full, initials, self.stamp, text, parent_id, None, done))
        return cid

    def stretch_spans(self):
        """Point each opener at its comment, so the range starts there.

        clean_marks keeps the first start of each id, which is the opener's.
        """
        for key, els in self.openers.items():
            group = self.spans.get(key)
            for el in els:
                if not group:
                    el.getparent().remove(el)
                    continue
                el.set(w("id"), group[0])
                for cid in reversed(group[1:]):
                    extra = etree.Element(w("commentRangeStart"))
                    extra.set(w("id"), cid)
                    el.addnext(extra)

    def keep_base(self, c: Comment, parent_id=None):
        """A comment inside a paragraph copied verbatim keeps its id."""
        cid = c.base_id
        if cid and cid not in self.used_base and cid in self.base.comments:
            info = self.base.comments[cid]
            self.used_base.add(cid)
            self.out_comments.append((cid, info["author"], info["initials"], info["date"],
                                      info["text"], parent_id, info["el"], cid in self.base.done))
        for r in c.replies:
            self.keep_base(r, cid)

    def change_id(self) -> str:
        self.next_change += 1
        return str(self.next_change)

    # -- paragraph rebuild -------------------------------------------------

    def rebuild(self, para: Para, src_p, prev_p):
        """New w:p from PARA, borrowing formatting from SRC_P (or PREV_P)."""
        model = src_p if src_p is not None else prev_p
        p = etree.Element(w("p"), nsmap=None)
        if model is not None and model.find(w("pPr")) is not None:
            ppr = copy.deepcopy(model.find(w("pPr")))
            prpr = ppr.find(w("rPr"))
            if prpr is not None:                   # paragraph-mark marks belong to the old text
                for x in prpr.findall(w("ins")) + prpr.findall(w("del")):
                    prpr.remove(x)
            p.append(ppr)
        # Heading level from org stars.
        model_level = style_level(model) if model is not None else 0
        if para.level != model_level:
            ppr = p.find(w("pPr"))
            if ppr is None:
                ppr = etree.SubElement(p, w("pPr"))
                p.insert(0, ppr)
            ps = ppr.find(w("pStyle"))
            if src_p is not None and not ppr.findall(w("pPrChange")):
                # A heading level changed in org: a tracked style change,
                # so the author sees "Formatted: Heading 2" and can reject it.
                old = etree.Element(w("pPr"))
                for x in ppr:
                    if isinstance(x.tag, str) and etree.QName(x).localname not in ("rPr", "sectPr", "pPrChange"):
                        old.append(copy.deepcopy(x))
                chg = etree.SubElement(ppr, w("pPrChange"))
                chg.set(w("id"), self.change_id())
                chg.set(w("author"), self.name(self.me))
                chg.set(w("date"), self.stamp)
                chg.append(old)
            if para.level:
                if ps is None:
                    ps = etree.Element(w("pStyle"))
                    ppr.insert(0, ps)
                ps.set(w("val"), f"Heading{para.level}")
            elif ps is not None:
                ppr.remove(ps)

        # Formatting per character, lined up against the source paragraph.
        base_chars, anchors = [], []
        if src_p is not None:
            try:
                base_chars, _, anchors = walk_para(src_p, self.tags, self.base_index.get(id(src_p)))
            except Opaque:
                base_chars = []
        fallback = None
        if model is not None:
            r0 = model.find(f".//{w('r')}/{w('rPr')}")
            fallback = r0
        sm = difflib.SequenceMatcher(None, "".join(c.ch for c in base_chars),
                                     "".join(c.ch for c in para.chars), autojunk=False)
        match, b2o = {}, {}
        for a, b, size in sm.get_matching_blocks():
            for t in range(size):
                match[b + t] = a + t
                b2o[a + t] = b + t
        # Bookmarks live between characters; move each one to the matching
        # spot in the new text (the next character that survived).
        marks_at = {}
        for pos, el in anchors:
            q = pos
            while q < len(base_chars) and q not in b2o:
                q += 1
            marks_at.setdefault(b2o.get(q, len(para.chars)), []).append(el)
        # What org could show of the original's italics and bold.  Where the
        # org text still says exactly that, the original formatting stands,
        # so styling org can't spell (an italic bracket, say) never comes
        # back as a change nobody made.
        seen_as = [(c.i, c.b) for c in base_chars]
        if base_chars:
            rt = parse_para(render(Para(base_chars, [])), None).chars
            if [c.ch for c in rt] == [c.ch for c in base_chars]:
                seen_as = [(c.i, c.b) for c in rt]
        rprs, dates, last = [], [], fallback
        anames = [None] * len(para.chars)
        for j, c in enumerate(para.chars):
            if j in match:
                bc = base_chars[match[j]]
                if (bc.chg, bc.author) == (c.chg, c.author):
                    anames[j] = bc.aname           # keep the author's name as Word had it
                if (c.i, c.b) == seen_as[match[j]]:
                    c.i, c.b = bc.i, bc.b
                rpr = bc.rpr
                date = bc.date if (bc.chg, bc.author) == (c.chg, c.author) else None
            else:
                rpr, date = last, None
            if c.obj:                              # objects bring their own formatting
                rprs.append(None)
                dates.append(date)
                continue
            last = rpr
            rprs.append(self.style_rpr(rpr, c))
            dates.append(date)

        # Comment ids, and where their marks go.
        starts, ends, pts, opens = {}, {}, {}, {}
        for c in para.comments:
            if c.opener:
                opens.setdefault(c.start, []).append(c.opener)
                continue
            cid = self.comment_id(c)
            reply_ids = [self.comment_id(r, cid) for r in c.replies]
            group = [cid] + reply_ids
            if c.span:
                self.spans[c.span] = group
            if c.point or c.end <= c.start:
                pts.setdefault(c.start, []).extend(group)
            else:
                starts.setdefault(c.start, []).extend(group)
                ends.setdefault(c.end, []).extend(group)

        n = len(para.chars)
        cuts = sorted({0, n} | set(starts) | set(ends) | set(pts) | set(opens) | set(marks_at))
        wrapper, wrap_key = None, None
        for pos in range(n + 1):
            if pos in cuts:
                wrapper = None                     # marks interrupt the change wrapper
                for el in marks_at.get(pos, []):
                    p.append(copy.deepcopy(el))
                for cid in ends.get(pos, []):
                    etree.SubElement(p, w("commentRangeEnd")).set(w("id"), cid)
                    self.ref_run(p, cid)
                for cid in pts.get(pos, []):
                    etree.SubElement(p, w("commentRangeStart")).set(w("id"), cid)
                    etree.SubElement(p, w("commentRangeEnd")).set(w("id"), cid)
                    self.ref_run(p, cid)
                for cid in starts.get(pos, []):
                    etree.SubElement(p, w("commentRangeStart")).set(w("id"), cid)
                for key in opens.get(pos, []):
                    self.openers.setdefault(key, []).append(etree.SubElement(p, w("commentRangeStart")))
            if pos == n:
                break
            c = para.chars[pos]
            key = (c.chg, c.author, dates[pos], anames[pos])
            if c.chg is None:
                wrapper, parent = None, p
            else:
                if wrapper is None or wrap_key != key:
                    wrapper = etree.SubElement(p, w(c.chg))
                    wrapper.set(w("id"), self.change_id())
                    wrapper.set(w("author"), anames[pos] if anames[pos] is not None else self.name(c.author))
                    wrapper.set(w("date"), dates[pos] or self.stamp)
                    wrap_key = key
                parent = wrapper
            if c.obj:
                self.put_object(p, parent, c)
                if c.chg is None or etree.QName(p[-1]).localname not in ("ins", "del"):
                    wrapper = None
                continue
            self.put_char(parent, c, rprs[pos])

        for ch in p.iter(w("rPrChange")):
            ch.set(w("id"), self.change_id())

        # A paragraph that is all deletion (or all insertion) takes its
        # paragraph mark along, so accepting the change joins paragraphs.
        # So does one split or joined while tracking (para.mark).
        pm = para.mark
        if not pm and n and len({c.chg for c in para.chars}) == 1 and para.chars[0].chg:
            pm = (para.chars[0].chg, para.chars[0].author)
        if pm:
            ppr = p.find(w("pPr"))
            if ppr is None:
                ppr = etree.Element(w("pPr"))
                p.insert(0, ppr)
            prpr = ppr.find(w("rPr"))
            if prpr is None:
                prpr = etree.Element(w("rPr"))
                later = [x for x in ppr if isinstance(x.tag, str) and
                         etree.QName(x).localname in ("sectPr", "pPrChange")]
                if later:
                    later[0].addprevious(prpr)
                else:
                    ppr.append(prpr)
            mark = etree.Element(w(pm[0]))
            prpr.insert(0, mark)                   # ins/del come first in a mark's rPr
            mark.set(w("id"), self.change_id())
            mark.set(w("author"), self.name(pm[1]))
            mark.set(w("date"), self.stamp)
        return p

    def style_rpr(self, rpr, c: Ch):
        """Copy RPR, switching italic/bold to match the org markup.

        A switch is recorded as a tracked formatting change (w:rPrChange),
        so the client sees "Formatted: Italic" and can reject it.
        """
        new = copy.deepcopy(rpr) if rpr is not None else etree.Element(w("rPr"))
        kept = new.findall(w("rPrChange"))        # the client's own formatting changes
        for x in kept:
            new.remove(x)
        old = copy.deepcopy(new)
        changed = False
        for tag, want in (("i", c.i), ("b", c.b)):
            if c.ch.isspace():
                break                              # org can't mark spaces; leave theirs be
            if on(new, tag) != want:
                changed = True
                for t in (tag, tag + "Cs"):
                    for x in new.findall(w(t)):
                        new.remove(x)
                if want:
                    insert_ordered(new, etree.Element(w(tag)))
        if changed and rpr is not None and c.chg is None:
            ch = etree.SubElement(new, w("rPrChange"))
            ch.set(w("id"), "0")                   # numbered once the paragraph is done
            ch.set(w("author"), self.name(self.me))
            ch.set(w("date"), self.stamp)
            ch.append(old)
        elif not changed:
            for x in kept:
                new.append(x)
        return new

    def put_char(self, parent, c: Ch, rpr):
        """Append C to the last run under PARENT if formatting matches."""
        key = etree.tostring(rpr)
        last = parent[-1] if len(parent) else None
        if not (last is not None and etree.QName(last).localname == "r"
                and last.get("_k") == str(hash(key))):
            last = etree.SubElement(parent, w("r"))
            last.set("_k", str(hash(key)))
            if len(rpr):
                last.append(copy.deepcopy(rpr))
        tname = "delText" if c.chg == "del" else "t"
        special = {"\t": "tab", BR: "br", "\u2011": "noBreakHyphen", "\u00ad": "softHyphen"}
        if c.ch in special:
            etree.SubElement(last, w(special[c.ch]))
        else:
            t = last[-1] if len(last) and etree.QName(last[-1]).localname == tname else None
            if t is None:
                t = etree.SubElement(last, w(tname))
                t.set(f"{{{XML_NS}}}space", "preserve")
                t.text = ""
            t.text += c.ch

    def put_object(self, p, parent, c: Ch):
        """Copy an inline object back in, tracked if it's inside a change."""
        nodes = self.base.objects.get(c.obj)
        if nodes is None:                          # not from this document: keep its text
            if c.desc:
                for ch in c.desc:
                    self.put_char(parent, Ch(ch, c.chg, c.author), etree.Element(w("rPr")))
            return
        nodes = [copy.deepcopy(n) for n in nodes]
        if c.chg is None:
            for n in nodes:
                p.append(n)
            return
        runs_only = all(etree.QName(n).localname == "r" for n in nodes)
        if runs_only:
            for n in nodes:
                if c.chg == "del":
                    for t in n.iter(w("t")):
                        t.tag = w("delText")
                    for t in n.iter(w("instrText")):
                        t.tag = w("delInstrText")
                parent.append(n)
            return
        # A container (link, smart tag, content control): mark each run
        # inside it, which Word and LibreOffice both accept.
        for n in nodes:
            for r in list(n.iter(w("r"))):
                wrap = etree.Element(w(c.chg))
                wrap.set(w("id"), self.change_id())
                wrap.set(w("author"), self.name(c.author))
                wrap.set(w("date"), self.stamp)
                if c.chg == "del":
                    for t in r.iter(w("t")):
                        t.tag = w("delText")
                    for t in r.iter(w("instrText")):
                        t.tag = w("delInstrText")
                r.addprevious(wrap)
                wrap.append(r)
            p.append(n)

    @staticmethod
    def ref_run(p, cid):
        r = etree.SubElement(p, w("r"))
        etree.SubElement(r, w("commentReference")).set(w("id"), cid)

    # -- comments.xml --------------------------------------------------------

    def comments_xml(self):
        nsmap = {"w": W_NS, "w14": W14_NS}
        root = etree.Element(w("comments"), nsmap=nsmap)
        ex = etree.Element(f"{{{W15_NS}}}commentsEx", nsmap={"w15": W15_NS, "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006"})
        ex.set("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable", "w15")
        pid_of, rows = {}, []
        for cid, author, initials, date, text, parent, base_el, done in sorted(
                self.out_comments, key=lambda x: int(x[0]) if x[0].isdigit() else 0):
            if base_el is not None:
                c = copy.deepcopy(base_el)
            else:
                c = etree.Element(w("comment"))
                for k, part in enumerate(text.split(PARA_SEP)):
                    cp = etree.SubElement(c, w("p"))
                    if k == 0:
                        etree.SubElement(etree.SubElement(cp, w("r")), w("annotationRef"))
                    r = etree.SubElement(cp, w("r"))
                    t = etree.SubElement(r, w("t"))
                    t.set(f"{{{XML_NS}}}space", "preserve")
                    t.text = part
            c.set(w("id"), cid)
            c.set(w("author"), author)
            if date:
                c.set(w("date"), date)
            if initials:
                c.set(w("initials"), initials)
            paras = c.findall(w("p"))
            last_p = paras[-1] if paras else etree.SubElement(c, w("p"))
            pid = last_p.get(f"{{{W14_NS}}}paraId") or para_id()
            last_p.set(f"{{{W14_NS}}}paraId", pid)
            pid_of[cid] = pid
            root.append(c)
            rows.append((cid, parent, done))
        for cid, parent, done in rows:
            ce = etree.SubElement(ex, f"{{{W15_NS}}}commentEx")
            ce.set(f"{{{W15_NS}}}paraId", pid_of[cid])
            if parent and parent in pid_of:
                ce.set(f"{{{W15_NS}}}paraIdParent", pid_of[parent])
            ce.set(f"{{{W15_NS}}}done", "1" if done else "0")
        return root, ex


RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike",
             "dstrike", "outline", "shadow", "emboss", "imprint", "noProof", "snapToGrid",
             "vanish", "webHidden", "color", "spacing", "w", "kern", "position", "sz", "szCs",
             "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign", "rtl", "cs",
             "em", "lang", "eastAsianLayout", "specVanish", "oMath"]


def insert_ordered(rpr, el):
    """Word checks child order in w:rPr, so put EL where the schema wants it."""
    rank = RPR_ORDER.index(etree.QName(el).localname)
    for k, child in enumerate(rpr):
        name = etree.QName(child).localname if isinstance(child.tag, str) else ""
        if name in RPR_ORDER and RPR_ORDER.index(name) > rank:
            rpr.insert(k, el)
            return
    rpr.append(el)


def clean_marks(body, comment_ids, known=None):
    """One start, one end, one reference per comment; none for lost comments.

    Marks for ids the original never defined (orphans Word sometimes
    leaves behind) are left exactly as they were.
    """
    seen = {}
    for kind in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        els = list(body.iter(w(kind)))
        if kind == "commentRangeEnd":
            els.reverse()                          # keep the last end
        for el in els:
            cid = el.get(w("id"))
            if known is not None and cid not in known and cid not in comment_ids:
                continue                           # an orphan from the original: not ours to touch
            if cid not in comment_ids or (kind, cid) in seen:
                parent = el.getparent()
                parent.remove(el)
                if kind == "commentReference" and etree.QName(parent).localname == "r" \
                        and not any(etree.QName(x).localname != "rPr" for x in parent):
                    parent.getparent().remove(parent)
            else:
                seen[(kind, cid)] = el
    # Pair up what's left: a start with no end (or the reverse) closes on
    # the spot, and every comment keeps one reference.
    for cid in comment_ids:
        st, en = seen.get(("commentRangeStart", cid)), seen.get(("commentRangeEnd", cid))
        if en is not None and st is None:
            st = etree.Element(w("commentRangeStart"))
            st.set(w("id"), cid)
            en.addprevious(st)
        elif st is not None and en is None:
            en = etree.Element(w("commentRangeEnd"))
            en.set(w("id"), cid)
            st.addnext(en)
        if ("commentReference", cid) not in seen and en is not None:
            r = etree.Element(w("r"))
            etree.SubElement(r, w("commentReference")).set(w("id"), cid)
            en.addnext(r)


def ensure_part(rels, types, target, rel_type, part, ctype):
    if not any(r.get("Type") == rel_type for r in rels):
        ids = {r.get("Id") for r in rels}
        n = 1
        while f"rId{n}" in ids:
            n += 1
        el = etree.SubElement(rels, f"{{{PKG_REL_NS}}}Relationship")
        el.set("Id", f"rId{n}")
        el.set("Type", rel_type)
        el.set("Target", target)
    if not any(o.get("PartName") == part for o in types.findall(f"{{{CT_NS}}}Override")):
        el = etree.SubElement(types, f"{{{CT_NS}}}Override")
        el.set("PartName", part)
        el.set("ContentType", ctype)


# Settings that come before w:trackRevisions in the schema's order.
SETTINGS_BEFORE = set("""writeProtection view zoom removePersonalInformation removeDateAndTime
    doNotDisplayPageBoundaries displayBackgroundShape printPostScriptOverText
    printFractionalCharacterWidth printFormsData embedTrueTypeFonts embedSystemFonts
    saveSubsetFonts saveFormsData mirrorMargins alignBordersAndEdges bordersDoNotSurroundHeader
    bordersDoNotSurroundFooter gutterAtTop hideSpellingErrors hideGrammaticalErrors
    activeWritingStyle proofState formsDesign attachedTemplate linkStyles stylePaneFormatFilter
    stylePaneSortMethod documentType mailMerge revisionView""".split())


def track_revisions_on(data: bytes):
    """Settings XML with Word's Track Changes switched on, or None if it already is.

    The author's answers to the edit then show as tracked changes too.
    """
    root = etree.fromstring(data)
    if root.find(w("trackRevisions")) is not None:
        return None
    at = 0
    for i, el in enumerate(root):
        if isinstance(el.tag, str) and etree.QName(el).namespace == W_NS \
                and etree.QName(el).localname in SETTINGS_BEFORE:
            at = i + 1
    root.insert(at, etree.Element(w("trackRevisions")))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def cmd_export(args):
    meta, blocks = read_org(args.org)
    src_rel = meta.get("review_source")
    if not src_rel:
        sys.exit("no #+review_source: line, so there's no original .docx to patch")
    base_path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(args.org)), src_rel))
    out = args.output or os.path.splitext(args.org)[0] + "-edited.docx"
    if os.path.abspath(out) == os.path.abspath(base_path):
        sys.exit("refusing to write over the original .docx")
    names = parse_authors(meta.get("review_authors", ""))
    me = args.me.partition("=")[0] if args.me else None
    if args.me and me not in names:
        names[me] = args.me.partition("=")[2].strip('"')

    base = Docx(base_path)
    tags = {v: k for k, v in names.items()}
    for m in re.finditer(r'(\S+?)="([^"]*)"', meta.get("review_aliases", "")):
        tags[m.group(2)] = m.group(1)
    for n in {c["author"] or "" for c in base.comments.values()}:
        tags.setdefault(n, make_tag(n, tags) if n else "Anon")
    base_paras = read_paras(base, tags)
    base_els = [el for el in base.body if isinstance(el.tag, str) and etree.QName(el).localname != "sectPr"]
    base_strs = [render(p) for p in base_paras]
    org_strs = blocks
    ex = Exporter(base, names, me, tags)

    # Empty paragraphs never reach the org.  Leave them out of the
    # alignment and put each one back after the paragraph it followed.
    real, trailing, leading = [], {}, []
    for bi, txt in enumerate(base_strs):
        if txt.strip():
            real.append(bi)
        elif real:
            trailing.setdefault(real[-1], []).append(base_els[bi])
        else:
            leading.append(base_els[bi])

    new_body = list(leading)
    kept = rebuilt = dropped = 0
    last_p = None

    def emit_base(bi):
        nonlocal last_p, kept
        el = base_els[bi]
        new_body.append(el)
        if etree.QName(el).localname == "p":
            last_p = el
        for c in base_paras[bi].comments:
            ex.keep_base(c)
        if base_paras[bi].opaque is not None:      # comments inside tables and such
            for ref in el.iter(w("commentReference")):
                ex.keep_base(Comment(None, "", base_id=ref.get(w("id"))))
        kept += 1

    def emit_rebuilt(text, bi):
        nonlocal last_p, rebuilt
        m = re.match(r"#\+docx_block:\s*(\d+)", text)
        if m:
            idx = int(m.group(1))
            if idx < len(base.body):
                new_body.append(base.body[idx])
            return
        src = base_els[bi] if bi is not None and base_paras[bi].opaque is None \
            and etree.QName(base_els[bi]).localname == "p" else None
        if bi is not None and src is None:
            new_body.append(base_els[bi])          # never rebuild what we can't read
        p = ex.rebuild(parse_para(text, me), src, last_p)
        new_body.append(p)
        last_p = p
        rebuilt += 1

    def drop(bi):
        nonlocal dropped
        dropped += 1
        print(f"note: paragraph left out of the export, untracked: {base_strs[bi][:60]!r}",
              file=sys.stderr)

    def plain(t):
        """The text as it stood before the marked changes, for pairing."""
        return TOKEN.sub(lambda m: m.group("del") or m.group("hl") or m.group("old") or "", t)

    sm = difflib.SequenceMatcher(None, [base_strs[b] for b in real], org_strs, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                bi = real[i1 + k]
                emit_base(bi)
                new_body.extend(trailing.get(bi, []))
            continue
        # Pair edited paragraphs with their originals by similarity, in order.
        cands = [real[x] for x in range(i1, i2)]
        ptr = 0
        for oj in range(j1, j2):
            best = None
            for q in range(ptr, len(cands)):
                r = difflib.SequenceMatcher(None, plain(base_strs[cands[q]]), plain(org_strs[oj]),
                                            autojunk=False).ratio()
                if r >= 0.5:
                    best = q
                    break
            if best is None:
                emit_rebuilt(org_strs[oj], None)
                continue
            for q in range(ptr, best):
                drop(cands[q])
                new_body.extend(trailing.get(cands[q], []))
            emit_rebuilt(org_strs[oj], cands[best])
            new_body.extend(trailing.get(cands[best], []))
            ptr = best + 1
        for q in range(ptr, len(cands)):
            drop(cands[q])
            new_body.extend(trailing.get(cands[q], []))

    sect = base.body.find(w("sectPr"))
    for el in list(base.body):
        base.body.remove(el)
    for el in new_body:
        base.body.append(el)
    if sect is not None:
        base.body.append(sect)
    for r in base.body.iter(w("r")):
        r.attrib.pop("_k", None)
    ex.stretch_spans()
    clean_marks(base.body, {c[0] for c in ex.out_comments}, set(base.comments))

    xml = lambda el: etree.tostring(el, xml_declaration=True, encoding="UTF-8", standalone=True)
    rels, types = base.rels, etree.fromstring(base.zf.read("[Content_Types].xml"))
    parts = {base.main: xml(base.doc)}
    if ex.out_comments or base.comments_part in base.names:
        croot, cex = ex.comments_xml()
        folder = posixpath.dirname(base.main)
        parts[base.comments_part] = xml(croot)
        parts[base.comments_ex_part] = xml(cex)
        ensure_part(rels, types, posixpath.relpath(base.comments_part, folder), REL_COMMENTS,
                    "/" + base.comments_part, CT_COMMENTS)
        ensure_part(rels, types, posixpath.relpath(base.comments_ex_part, folder), REL_COMMENTS_EX,
                    "/" + base.comments_ex_part, CT_COMMENTS_EX)
        parts[base.main_rels] = xml(rels)
    parts["[Content_Types].xml"] = xml(types)
    if not args.keep_settings:
        for r in rels:
            if (r.get("Type") or "").endswith("/settings"):
                name = posixpath.normpath(posixpath.join(posixpath.dirname(base.main), r.get("Target")))
                if name in base.names:
                    changed = track_revisions_on(base.zf.read(name))
                    if changed is not None:
                        parts[name] = changed

    tmp = out + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in base.names:
            data = parts.pop(name, None)
            zout.writestr(base.zf.getinfo(name), data if data is not None else base.zf.read(name))
        for name, data in parts.items():
            zout.writestr(name, data)
    os.replace(tmp, out)
    print(out)
    print(f"{kept} paragraphs copied unchanged, {rebuilt} rebuilt, {dropped} dropped", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"redline {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("import", help="docx -> org with CriticMarkup")
    a.add_argument("docx")
    a.add_argument("-o", "--output")
    a.add_argument("--me", help='your tag and name, like SLW="Stephen Lloyd Webber"')
    a.add_argument("--alias", action="append", default=[],
                   help="another name you've used in Word (repeatable); shown with your tag")
    a.add_argument("--force", action="store_true", help="replace an existing .org")
    b = sub.add_parser("export", help="org -> docx, patched onto the original")
    b.add_argument("org")
    b.add_argument("-o", "--output")
    b.add_argument("--me", help="your tag and name, for changes with no author")
    b.add_argument("--keep-settings", action="store_true",
                   help="leave Word's Track Changes switch as the original had it")
    args = ap.parse_args(argv)
    (cmd_import if args.cmd == "import" else cmd_export)(args)


if __name__ == "__main__":
    main()
