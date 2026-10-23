#!/usr/bin/env python3
"""Scrape the Metasploit Framework YARD API reference into Markdown.

Downloads every class page under https://docs.metasploit.com/api/ and converts the YARD
`#content` region to Markdown, one file per class, mirroring YARD's namespace layout, into
`metasploit-api-docs/` (the corpus the RAG ingests). stdlib only.

    python scripts/scrape_api_docs.py [OUT_DIR] [--workers N]

The output is a mirror of Rapid7's documentation and is intentionally not committed to the
repo; regenerate it with this script. Then index it with scripts/ingest_api_docs.py.
"""
import argparse
import html
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

BASE = "https://docs.metasploit.com/api/"
ROOT = Path(__file__).resolve().parent.parent


def fetch(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "msfrag-docs-mirror/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError):
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def class_paths():
    s = fetch(BASE + "class_list.html")
    paths = re.findall(r'href="(\S+?\.html)"', s)
    skip = {"class_list.html", "method_list.html", "file_list.html"}
    seen, out = set(), []
    for p in paths:
        if p.startswith("http") or p in skip or "css" in p or p.startswith("js/"):
            continue
        if p not in seen:
            seen.add(p); out.append(p)
    return out


# ---- minimal HTML -> Markdown for the YARD #content region ----
class MD(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.pre = [], 0, 0
        self.href, self.link_text = None, []
        self.list_stack, self.row, self.cell, self.in_table, self.table_rows = [], [], None, 0, []
        self.heading = None

    def w(self, t):
        (self.cell if self.cell is not None else self.out).append(t)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1; return
        if self.skip:
            return
        if tag == "a":
            self.href = a.get("href"); self.link_text = []; return
        if tag == "pre":
            self.pre += 1; self.w("\n```\n"); return
        if tag == "code" and not self.pre:
            self.w("`"); return
        if tag == "br":
            self.w("\n"); return
        if tag == "hr":
            self.w("\n\n---\n\n"); return
        if tag in ("strong", "b"):
            self.w("**"); return
        if tag in ("em", "i"):
            self.w("*"); return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.w("\n\n" + "#" * int(tag[1]) + " "); self.heading = tag; return
        if tag == "p":
            self.w("\n\n"); return
        if tag == "ul":
            self.list_stack.append(["ul", 0]); self.w("\n"); return
        if tag == "ol":
            self.list_stack.append(["ol", 0]); self.w("\n"); return
        if tag == "li":
            ind = "  " * (len(self.list_stack) - 1)
            if self.list_stack and self.list_stack[-1][0] == "ol":
                self.list_stack[-1][1] += 1
                self.w("\n" + ind + f"{self.list_stack[-1][1]}. ")
            else:
                self.w("\n" + ind + "- ")
            return
        if tag in ("dt", "dd", "div"):
            self.w("\n"); return
        if tag == "table":
            self.in_table += 1; self.table_rows = []; return
        if tag == "tr" and self.in_table:
            self.row = []; return
        if tag in ("td", "th") and self.in_table:
            self.cell = []; return
        if tag == "blockquote":
            self.w("\n\n> "); return

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            if self.skip:
                self.skip -= 1
            return
        if self.skip:
            return
        if tag == "a":
            txt = "".join(self.link_text).strip()
            if self.href and txt and not self.href.startswith("#"):
                self.w(f"[{txt}]({self.href})")
            else:
                self.w(txt)
            self.href = None; self.link_text = []; return
        if tag == "pre":
            self.pre -= 1 if self.pre else 0; self.w("\n```\n"); return
        if tag == "code" and not self.pre:
            self.w("`"); return
        if tag in ("strong", "b"):
            self.w("**"); return
        if tag in ("em", "i"):
            self.w("*"); return
        if tag and tag[0] == "h" and tag[1:].isdigit():
            self.w("\n"); self.heading = None; return
        if tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self.w("\n"); return
        if tag in ("td", "th") and self.in_table:
            self.row.append("".join(self.cell).strip().replace("\n", " ")); self.cell = None; return
        if tag == "tr" and self.in_table:
            if self.row:
                self.table_rows.append(self.row)
            self.row = []; return
        if tag == "table" and self.in_table:
            self.in_table -= 1
            if self.table_rows:
                self.out.append("\n\n")
                hdr = self.table_rows[0]
                self.out.append("| " + " | ".join(hdr) + " |\n")
                self.out.append("| " + " | ".join("---" for _ in hdr) + " |\n")
                for r in self.table_rows[1:]:
                    r = (r + [""] * len(hdr))[:len(hdr)]
                    self.out.append("| " + " | ".join(r) + " |\n")
                self.out.append("\n")
            self.table_rows = []; return

    def handle_data(self, d):
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(d); return
        if self.pre:
            self.w(d); return
        d = re.sub(r"[ \t]+", " ", d)
        if d.strip() == "" and d.count("\n") == 0 and not (self.out and self.out[-1][-1:] == " "):
            self.w(" "); return
        self.w(d)


def extract_content(page):
    i = page.find('id="content"')
    if i == -1:
        return None
    start = page.rfind("<div", 0, i)
    if start == -1:
        start = i
    depth, j = 0, start
    for m in re.compile(r"</?div\b", re.I).finditer(page, start):
        if page[m.start():m.start() + 2].lower() == "<d":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                j = m.end() + page[m.end():].find(">") + 1; break
    return page[start:j]


def to_md(page, title):
    c = extract_content(page) or page
    c = re.sub(r'<div[^>]*id="search"[^>]*>.*?</div>', "", c, flags=re.S | re.I)
    p = MD(); p.feed(c)
    text = "".join(p.out)
    text = re.sub(r"\*\*\s*\*\*", "", text)
    text = re.sub(r"\*\s*\*", "", text)
    text = re.sub(r"\*{3,}", "**", text)
    lines = [re.sub(r"[ \t]+$", "", l) for l in text.splitlines()]
    lines = ["" if l.strip() == "" else l for l in lines]
    text = "\n".join(lines)
    text = re.sub(r"(?m)^(#{1,6})[ \t]*\n+[ \t]*(?=\S)", r"\1 ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n```\n+```\n", "\n", text)
    text = text.strip()
    body = re.sub(r"^#+\s*(Module|Class|Exception):?\s*.*\n+", "", text, count=1)
    return "# " + title + "\n\n" + body.strip() + "\n"


def title_of(page, path):
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    if m:
        t = html.unescape(m.group(1)).strip()
        t = re.sub(r"\s*&mdash;.*$", "", t); t = re.sub(r"\s*—.*$", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t
    return path[:-5].replace("_", "::")


def work(path, out_dir):
    try:
        page = fetch(BASE + path)
        md = to_md(page, title_of(page, path))
        dst = Path(out_dir) / (path[:-5] + ".md")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(md, encoding="utf-8")
        return path, len(md), None
    except Exception as e:
        return path, 0, str(e)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out", nargs="?", default=str(ROOT / "metasploit-api-docs"))
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)

    paths = class_paths()
    print(f"{len(paths)} class pages -> {a.out}", flush=True)
    done, errs = 0, []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, p, a.out): p for p in paths}
        for fu in as_completed(futs):
            path, n, err = fu.result(); done += 1
            if err:
                errs.append((path, err))
            if done % 200 == 0 or done == len(paths):
                print(f"  {done}/{len(paths)}  errors={len(errs)}", flush=True)
    print("done:", done, "pages,", len(errs), "errors", flush=True)
    if errs:
        (Path(a.out) / "_errors.log").write_text("\n".join(f"{p}\t{e}" for p, e in errs), encoding="utf-8")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
