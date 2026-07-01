#!/usr/bin/env python3
"""Build a GDsmith jar with AgensGraph syntax compatibility guards."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


CONNECTION_RELATIVE_PATH = Path("src/main/java/org/example/gdsmith/agensGraph/AgensGraphConnection.java")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="/tmp/gdsmith-src", help="GDsmith source checkout")
    parser.add_argument("--out", default="/tmp/GDsmith-agensgraph-compat.jar", help="output jar path")
    parser.add_argument(
        "--work-dir",
        default="/tmp/graphsymperf-gdsmith-agensgraph-compat-src",
        help="temporary patched source directory",
    )
    args = parser.parse_args()

    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    if not (src / "pom.xml").is_file():
        raise SystemExit(f"GDsmith source does not contain pom.xml: {src}")

    _copy_source(src, work_dir)
    patch_path = _patch_agens_connection(work_dir)
    jar_path = _build_with_maven(work_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(jar_path, out)
    sha256 = _sha256(out)
    out.with_suffix(out.suffix + ".sha256").write_text(f"{sha256}  {out.name}\n", encoding="utf-8")
    out.with_suffix(out.suffix + ".manifest.json").write_text(
        json.dumps(
            {
                "source": str(src),
                "work_dir": str(work_dir),
                "patched_file": str(work_dir / CONNECTION_RELATIVE_PATH),
                "patch": str(patch_path),
                "source_jar": str(jar_path),
                "output": str(out),
                "sha256": sha256,
                "compatibility_guards": (
                    "collapse_multi_label_node_patterns",
                    "convert_double_quoted_string_literals_to_single_quoted_literals",
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"jar": str(out), "sha256": sha256, "patch": str(patch_path)}, indent=2))
    return 0


def _copy_source(src: Path, work_dir: Path) -> None:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    shutil.copytree(src, work_dir, ignore=shutil.ignore_patterns(".git", "target", "out", "logs", "*.log"))


def _patch_agens_connection(work_dir: Path) -> Path:
    path = work_dir / CONNECTION_RELATIVE_PATH
    original = path.read_text(encoding="utf-8")
    patched = _patch_agens_connection_source(original)
    if patched == original:
        raise RuntimeError(f"failed to patch {path}")
    path.write_text(patched, encoding="utf-8")
    patch = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=str(CONNECTION_RELATIVE_PATH),
            tofile=str(CONNECTION_RELATIVE_PATH) + ".compat",
        )
    )
    patch_path = work_dir / "GraphSymPerf-agensgraph-compat.patch"
    patch_path.write_text(patch, encoding="utf-8")
    return patch_path


def _patch_agens_connection_source(source: str) -> str:
    source = source.replace(
        "import java.sql.Statement;\n",
        "import java.sql.Statement;\nimport java.util.regex.Matcher;\nimport java.util.regex.Pattern;\n",
    )
    source = source.replace(
        "public class AgensGraphConnection extends CypherConnection {\n\n    private Connection connection;",
        """public class AgensGraphConnection extends CypherConnection {

    private static final Pattern MULTI_LABEL_NODE = Pattern.compile("(:[A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z_][A-Za-z0-9_]*)+(?=\\\\s*[\\\\{\\\\)])");
    private static final Pattern DOUBLE_QUOTED_STRING = Pattern.compile("\\\"((?:\\\\\\\\.|[^\\\"\\\\\\\\])*)\\\"");

    private Connection connection;""",
    )
    source = source.replace(
        """    public void executeStatement(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        stmt.execute(arg);
    }

    @Override
    public List<GDSmithResultSet> executeStatementAndGet(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        return Arrays.asList(new GDSmithResultSet(stmt.executeQuery(arg)));
    }""",
        """    public void executeStatement(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        stmt.execute(agensCompatible(arg));
    }

    @Override
    public List<GDSmithResultSet> executeStatementAndGet(String arg) throws Exception{
        Statement stmt = connection.createStatement();
        return Arrays.asList(new GDSmithResultSet(stmt.executeQuery(agensCompatible(arg))));
    }

    private static String agensCompatible(String query) {
        return singleQuotedStrings(singleLabelNodes(query));
    }

    private static String singleLabelNodes(String query) {
        return MULTI_LABEL_NODE.matcher(query).replaceAll("$1");
    }

    private static String singleQuotedStrings(String query) {
        Matcher matcher = DOUBLE_QUOTED_STRING.matcher(query);
        StringBuffer buffer = new StringBuffer();
        while (matcher.find()) {
            String content = matcher.group(1).replace("'", "\\\\'");
            matcher.appendReplacement(buffer, Matcher.quoteReplacement("'" + content + "'"));
        }
        matcher.appendTail(buffer);
        return buffer.toString();
    }""",
    )
    return source


def _build_with_maven(work_dir: Path) -> Path:
    completed = subprocess.run(("mvn", "-q", "-DskipTests", "package"), cwd=work_dir, capture_output=True, text=True, check=False)
    (work_dir / "maven_stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (work_dir / "maven_stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(f"mvn package failed: {completed.stderr.strip()}")
    jars = [
        path
        for path in (work_dir / "target").glob("*.jar")
        if not path.name.endswith(("-sources.jar", "-javadoc.jar", "-tests.jar"))
    ]
    if not jars:
        raise RuntimeError(f"mvn package produced no jar under {work_dir / 'target'}")
    preferred = sorted(
        jars,
        key=lambda path: ("jar-with-dependencies" in path.name or "all" in path.name.lower(), path.stat().st_size),
        reverse=True,
    )
    return preferred[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
