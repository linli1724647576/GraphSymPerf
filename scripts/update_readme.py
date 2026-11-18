"""Update README issue tables and the report index from bugs.csv."""
from collections import Counter
import csv
from pathlib import Path
import re


def main():
    root = Path(__file__).resolve().parents[1]
    with (root / "bugs.csv").open(newline="") as stream:
        registry = list(csv.DictReader(stream))
    rows = [row for row in registry if row["status"] in {"pending", "confirmed", "fixed"}]
    statuses = Counter(row["status"] for row in rows)
    checked = sorted({row["last_checked"] for row in rows})
    systems = list(dict.fromkeys(["Neo4j", "Memgraph", "ArcadeDB", "LadybugDB", "FalkorDB", "Apache AGE", "JanusGraph"] + [row["dbms"] for row in rows]))
    for filename, labels, header, summary in [
        ("README.md", {"fixed": "Fixed", "confirmed": "Confirmed", "pending": "Pending"},
         "| ID | Issue ID | Status |",
         f"The tables below list {len(rows)} submitted reports: {statuses['fixed']} fixed, {statuses['confirmed']} confirmed, and {statuses['pending']} pending. Status review dates: {', '.join(checked)}. See the [report index](docs/bug-reports.md) for the complete registry, status evidence, and reproduction reports."),
        ("README.zh-CN.md", {"fixed": "已修复", "confirmed": "已确认", "pending": "待确认"},
         "| 序号 | 问题编号 | 状态 |",
         f"下表列出 {len(rows)} 份已提交报告：{statuses['fixed']} 份已修复，{statuses['confirmed']} 份已确认，{statuses['pending']} 份待确认。状态核查日期：{'、'.join(checked)}。完整台账、状态依据和复现报告见[报告索引](docs/bug-reports.md)。"),
    ]:
        tables = []
        for system in systems:
            entries = [row for row in rows if row["dbms"] == system]
            if not entries:
                continue
            tables += [f"## {system}", "", header, "| --- | --- | --- |"]
            tables += [f"| {i} | [{row['issue_number']}]({row['issue_url']}) | {labels[row['status']]} |"
                       for i, row in enumerate(entries, 1)]
            tables.append("")
        path = root / filename
        text = path.read_text()
        for name, content in [("ISSUE SUMMARY", summary), ("ISSUE TABLES", "\n".join(tables).rstrip())]:
            pattern = rf"(<!-- BEGIN {name} -->).*?(<!-- END {name} -->)"
            # Keep the hand-maintained introduction and running instructions intact.
            text, count = re.subn(pattern, lambda match: match[1] + "\n" + content + "\n" + match[2], text, flags=re.S)
            if count != 1:
                raise ValueError(f"Expected one {name} region in {filename}")
        path.write_text(text)

    tables = []
    for system in dict.fromkeys(row["dbms"] for row in registry):
        tables += [f"## {system}", "", "| Report | Status | Patterns | Local report |", "| --- | --- | --- | --- |"]
        for row in registry:
            if row["dbms"] != system:
                continue
            title = row["title"].replace("|", "\\|")
            report = f"[{row['issue_number']}]({row['issue_url']}) · {title}" if row["issue_url"] else title
            patterns = row["pattern_ids"].replace(";", ", ") or "—"
            local = f"[Reproducer](../{row['local_report']})" if row["local_report"] else "—"
            tables.append(f"| {report} | `{row['status']}` | {patterns} | {local} |")
        tables.append("")
    path = root / "docs" / "bug-reports.md"
    text, count = re.subn(
        r"(<!-- BEGIN REPORT INDEX -->).*?(<!-- END REPORT INDEX -->)",
        lambda match: match[1] + "\n" + "\n".join(tables).rstrip() + "\n" + match[2],
        path.read_text(), flags=re.S,
    )
    if count != 1:
        raise ValueError("Expected one REPORT INDEX region in docs/bug-reports.md")
    path.write_text(text)


if __name__ == "__main__":
    main()
