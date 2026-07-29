# `string.regexReplace()` with an empty regex corrupts supplementary Unicode characters

I found that `string.regexReplace()` corrupts a supplementary Unicode
character when the regular expression is empty. A single emoji is split into
two replacement characters, although other Cypher string functions treat it
as one Unicode character.

Neo4j Version: 2026.06.0 Community
Operating System: macOS 27.0 arm64
Installation Method: Docker image `neo4j:2026.06.0`
API/Driver: Cypher 25 / Neo4j Python Driver 5.28.4

### Discovery provenance

GraphSymPerf found this with the experimental SYM rule
`regex_zero_width_unicode`: for a one-code-point input and a literal replacement
without group references, empty-regex replacement must preserve the unmatched
Unicode character. The framework compared complete typed results and replayed
the mismatch six times in alternating A/B order before this standalone
reproducer was written.

### Steps to reproduce

No graph data is required. Run:

```cypher
CYPHER 25
RETURN string.regexReplace('🙂', '', '-') AS actual,
       '-' || '🙂' || '-' AS expected,
       size(string.regexReplace('🙂', '', '-')) AS actual_size,
       size('🙂') + 2 AS expected_size;
```

The attached `reproducer.py` uses parameters and runs the query six times in
alternating input order. It also checks ASCII, BMP Unicode, and a consuming
capturing-group control.

### Expected behavior

The empty regular expression has zero-width matches around the single Unicode
character, so the result should preserve that character:

```text
actual = "-🙂-"
actual_size = 3
```

More generally, a zero-width match must not split or replace the unmatched
input character. Neo4j's string-function documentation states that string
functions operate on Unicode characters, and `size('🙂')` correctly returns
`1`.

### Actual behavior

Neo4j returns:

```text
actual = "-?-?-"
actual_size = 5
```

This is consistent with the supplementary character being split into its two
UTF-16 surrogate code units before zero-width replacement. The ASCII input
`'a'` and BMP input `'汉'` both return the expected three-character result,
and `string.regexReplace('🙂', '(.)', '$1')` preserves the emoji.

The result was reproduced in two fresh Neo4j 2026.06.0 Docker processes, with
six alternating executions per process. GitHub issue searches for
`string.regexReplace` with Unicode, surrogate, emoji, empty-regex, and
zero-width terms found no existing report.

Java's `String.replaceAll("", "-")` exhibits the same surrogate-splitting
behavior. That supports the implementation hypothesis but is also the main
contract caveat: this report is valid only if Cypher's documented Unicode
character semantics apply to zero-width regular-expression matches instead of
inheriting Java's UTF-16 code-unit behavior. Maintainer confirmation is needed
before treating this candidate as submit-ready.
