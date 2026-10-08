"""Run the bridge over a folder of .docx files and report what breaks.

    python3 tests/corpus.py redline.py FOLDER

For each file: import, export with no edits (every body element should
come back unchanged), then make edits of each kind cm-mode makes, export,
re-import, and check the org reads the same.  pandoc's test/docx folder
(github.com/jgm/pandoc) is a good corpus: 88 files, many saved by Word.
"""
import os, re, sys, shutil, subprocess, tempfile, zipfile, traceback
from lxml import etree
BR = os.path.abspath(sys.argv[1]); CORPUS = sys.argv[2]
sys.path.insert(0, os.path.dirname(BR)); import redline
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ME = 'SLW=Stephen Lloyd Webber'
ALIASES = ['--alias', 'Stephen Webber', '--alias', 'Stephen W']
def run(*a):
    r = subprocess.run([sys.executable, BR, *a], capture_output=True, text=True)
    return r
def body(p):
    d = redline.Docx(p)
    return [etree.tostring(x, method="c14n", exclusive=True) for x in d.body]
def ir(org):
    bl = redline.org_blocks("\n".join(l for l in org.split("\n") if not (l.startswith("#+") and not l.startswith("#+docx_block"))))
    vis = lambda c, f: f and not c.ch.isspace()
    return [[(c.ch, c.chg, c.author, vis(c,c.i), vis(c,c.b)) for c in redline.parse_para(b, None).chars] for b in bl]
def edit(org):
    """Edits of every kind cm-mode makes, spread over the document."""
    head, body_ = org.split("\n", 4)[:4], org.split("\n", 4)[4]
    blocks = body_.split("\n\n"); n = 0
    LINKRE = re.compile(r"\[\[docx:[0-9.]+\](?:\[[^\]]*\])?\]")
    for i, b in enumerate(blocks):
        if b.strip().startswith(("#+", "*")) or len(b) < 12: continue
        kind = n % 6
        if kind == 0 and "{" not in b:
            m = re.search(r"\w+ ", b)
            if not m: continue
            blocks[i] = b[:m.end()] + "{++really ++}{>>@SLW<<}" + b[m.end():]
        elif kind == 1 and "{" not in b:
            m = re.search(r"(?<![\[\w:.])(\w{4,})(?![\w\]])", b)
            if not m: continue
            blocks[i] = b[:m.start()] + "{--" + m.group(1) + "--}{>>@SLW Cut?<<}" + b[m.end():]
        elif kind == 2:
            m = LINKRE.search(b)
            if not m or "{" in b: continue
            blocks[i] = b[:m.start()] + "{--" + m.group(0) + "--}{>>@SLW<<}" + b[m.end():]
        elif kind == 3 and "{" not in b:
            m = re.search(r"(?<![\[\w:.])(\w{3,} \w{3,})(?![\w\]])", b)
            if not m: continue
            blocks[i] = b[:m.start()] + "{==" + m.group(1) + "==}{>>@SLW Check this.<<}" + b[m.end():]
        elif kind == 4:
            k = b.find("<<}")
            if k < 0: continue
            blocks[i] = b[:k + 3] + "{>>@SLW Agreed.<<}" + b[k + 3:]
        elif kind == 5:
            m = re.search(r"\{\+\+((?:(?!\+\+\}).)*?)\+\+\}\{>>@[^<\s]+<<\}", b, re.S)
            if not m: continue
            blocks[i] = b[:m.start()] + m.group(1) + b[m.end():]
        else:
            continue
        n += 1
        if n >= 12: break
    return "\n".join(head) + "\n" + "\n\n".join(blocks), n
res = {}
for f in sorted(os.listdir(CORPUS)):
    if not f.endswith(".docx"): continue
    tmp = tempfile.mkdtemp(); src = os.path.join(tmp, f); shutil.copy(os.path.join(CORPUS, f), src)
    org = src[:-5] + ".org"; status = []
    try:
        r = run("import", src, "-o", org, "--me", ME, *ALIASES)
        if r.returncode: res[f] = "IMPORT FAIL " + r.stderr.strip().splitlines()[-1][:150]; continue
        r = run("export", org, "-o", os.path.join(tmp, "id.docx"), "--me", ME)
        if r.returncode: res[f] = "IDEXPORT FAIL " + r.stderr.strip().splitlines()[-1][:150]; continue
        a, b = body(src), body(os.path.join(tmp, "id.docx"))
        same = sum(x == y for x, y in zip(a, b))
        if len(a) != len(b) or same != len(a): status.append(f"noop-diff {same}/{len(a)} vs {len(b)}")
        text = open(org).read()
        ed, n = edit(text)
        open(org, "w").write(ed)
        r = run("export", org, "-o", os.path.join(tmp, "ed.docx"), "--me", ME)
        if r.returncode: res[f] = "EDEXPORT FAIL " + r.stderr.strip().splitlines()[-1][:150]; continue
        r = run("import", os.path.join(tmp, "ed.docx"), "-o", os.path.join(tmp, "back.org"), "--me", ME, *ALIASES, "--force")
        if r.returncode: res[f] = "REIMPORT FAIL " + r.stderr.strip().splitlines()[-1][:150]; continue
        back = open(os.path.join(tmp, "back.org")).read()
        if ir(back) != ir(ed): status.append("reimport-differs")
        status.append(f"edits={n}")
        res[f] = "ok " + " ".join(status)
    except Exception as e:
        res[f] = "PYERR " + repr(e)[:150]
    finally:
        pass
    if res[f].startswith("ok") and "diff" not in res[f] and not os.environ.get("KEEP"):
        shutil.rmtree(tmp, ignore_errors=True)        # keep only the ones worth a look
    else:
        res[f] += " " + tmp
for k, v in res.items(): print(f"{k:45} {v}")
