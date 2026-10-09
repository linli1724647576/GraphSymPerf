import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import age_native_probe as probe
original = probe.recipe
def recipe(mechanism, scale, roots):
    selected = original(mechanism, scale, roots)
    if mechanism == 'property-index':
        selected['queries']['a'] = 'MATCH (n:ProbeNode) WHERE coalesce(n.id,null)=1 RETURN n.uid AS uid'
        selected['equivalence'] = 'COALESCE(property,NULL) preserves the property value. The state uses integer id properties and both queries select id=1.'
        selected['work_obligation'] = 'Simplify the redundant COALESCE wrapper so the existing property index can be used, as the same property-map predicate does.'
        selected['novelty'] = 'Separate no-op wrapper probe; do not assume it is the same root cause as the previously reported direct-WHERE issue.'
    return selected
probe.recipe = recipe
raise SystemExit(probe.main())
