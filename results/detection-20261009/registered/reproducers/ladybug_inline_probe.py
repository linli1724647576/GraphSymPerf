import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent))
import ladybug_native_probe as probe
original=probe.recipe
def recipe(mechanism,scale):
    selected=original(mechanism,scale)
    if mechanism=='path-pruning':
        selected['queries']['b']=selected['controls']['recursive_inline_filter']['query']
        selected['equivalence']='Both use the same three-hop TRAIL. ALL over all path nodes is the root keep condition plus the same node predicate at every recursive hop. Positive and NULL controls preserve duplicate paths.'
        selected['work_obligation']='Push the pure ALL node predicate into the same recursive operator, rather than enumerate paths that already contain the false Hub.'
    return selected
probe.recipe=recipe
raise SystemExit(probe.main())
