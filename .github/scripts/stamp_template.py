"""Stamp the Unraid template with the publish date and recent changes before it goes to Community Apps.

usage: stamp_template.py <template.xml> <CHANGELOG.md> <date YYYY-MM-DD> <build number> [sections]
Writes the template in place: <Date> = publish date, <Changes> = "Build N, published DATE" followed by
the newest changelog sections (### headings with bullet lists, the format CA displays).
"""
import re
import sys
from xml.sax.saxutils import escape


def recent_changes(changelog, count):
    text = open(changelog, encoding="utf-8").read()
    sections = re.split(r"(?m)^(?=### )", text)
    sections = [s.strip() for s in sections if s.startswith("### ")]
    return "\n\n".join(sections[:count])


def main():
    xml_path, changelog, date, build = sys.argv[1:5]
    count = int(sys.argv[5]) if len(sys.argv) > 5 else 4
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        sys.exit(f"bad date: {date}")
    xml = open(xml_path, encoding="utf-8").read()
    body = f"Build {build}, published {date}.\n\n{recent_changes(changelog, count)}"
    changes = "<Changes>\n" + escape(body) + "\n</Changes>"
    xml = re.sub(r"<Date>.*?</Date>", f"<Date>{date}</Date>", xml, flags=re.S)
    if re.search(r"<Changes>.*?</Changes>|<Changes/>", xml, flags=re.S):
        xml = re.sub(r"<Changes>.*?</Changes>|<Changes/>", lambda _m: changes, xml, count=1, flags=re.S)
    else:
        xml = re.sub(r"(\n\s*)<Date>", lambda m: f"{m.group(1)}{changes}{m.group(1)}<Date>", xml, count=1)
    open(xml_path, "w", encoding="utf-8").write(xml)


if __name__ == "__main__":
    main()
