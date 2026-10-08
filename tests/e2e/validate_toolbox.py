"""Load the toolbox the way Pro does (import the module), then for every tool build the
parameters, run updateParameters and updateMessages on the defaults, and check the basics Pro
validates at load time: unique parameter names, value lists containing the default, dependencies
pointing at existing parameters, licensed, labels unique."""
import importlib.machinery
import importlib.util
import os
import sys
import time

import arcpy

PYT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "..", "LidarTerrainToolbox.pyt"))
t0 = time.time()
loader = importlib.machinery.SourceFileLoader("ltt", PYT)
spec = importlib.util.spec_from_loader("ltt", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
tb = mod.Toolbox()
print("toolbox '%s' alias '%s', %d tools, loaded in %.1f s" % (tb.label, tb.alias, len(tb.tools), time.time() - t0))
problems = []
labels = set()
for cls in tb.tools:
    tool = cls()
    if tool.label in labels:
        problems.append("duplicate label " + tool.label)
    labels.add(tool.label)
    try:
        params = tool.getParameterInfo()
    except Exception as exc:
        problems.append("%s: getParameterInfo raised %s" % (tool.label, exc)); continue
    names = [p.name for p in params]
    if len(set(names)) != len(names):
        problems.append("%s: duplicate parameter names %s" % (tool.label, names))
    for p in params:
        if p.filter and p.filter.type == "ValueList" and p.filter.list and p.value is not None and not p.multiValue:
            if str(p.value) not in [str(v) for v in p.filter.list]:
                problems.append("%s: default %r of %s not in its value list" % (tool.label, p.value, p.name))
        for dep in (p.parameterDependencies or []):
            if dep not in names:
                problems.append("%s: %s depends on unknown parameter %s" % (tool.label, p.name, dep))
    try:
        if hasattr(tool, "updateParameters"):
            tool.updateParameters(params)
        if hasattr(tool, "updateMessages"):
            tool.updateMessages(params)
    except Exception as exc:
        problems.append("%s: updateParameters/updateMessages raised %r" % (tool.label, exc))
    try:
        lic = tool.isLicensed()
    except Exception as exc:
        lic = "raised %r" % exc
    cats = sorted(set(p.category for p in params if p.category))
    print("  %-46s %2d params, licensed %s, categories %s" % (tool.label, len(params), lic, cats or "-"))
print("PROBLEMS:" if problems else "no problems found")
for pr in problems:
    print("  -", pr)
sys.exit(1 if problems else 0)
