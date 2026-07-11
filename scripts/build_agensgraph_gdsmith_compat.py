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

import yaml


CONNECTION_RELATIVE_PATH = Path("src/main/java/org/example/gdsmith/agensGraph/AgensGraphConnection.java")
MAIN_RELATIVE_PATH = Path("src/main/java/org/example/gdsmith/Main.java")
POM_RELATIVE_PATH = Path("pom.xml")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="/tmp/gdsmith-src", help="GDsmith source checkout")
    parser.add_argument("--out", default="/tmp/GDsmith-agensgraph-compat.jar", help="output jar path")
    parser.add_argument(
        "--work-dir",
        default="/tmp/graphsymperf-gdsmith-agensgraph-compat-src",
        help="temporary patched source directory",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "config" / "experiment.yaml"),
        help="formal experiment config containing generation.expected_commit",
    )
    args = parser.parse_args()

    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    if not (src / "pom.xml").is_file():
        raise SystemExit(f"GDsmith source does not contain pom.xml: {src}")
    expected_commit = _expected_commit(Path(args.config))
    source_commit = _verify_source_revision(src, expected_commit)

    _copy_source(src, work_dir)
    patch_paths = (
        _patch_agens_connection(work_dir),
        _patch_main_provider_registry(work_dir),
        _patch_maven_build(work_dir),
    )
    jar_path = _build_with_maven(work_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(jar_path, out)
    sha256 = _sha256(out)
    out.with_suffix(out.suffix + ".sha256").write_text(f"{sha256}  {out.name}\n", encoding="utf-8")
    out.with_suffix(out.suffix + ".manifest.json").write_text(
        json.dumps(
            {
                "source": str(src),
                "source_commit": source_commit,
                "expected_commit": expected_commit,
                "work_dir": str(work_dir),
                "patched_file": str(work_dir / CONNECTION_RELATIVE_PATH),
                "patches": [str(path) for path in patch_paths],
                "source_jar": str(jar_path),
                "output": str(out),
                "sha256": sha256,
                "compatibility_guards": (
                    "reject_multi_label_node_patterns",
                    "render_safe_double_quoted_literals",
                    "reject_unsafe_double_quoted_tokens",
                    "exclude_unbuildable_tugraph_provider",
                    "shade_runnable_jar",
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"jar": str(out), "sha256": sha256, "patches": [str(path) for path in patch_paths]}, indent=2))
    return 0


def _copy_source(src: Path, work_dir: Path) -> None:
    if work_dir.exists():
        shutil.rmtree(work_dir)
    shutil.copytree(src, work_dir, ignore=shutil.ignore_patterns(".git", "target", "out", "logs", "*.log"))


def _expected_commit(config_path: Path) -> str:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    try:
        value = payload["generation"]["expected_commit"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("formal config has no generation.expected_commit") from exc
    if not isinstance(value, str) or len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError("generation.expected_commit must be full lowercase 40-hex")
    return value


def _verify_source_revision(src: Path, expected_commit: str) -> str:
    commit = subprocess.run(
        ("git", "-C", str(src), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if commit != expected_commit:
        raise RuntimeError(f"GDsmith commit mismatch: expected {expected_commit}, got {commit}")
    dirty = subprocess.run(
        ("git", "-C", str(src), "status", "--porcelain"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError("GDsmith checkout is dirty; compatibility build requires the pinned clean source")
    return commit


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


def _patch_main_provider_registry(work_dir: Path) -> Path:
    path = work_dir / MAIN_RELATIVE_PATH
    original = path.read_text(encoding="utf-8")
    patched = original.replace("import org.example.gdsmith.tugraph.TuGraphProvider;\n", "")
    patched = patched.replace("            providers.add(new TuGraphProvider());\n", "")
    if patched == original or "TuGraphProvider" in patched:
        raise RuntimeError("failed to isolate the unbuildable TuGraph provider")
    path.write_text(patched, encoding="utf-8")
    return _write_patch(work_dir, MAIN_RELATIVE_PATH, original, patched, "provider-registry")


def _patch_maven_build(work_dir: Path) -> Path:
    path = work_dir / POM_RELATIVE_PATH
    original = path.read_text(encoding="utf-8")
    build = """
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.15.0</version>
        <configuration>
          <excludes><exclude>org/example/gdsmith/tugraph/**</exclude></excludes>
        </configuration>
      </plugin>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-shade-plugin</artifactId>
        <version>3.6.1</version>
        <executions><execution><phase>package</phase><goals><goal>shade</goal></goals>
          <configuration><transformers><transformer implementation="org.apache.maven.plugins.shade.resource.ManifestResourceTransformer">
            <mainClass>org.example.gdsmith.Main</mainClass>
          </transformer></transformers></configuration>
        </execution></executions>
      </plugin>
    </plugins>
  </build>
"""
    if "<artifactId>maven-shade-plugin</artifactId>" in original or "</project>" not in original:
        raise RuntimeError("GDsmith pom is not the expected clean upstream build file")
    patched = original.replace("</project>", build + "\n</project>")
    path.write_text(patched, encoding="utf-8")
    return _write_patch(work_dir, POM_RELATIVE_PATH, original, patched, "maven-build")


def _write_patch(
    work_dir: Path,
    relative: Path,
    original: str,
    patched: str,
    label: str,
) -> Path:
    patch = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile=str(relative),
            tofile=str(relative) + ".compat",
        )
    )
    patch_path = work_dir / f"GraphSymPerf-{label}.patch"
    patch_path.write_text(patch, encoding="utf-8")
    return patch_path


def _patch_agens_connection_source(source: str) -> str:
    source = source.replace(
        "import java.sql.Statement;\n",
        "import java.sql.Statement;\nimport java.util.regex.Pattern;\n",
    )
    source = source.replace(
        "public class AgensGraphConnection extends CypherConnection {\n\n    private Connection connection;",
        """public class AgensGraphConnection extends CypherConnection {

    private static final Pattern MULTI_LABEL_NODE = Pattern.compile("(:[A-Za-z_][A-Za-z0-9_]*)(?::[A-Za-z_][A-Za-z0-9_]*)+(?=\\\\s*[\\\\{\\\\)])");
    private static final Pattern SAFE_DOUBLE_QUOTED_LITERAL = Pattern.compile("\\\"([A-Za-z0-9_]*)\\\"");

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
        if (MULTI_LABEL_NODE.matcher(query).find()) {
            throw new IllegalArgumentException("GRAPHSYMPERF_REJECT_MULTI_LABEL");
        }
        String rendered = SAFE_DOUBLE_QUOTED_LITERAL.matcher(query).replaceAll("'$1'");
        if (rendered.indexOf('"') >= 0) {
            throw new IllegalArgumentException("GRAPHSYMPERF_REJECT_UNSAFE_DOUBLE_QUOTED_TOKEN");
        }
        return rendered;
    }""",
    )
    required = (
        "GRAPHSYMPERF_REJECT_MULTI_LABEL",
        "GRAPHSYMPERF_REJECT_UNSAFE_DOUBLE_QUOTED_TOKEN",
        "SAFE_DOUBLE_QUOTED_LITERAL.matcher(query).replaceAll(\"'$1'\")",
        "stmt.execute(agensCompatible(arg))",
        "stmt.executeQuery(agensCompatible(arg))",
    )
    missing = tuple(anchor for anchor in required if anchor not in source)
    if missing:
        raise RuntimeError(f"required compatibility guard patch anchors are missing: {', '.join(missing)}")
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
