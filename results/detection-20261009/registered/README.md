# Selected local issue evidence

Generated only from the verified issue inventory. These records annotate local reproductions; they are neither submitted issues nor developer confirmations. This directory's bugs.csv is the selected annotation export; the repository owner can synchronize it to the root bugs.csv.

Standard records retain their original input_hash and case metadata. Full samples, graph files and plans remain in the original evidence paths. These compact files cannot be passed to main.py --replay.

Native records use format_version native-evidence-summary-v1 and kind native_optimization_rule. Their pattern is native:<mechanism>, their result status is timing_observation, and their annotations are reproduced after manual plan/work review. No query IR or built-in Pattern hit is invented. Reproduce them using the archived scripts under reproducers/, a dedicated database configuration, and the commands below. The AGE COALESCE property-index case requires age_coalesce_index.py, and the Ladybug inline-path control requires ladybug_inline_probe.py; each wrapper imports its matching base script. Base scripts locate this repository's Src directory through __file__ ancestors. Only that path resolution was changed; queries, data and measurement defaults remain unchanged. reproducers/manifest.json records original_sha256 and archived_sha256 for each file.

The --execute option rebuilds the explicitly dedicated test target. Omitting it gives a dry run; these examples do not use main.py --replay. Set any required password environment variables from the dedicated configuration before running one target at a time.

```bash
python results/detection-20261009/registered/reproducers/age_native_probe.py --config /absolute/dedicated/age.yaml --mechanism correlated-count --scale 100000 --roots 16 --execute
python results/detection-20261009/registered/reproducers/age_coalesce_index.py --config /absolute/dedicated/age.yaml --mechanism property-index --scale 2000000 --execute
python results/detection-20261009/registered/reproducers/gremlin_native_probe.py --config /absolute/dedicated/hugegraph.yaml --mechanism count-nonnull --scale 100000 --execute
python results/detection-20261009/registered/reproducers/gremlin_native_probe.py --config /absolute/dedicated/hugegraph.yaml --mechanism filter-wrapper --scale 30000 --execute
python results/detection-20261009/registered/reproducers/gremlin_native_probe.py --config /absolute/dedicated/janusgraph.yaml --mechanism filter-wrapper --scale 10000 --execute
python results/detection-20261009/registered/reproducers/gremlin_native_probe.py --config /absolute/dedicated/janusgraph.yaml --mechanism unique-singleton --scale 10000 --execute
python results/detection-20261009/registered/reproducers/ladybug_native_probe.py --config /absolute/dedicated/ladybug.yaml --mechanism coalesce-count --scale 1500 --execute
python results/detection-20261009/registered/reproducers/ladybug_inline_probe.py --config /absolute/dedicated/ladybug.yaml --mechanism path-pruning --scale 1000 --execute
```

This selected directory has no experiment-budget manifests. Its summary must not be used to infer a discovery rate, full Pattern coverage or bugs per hour.
