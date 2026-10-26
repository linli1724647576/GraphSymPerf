# Algorithm figures

The two editable SVGs illustrate Algorithms 1 and 2 in `docs/Approach.md`. The design uses compact panels, aligned comparisons, and local changes. Black and white encode structure; red highlights an instantiated or rewritten fragment. It does not encode execution cost. Node labels, transformation labels, and line styles preserve the meaning without color.

## Algorithm 1: graph state generation

[Editable SVG](Algorithm_1_graph_state_generation.svg) · [PNG preview](algorithm_assets/Algorithm_1_graph_state_generation.png)

**Caption.** Graph state generation for S7. (a) The skeleton requires a non-null scalar placeholder \(L\) and a graph-dependent expression \(E_H\). (b) At scale \(N=2\), the construction recipe specifies a unique root with eight `HIGH` targets and three `REF` targets; every target satisfies `mark=true`. Stacked circles abbreviate target bundles, and each multiplicity denotes the total number of targets and corresponding outgoing relationships from the shared root. (c) The graph specification \(G\) and construction conditions \(I\) are passed to Algorithm 2. Materialization, index preparation, and verification of \(I\) occur during execution.

The graph is a construction specification at this stage. The figure deliberately presents each target bundle as a compact adjacency motif, rather than drawing every relationship separately. \(L\) remains a scalar placeholder and is not a node in \(G\).

Suggested placement: immediately after Algorithm 1 in Section 3.2.

## Algorithm 2: skeleton population and query mutation

[Editable SVG](Algorithm_2_query_pair_construction.svg) · [PNG preview](algorithm_assets/Algorithm_2_query_pair_construction.png)

**Caption.** Skeleton population and query-pair construction. Columns show the skeleton, the base query \(Q_1\), and the mutated query \(Q_2\). Dashed nodes denote placeholders, and \(\beta\) records the selected substitutions. In the upper row, the S7 instance uses \((G,I)\) from Algorithm 1. Population instantiates \(L\) as `0` and \(E_H\) as a correlated count. Reference substitution changes the designated relationship type from `HIGH` to `REF` in a copy of \(Q_1\); the non-null prefix preserves the single-row result `value=0`. The lower row is an independent P1 instance with its own skeleton and graph. Its symmetry transformation mirrors an undirected matching pattern while retaining endpoint constraints and projection order. Each row therefore generates its own query pair.

The expression diagrams show only the relevant local structure. The `COUNT {…}` boxes are abbreviated subquery fragments, with `:HIGH` or `:REF` identifying the designated relationship-type reference. They are not complete Cypher listings or full syntax trees. In the S7 example, the omitted context is:

```cypher
MATCH (u:Root {id:1})
RETURN coalesce(NULL, 0,
  COUNT { MATCH (u)-[:HIGH]->(v) WHERE v.mark = true }) AS value
```

The reference query substitutes only `REF` for `HIGH`. If evaluated, the counts are eight and three, respectively; the complete queries both return zero. This is a construction-derived result, not a timing measurement. The illustrated query choices correspond to \(k=0\) and \(d=0\). General candidate filtering, equivalent wrapping, and applicability checks remain specified by Algorithm 2.

Suggested placement: immediately after Algorithm 2 in Section 3.3; the figure can replace the standalone S7 example listing to avoid duplication.

## Terminology and notation

| Figure term | Meaning in the method |
|---|---|
| Graph state generation | Construction of the graph specification and conditions \((G,I)\). |
| Skeleton population | Selection of well-formed, typed, in-scope substitutions for \(S\). |
| \(\beta\) | The substitution map recorded during population; it is not an operation being instantiated. |
| Base query / mutated query | \(Q_1\) and the variant \(Q_2\) derived from an independent copy. |
| Reference substitution | Replacement of designated \(H\)-references by \(R\)-references. |
| Symmetry transformation | A guarded, result-preserving change of query representation. |

Names are schematic: `Root/HIGH/REF` correspond to `ScorRoot/GSP_SC_HIGH/GSP_SC_REF`; `Low/High/T` correspond to `GspLow/GspHigh/GSP_LINK`. Target labels are omitted where redundant on the constructed graph.

## Publication guidance and visual references

Publication guidance is distinct from the layout conventions observed in research papers. The reviewed sources do not establish a graph-database-specific universal font size, line width, or diagram template.

- **Caption and description placement.** [ACM SIGACCESS, *Describing Figures*](https://www.sigaccess.org/welcome-to-sigaccess/resources/describing-figures/) recommends terminology consistent with the main text, concise descriptions, and captions outside the image. The SVGs therefore contain short panel labels and accessible descriptions; extended captions remain in this document.
- **Color-independent interpretation.** [ACM DIS 2023, *Creating Accessible Figures and Tables*](https://dis.acm.org/2023/creating-accessible-figures-and-tables/) recommends redundant visual encoding and adequate contrast. Red emphasis is paired with explicit reference names, endpoint labels, arrows, or underlining. This is adopted as accessibility guidance, not asserted as an ISSTA/PACMMOD-specific formatting rule.

| Reference | Figure inspected | Convention adopted |
|---|---|---|
| Jinhui Lai et al. (2026), *Finding Missed Optimizations in DBMSs through Unbalanced Short-Circuit Query Construction* | SCor Fig. 2, PDF p. 5; Fig. 3, PDF p. 9 | Compact stage panels, comparable expression structures, and local transformation emphasis. |
| Shuang Liu et al. (2024), *Testing Graph Database Systems with Graph-State Persistence Oracle* | GraspDB Figs. 3–4, PDF p. 4 | Stable graph-object labels, aligned comparisons, and explicit generation-to-execution connections. |
| Ziyue Hua et al. (2023), *GDsmith: Detecting Bugs in Cypher Graph Database Engines* | [Author PDF](https://taoxiease.github.io/publications/issta23-gdsmith.pdf), Fig. 2, PDF p. 6 | Concrete correspondence between graph objects, placeholders, and the generated query. |

SCor and GraspDB were inspected from the supplied PDFs in `reference/`. The figures were independently drawn for GraphSymPerf; no reference artwork is embedded.

## Source correspondence and validation

The examples are grounded in `src/state.py` (`GraphSpec._groups`, `GraphSpec.indexes`), `src/generator.py` (`_build_scor`, `_populate_variants`, `instantiate`), and `src/mutator.py` (`mutate`). The previously checked generated pairs are retained in `algorithm_assets/example_validation.json`.

The authoring width is 180 mm. Labels use Arial, mathematical symbols use Times New Roman, and query fragments use Courier New. The smallest non-subscript label is approximately 7.4 pt at that width. These are project layout choices, not claimed venue requirements. Native text, vector shapes, SVG accessibility metadata, text bounds, and final-size readability are checked. Final submission-PDF font embedding is a separate typesetting check.

The editable SVGs are the primary artifacts. `algorithm_assets/build_algorithm_figures.py` regenerates them; `algorithm_assets/render_algorithm_figures.cjs` renders PNG previews with Playwright and an isolated Chromium session. Regeneration overwrites manual SVG edits. `algorithm_assets/render_validation.json` records the rendered text bounds.
