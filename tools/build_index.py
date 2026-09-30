#!/usr/bin/env python3
"""Build one ReaPack index.xml from several plugin repositories.

Each repository must contain exactly ONE script in its top-level dist/ folder
(dist/*.lua). That file becomes one ReaPack package. Metadata comes from the
file's own header comment (the same tags reapack-index understands):

    -- @description  Short title
    -- @author       Name
    -- @version      1.2.3
    -- @about
    --   Longer text, indented by two or more spaces after the dashes.

Extras picked up automatically:
  * changelog  - the "## <version>" section of the repo's CHANGELOG.md
  * links      - website (repo page) and screenshot (screenshot.png, if present)
  * time       - date of the last commit that touched the dist file
  * source     - raw.githubusercontent.com URL pinned to that commit
  * history    - versions already present in the existing index are kept

Why not the reapack-index gem? It indexes ONE git repository (it stores a single
"last commit" in the index), ignores files in a repository's root folder and needs
the .git history. That does not fit "merge the dist/ file of four repositories".

Usage:
  build_index.py --org ORG --name INDEX_NAME --repos-dir DIR --output index.xml REPO [REPO ...]
"""
import argparse
import datetime
import glob
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


class BuildError(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def git(repo_dir, *args):
    res = subprocess.run(["git", "-C", repo_dir, *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed in {repo_dir}: {res.stderr.strip()}")
    return res.stdout.strip()


def version_key(name):
    return [int(x) if x.isdigit() else x for x in re.findall(r"\d+|[A-Za-z]+", name)]


def xml_attr(value):
    return (value.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))


def xml_text(value):
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cdata(value):
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def utc_iso(unix_ts):
    return datetime.datetime.fromtimestamp(int(unix_ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- header parsing
def parse_header(path):
    """Return {tag: value} from the leading '-- @tag value' comment block."""
    tags, current = {}, None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line.startswith("--"):
                break
            m = re.match(r"^--\s*@(\w+)[ \t]*(.*)$", line)
            if m:
                current = m.group(1).lower()
                tags[current] = [m.group(2)] if m.group(2) else []
                continue
            cont = re.match(r"^--(?:[ \t]{2,}(.*)|[ \t]*)$", line)   # indented or empty comment line
            if cont and current:
                tags[current].append((cont.group(1) or "").rstrip())
            else:
                current = None          # e.g. "-- BUNDLED BUILD ..." ends the tag block
    return {k: "\n".join(v).strip() for k, v in tags.items()}


def rtf_escape(text):
    out = []
    for ch in text:
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ord(ch) < 128:
            out.append(ch)
        else:                                            # RTF wants signed 16-bit code units
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):
                code = int.from_bytes(data[i:i + 2], "little")
                out.append("\\u%d?" % (code - 65536 if code > 32767 else code))
    return "".join(out)


def about_to_rtf(about):
    """Re-flow hard-wrapped text into paragraphs (list items stay separate) and wrap as RTF."""
    paras, cur = [], []
    for line in about.split("\n"):
        s = line.strip()
        if not s:
            if cur:
                paras.append(" ".join(cur)); cur = []
        elif re.match(r"^(\d+[.)]|[-*])\s", s):
            if cur:
                paras.append(" ".join(cur))
            cur = [s]
        else:
            cur.append(s)
    if cur:
        paras.append(" ".join(cur))
    body = "".join("\\pard " + rtf_escape(p) + "\\par\n" for p in paras)
    return "{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\n" + body + "}"


def changelog_for(repo_dir, version):
    path = os.path.join(repo_dir, "CHANGELOG.md")
    if not os.path.isfile(path):
        return ""
    lines, grab = [], False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^##\s+v?(\S+)", line)
            if m:
                if grab:
                    break
                grab = (m.group(1).rstrip(":—-–") == version)
                continue
            if grab:
                lines.append(line.rstrip())
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- existing index
def load_old_versions(path):
    """{package name: [version dict, ...]} from an existing index; {} if unreadable/empty."""
    old = {}
    if not os.path.isfile(path):
        return old
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        print(f"WARNING: existing {path} is not valid XML ({e}); starting clean", file=sys.stderr)
        return old
    for pkg in root.iter("reapack"):
        vers = []
        for v in pkg.findall("version"):
            vers.append({
                "name": v.get("name", ""),
                "author": v.get("author", ""),
                "time": v.get("time", ""),
                "changelog": (v.findtext("changelog") or "").strip(),
                "sources": [(s.get("main", ""), (s.text or "").strip()) for s in v.findall("source")],
            })
        old[pkg.get("name", "")] = vers
    return old


# --------------------------------------------------------------------------- one repository -> one package
def package_from_repo(org, repo, repos_dir, category):
    repo_dir = os.path.join(repos_dir, repo)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        raise BuildError(f"{repo}: {repo_dir} is not a git clone")

    luas = sorted(glob.glob(os.path.join(repo_dir, "dist", "*.lua")))
    if len(luas) != 1:
        found = [os.path.basename(x) for x in luas] or "nothing"
        raise BuildError(f"{repo}: dist/ must contain exactly one .lua file, found {found}")
    lua = luas[0]
    fname = os.path.basename(lua)
    rel = f"dist/{fname}"

    hdr = parse_header(lua)
    missing = [t for t in ("description", "version") if not hdr.get(t)]
    if missing:
        raise BuildError(f"{repo}: {rel} header lacks @{', @'.join(missing)}")
    version = hdr["version"].split()[0]
    if not re.match(r"^\d", version):
        raise BuildError(f"{repo}: @version must start with a digit, got '{version}'")

    sha, ts = git(repo_dir, "log", "-1", "--format=%H %ct", "--", rel).split()
    head = git(repo_dir, "rev-parse", "HEAD")
    raw = f"https://raw.githubusercontent.com/{org}/{repo}"

    links = [("website", f"https://github.com/{org}/{repo}", repo)]
    if os.path.isfile(os.path.join(repo_dir, "screenshot.png")):
        links.append(("screenshot", f"{raw}/{head}/screenshot.png", "Screenshot"))

    return {
        "category": category,
        "name": fname,
        "desc": " ".join(hdr["description"].split()),
        "rtf": about_to_rtf(hdr["about"]) if hdr.get("about") else "",
        "links": links,
        "version": {
            "name": version,
            "author": hdr.get("author", org) or org,
            "time": utc_iso(ts),
            "changelog": changelog_for(repo_dir, version),
            "sources": [("main", f"{raw}/{sha}/{rel}")],
        },
        "repo": repo,
    }


# --------------------------------------------------------------------------- output
def render(index_name, packages, old_versions):
    out = ['<?xml version="1.0" encoding="utf-8"?>',
           f'<index version="1" name="{xml_attr(index_name)}">']
    categories = sorted({p["category"] for p in packages})
    for cat in categories:
        out.append(f'  <category name="{xml_attr(cat)}">')
        for p in sorted((p for p in packages if p["category"] == cat), key=lambda p: p["name"].lower()):
            cur = p["version"]
            kept = [v for v in old_versions.get(p["name"], []) if v["name"] != cur["name"]]
            versions = sorted(kept + [cur], key=lambda v: version_key(v["name"]))
            out.append(f'    <reapack name="{xml_attr(p["name"])}" type="script" desc="{xml_attr(p["desc"])}">')
            out.append("      <metadata>")
            if p["rtf"]:
                out.append(f'        <description>{cdata(p["rtf"])}</description>')
            for rel, href, label in p["links"]:
                out.append(f'        <link rel="{rel}" href="{xml_attr(href)}">{xml_text(label)}</link>')
            out.append("      </metadata>")
            for v in versions:
                out.append(f'      <version name="{xml_attr(v["name"])}" author="{xml_attr(v["author"])}" time="{v["time"]}">')
                if v["changelog"]:
                    out.append(f'        <changelog>{cdata(v["changelog"])}</changelog>')
                for main, url in v["sources"]:
                    attr = f' main="{xml_attr(main)}"' if main else ""
                    out.append(f"        <source{attr}>{xml_text(url)}</source>")
                out.append("      </version>")
            out.append("    </reapack>")
        out.append("  </category>")
    out.append("</index>")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", required=True, help="GitHub organisation / user that owns the repos")
    ap.add_argument("--name", required=True, help="index name shown in ReaPack")
    ap.add_argument("--repos-dir", required=True, help="folder containing one git clone per repo")
    ap.add_argument("--output", default="index.xml")
    ap.add_argument("--category", default="Scripts", help="ReaPack category for every package")
    ap.add_argument("--fresh", action="store_true", help="ignore versions already in the output file")
    ap.add_argument("repos", nargs="+")
    args = ap.parse_args()

    try:
        packages = [package_from_repo(args.org, r, args.repos_dir, args.category) for r in args.repos]
    except BuildError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    names = [p["name"] for p in packages]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        print(f"ERROR: two repos ship a package with the same file name: {', '.join(dupes)}", file=sys.stderr)
        return 1

    old = {} if args.fresh else load_old_versions(args.output)
    xml = render(args.name, packages, old)
    ET.fromstring(xml)                       # sanity: must be well-formed
    with open(args.output, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)

    for p in packages:
        print(f"  {p['repo']:<20} -> {p['name']} v{p['version']['name']}  ({p['version']['time']})")
    print(f"wrote {args.output}: {len(packages)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
