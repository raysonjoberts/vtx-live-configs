#python - << "PY"
import os
from datetime import datetime

inp = r"C:\BTDM_7.1\bin\ui\inferred_decision_engine.csv"
out = r"C:\Users\Jayson.Roberts\State of Maine\DAFS-MaineIT CCOE - Data Dog\ProgramManagement\inferred_decision_engine.xlsx"

# If you want to resolve VTX_ROOT exactly like orchestrator does:
VTX_ROOT = os.environ.get("VTX_ROOT")
if VTX_ROOT and inp.startswith("VTX_ROOT\\"):
    inp = os.path.join(VTX_ROOT, inp.replace("VTX_ROOT\\","",1))

def mt(p):
    st = os.stat(p)
    return float(st.st_mtime)

i = mt(inp)
o = mt(out)
print("INPUT :", inp)
print("OUTPUT:", out)
print("input_mtime :", i, datetime.fromtimestamp(i))
print("output_mtime:", o, datetime.fromtimestamp(o))
print("diff_seconds:", i - o)
print("orchestrator_stale?:", i > o)
#PY
