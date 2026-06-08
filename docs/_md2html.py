#!/usr/bin/env python3
"""把操作手册 Markdown 转成带样式的 HTML（供 textutil→docx、Chrome→pdf 用）。"""
import html
import re
import sys

src, out = sys.argv[1], sys.argv[2]
lines = open(src, encoding="utf-8").read().splitlines()


def inline(t):
    t = html.escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return t


body = []
i = 0
n = len(lines)
while i < n:
    ln = lines[i]
    s = ln.strip()
    if not s:
        i += 1
        continue
    # 表格
    if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|?\s*$", lines[i + 1].strip()):
        header = [c.strip() for c in s.strip("|").split("|")]
        i += 2
        rows = []
        while i < n and lines[i].strip().startswith("|"):
            rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
            i += 1
        body.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr></thead><tbody>")
        for r in rows:
            body.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
        body.append("</tbody></table>")
        continue
    # 标题
    m = re.match(r"^(#{1,6})\s+(.*)$", s)
    if m:
        lvl = len(m.group(1))
        body.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
        i += 1
        continue
    # 分割线
    if re.match(r"^---+$", s):
        body.append("<hr/>")
        i += 1
        continue
    # 引用
    if s.startswith(">"):
        quote = []
        while i < n and lines[i].strip().startswith(">"):
            quote.append(inline(lines[i].strip()[1:].strip()))
            i += 1
        body.append("<blockquote>" + "<br/>".join(quote) + "</blockquote>")
        continue
    # 有序列表
    if re.match(r"^\d+\.\s+", s):
        items = []
        while i < n and re.match(r"^\d+\.\s+", lines[i].strip()):
            items.append("<li>" + inline(re.sub(r"^\d+\.\s+", "", lines[i].strip())) + "</li>")
            i += 1
        body.append("<ol>" + "".join(items) + "</ol>")
        continue
    # 无序列表
    if s.startswith("- "):
        items = []
        while i < n and lines[i].strip().startswith("- "):
            items.append("<li>" + inline(lines[i].strip()[2:]) + "</li>")
            i += 1
        body.append("<ul>" + "".join(items) + "</ul>")
        continue
    # 普通段落
    body.append(f"<p>{inline(s)}</p>")
    i += 1

style = """
body{font-family:'PingFang SC','Heiti SC','STHeiti',sans-serif;font-size:12pt;line-height:1.6;color:#222;max-width:820px;margin:24px auto;padding:0 16px;}
h1{font-size:22pt;border-bottom:3px solid #2b6cb0;padding-bottom:8px;color:#1a365d;}
h2{font-size:17pt;border-bottom:1px solid #cbd5e0;padding-bottom:4px;color:#2b6cb0;margin-top:26px;}
h3{font-size:14pt;color:#2c5282;margin-top:18px;}
h4{font-size:12pt;color:#2c5282;}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:11pt;}
th,td{border:1px solid #cbd5e0;padding:6px 8px;text-align:left;vertical-align:top;}
th{background:#ebf4ff;}
code{background:#f1f5f9;padding:1px 5px;border-radius:3px;font-family:Menlo,Consolas,monospace;font-size:10.5pt;color:#c026d3;}
blockquote{border-left:4px solid #f6ad55;background:#fffaf0;margin:10px 0;padding:8px 14px;color:#744210;}
hr{border:0;border-top:1px solid #e2e8f0;margin:18px 0;}
ul,ol{margin:8px 0;padding-left:26px;}
li{margin:3px 0;}
strong{color:#1a365d;}
@media print{body{margin:0;}h2{page-break-after:avoid;}table,blockquote{page-break-inside:avoid;}}
"""
htmlout = f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'><style>{style}</style></head><body>" + "\n".join(body) + "</body></html>"
open(out, "w", encoding="utf-8").write(htmlout)
print("HTML 生成:", out)
