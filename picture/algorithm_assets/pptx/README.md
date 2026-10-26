# Current editable PowerPoint figures

The current PPTX figures follow the corresponding algorithms in `docs/Approach.md`. The latest revision retains the full algorithm flow and canvas dimensions while standardizing fonts, font-size hierarchy, mathematical notation, card spacing, margins and alignment:

- `picture/Algorithm1.pptx`: Algorithm 1 (Section 3.2); 180 × 111.96 mm; 179 native objects. It illustrates recipe selection, low-cost and high-cost component specifications, prescribed attributes, conditional inverse/reference additions, identifier assignment, and the output `(G,I)`.
- `picture/Algorithm2.pptx`: Algorithm 2 (Section 3.3); 180 × 115.92 mm; 278 native objects. It illustrates candidate filtering, seeded placeholder population, bounded wrapping, binding updates, independent query copying, and the exclusive symmetry/reference-substitution branches.

Algorithm 1 records construction conditions; graph materialization, condition execution and index readiness belong to the later execution stage. Its component diagrams are schematic, with topology and multiplicities determined by the selected recipe. Its data seed selects an identifier offset, not a new topology. The output `(G,I)` is the input to Algorithm 2. The inverse and reference structures are included only when required by the selected pattern.

Algorithm 2 depicts generation and mutation only. P3 illustrates symmetry transformation through a mirrored bounded path. S7 separately illustrates reference substitution in a coalesce fallback subtree. The S7 illustration does not represent the Neo4j #14008 case. The figure ends at the generated test case and transformation record.

Headings and labels use Arial; mathematical variables use Times New Roman italic. Algorithm 2 uses Menlo only for the mapping-arrow and empty-set glyphs. Inline variables use the same mathematical style as standalone formulas; numeric subscripts and the copy operator are upright. Text, nodes, borders and connectors are native editable PowerPoint objects. Both figures use black and white with one red accent and contain no embedded pictures.

## Sources and validation

- `algorithm1_method/`: current Algorithm 1 authoring sources, native-object specification and manifest, paper caption, validation record, and previous PPTX/manuscript backup.
- `algorithm2_method/`: current Algorithm 2 authoring sources, native-object specification and manifest, validation record, and previous PPTX backup.
- `issue14008/`: historical case-specific versions, retained for provenance. These are not the current algorithm-wide figures.
- `previews/`: current PPTX renders, as PNG and PDF.

The current files were rendered through LibreOffice, inspected visually, checked for overflow, and reviewed against their pseudocode. PowerPoint/WPS live rendering was not used. The Algorithm 1 revision was also checked against the graph-specification implementation; `tests/test_state.py` passed all 104 tests in the project virtual environment. The original SVG illustrations remain earlier versions; this editing series updates the PPTX files.

## Rebuild

For either algorithm number `n`:

1. Run `algorithm<n>_method/author_algorithm<n>.cjs`.
2. Run `algorithm<n>_method/build_algorithm<n>.cjs <scratch-output-directory>`.
3. Run `apply_exact_dashes.py <scratch-output-directory> algorithm<n>_method/algorithm<n>_geometry.json`.
4. Render and inspect the scratch PPTX before replacing the corresponding current file and its previews.

JavaScript dependencies: PptxGenJS, skia-canvas, fontkit and linebreak. Shared text calibration and layout helpers are retained here. Rebuilding replaces manual edits to generated output files. The historical `issue14008` builder produces old designs and should not overwrite the current files.

The current filenames are `Algorithm1.pptx` and `Algorithm2.pptx`. Rebuild scripts retain the descriptive `Algorithm_1_graph_state_generation.pptx` and `Algorithm_2_query_pair_construction.pptx` scratch names; copy each validated output to the corresponding current filename. The `detailed_version/` directories preserve the version before label trimming.

`figure_typography.cjs` is the shared typography source for both figures. `before_typography/` preserves the preceding PPTX and sources; `typography_audit.json` records the final style and consistency checks. The manuscript pseudocode remains unchanged.
