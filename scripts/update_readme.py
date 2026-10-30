"""Regenerate the ValScope-style issue inventory from bugs.csv."""
from collections import Counter
import csv
from pathlib import Path
import re


def main():
    root = Path(__file__).resolve().parents[1]
    with (root / "bugs.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    statuses = Counter(row["status"] for row in rows)
    checked = sorted({row["last_checked"] for row in rows})
    tables = []
    systems = list(dict.fromkeys(["Neo4j", "Memgraph", "ArcadeDB", "LadybugDB", "FalkorDB", "Apache AGE", "JanusGraph"] + [row["dbms"] for row in rows]))
    for system in systems:
        entries = [row for row in rows if row["dbms"] == system]
        if not entries:
            continue
        tables += [f"## {system}", "", "| ID   | Issue ID | Status |", "| ---- | -------- | ------ |"]
        tables += [f"| {i} | [{row['issue_number']}]({row['issue_url']}) | {row['status']} |"
                   for i, row in enumerate(entries, 1)]
        tables.append("")
    counts = ", ".join(f"{statuses[name]} {name}" for name in ["fixed", "confirmed", "pending", "uninterested", "not_a_bug"])
    for filename, summary in [
        ("README.md", f"The submitted-issue ledger contains {len(rows)} issue URLs: {counts}. Statuses are the recorded snapshot ({', '.join(checked)}), not a new upstream audit. Historical reports and merged issues are included; these totals are not unique root causes or bugs discovered by the current rewrite."),
        ("README.zh-CN.md", f"The submitted-issue ledger contains {len(rows)} issue URLs: {counts}. Status snapshot: {', '.join(checked)}. Historical and merged reports are included; counts do not represent unique root causes."),
    ]:
        path = root / filename
        text = path.read_text()
        for name, content in [("ISSUE SUMMARY", summary), ("ISSUE TABLES", "\n".join(tables).rstrip())]:
            pattern = rf"(<!-- BEGIN {name} -->).*?(<!-- END {name} -->)"
            # Only generated regions change; running instructions remain hand-maintained.
            # 仅更新自动生成区域，运行说明仍由维护者编辑。
            text, count = re.subn(pattern, lambda match: match[1] + "\n" + content + "\n" + match[2], text, flags=re.S)
            if count != 1:
                raise ValueError(f"Expected one {name} region in {filename}")
        path.write_text(text)


if __name__ == "__main__":
    main()
