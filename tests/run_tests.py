#!/usr/bin/env python3
"""
run_tests.py -- triage-doctor's test suite (standard library only,
no Blender needed: the classifiers are pure functions over measured
metrics, and that is where the decisions live).

Fixtures are shaped like the real tools' output:
  * the FBX fixtures mirror ripdoctor's --json payload for a loose FBX
    (the healthy one uses the measured values of a real 1.6396 m
    character rip that ripdoctor passes);
  * the GLB fixtures mirror the built-in probe's totals (the soup one
    uses the recorded audit of a real generator export: 1,499,788 tris,
    8,053 shells, not watertight).

Run:  python3 tests/run_tests.py
Exit: 0 = all passed, 1 = failures (printed).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import triage_doctor as td  # noqa: E402

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name} {extra}")
        FAILURES.append(name)


OPTS = {"voxel_fraction": 0.0035, "target_tris": 25000,
        "soup_polys": 300000, "blender": "blender",
        "cleanup_py": Path("clean_ai_mesh.py"), "out_dir": None}


def rip_payload(geo_status="PASS", flags=None, meshes=None, bones=145,
                skinning_status="PASS", probe_error=None):
    checks = []
    if probe_error is not None:
        checks.append({"id": "fbx_probe", "title": "FBX probe",
                       "status": "FAIL", "summary": probe_error,
                       "details": {}})
        return {"target": "x.fbx", "kind": "fbx",
                "counts": {"meshes": 0, "verts": 0, "bones": 0},
                "checks": checks}
    meshes = meshes if meshes is not None else [
        {"name": "body", "verts": 15197, "polys": 24000,
         "dims_m": [1.1547, 0.5302, 1.6396], "armature_modifier": True,
         "vertex_groups": 60, "materials": 3}]
    checks.append({
        "id": "fbx_geometry", "title": "geo", "status": geo_status,
        "summary": "summary",
        "details": {"flags": flags or
                    ["scale and geometry within plausible bounds"],
                    "meshes": meshes, "empty_meshes": []}})
    checks.append({
        "id": "fbx_skinning", "title": "skin", "status": skinning_status,
        "summary": "skinning summary", "details": {}})
    return {"target": "x.fbx", "kind": "fbx",
            "counts": {"meshes": len(meshes),
                       "verts": sum(m["verts"] for m in meshes),
                       "bones": bones},
            "checks": checks}


def glb_probe(tris=1499788, verts=891360, shells=8053, watertight=False,
              height=1.143, max_dim=None, boundary=218610):
    dims = [0.9, 0.6, height]
    if max_dim is not None:
        dims = [max_dim, max_dim / 2, max_dim / 2]
    return {"meshes": [{"name": "soup", "verts": verts, "tris": tris,
                        "dims_m": dims, "shells": shells,
                        "watertight": watertight}],
            "scene_dims_m": dims,
            "totals": {"verts": verts, "tris": tris, "shells": shells,
                       "boundary_edges": boundary,
                       "nonmanifold_edges": 0, "zero_area_tris": 0,
                       "watertight": watertight}}


print("== voxel math ==")
v = td.compute_voxel_size(1.143)
check("gown height 1.143 -> voxel 0.004 (the value hand-picked in "
      "ai-mesh-cleanup's own testing)", v == 0.004, f"got {v}")
v = td.compute_voxel_size(1.6396)
check("avatar height 1.6396 -> voxel in the 0.3-0.4% band",
      0.003 * 1.6396 <= v <= 0.004 * 1.6396, f"got {v}")
check("zero height -> zero voxel", td.compute_voxel_size(0) == 0.0)
check("target tris never exceeds raw count",
      td.compute_target_tris(12000, 25000) == 12000)
check("target tris caps at the budget",
      td.compute_target_tris(1499788, 25000) == 25000)

print("== ripdoctor payload parsing ==")
m = td.parse_ripdoctor_fbx(rip_payload())
check("height = tallest mesh Z extent", m["height_m"] == 1.6396,
      f"got {m['height_m']}")
check("max dim parsed", m["max_dim_m"] == 1.6396)
check("bones parsed", m["bones"] == 145)
check("geometry status parsed", m["geometry_status"] == "PASS")

print("== FBX classification ==")
item = td.classify_fbx(td.parse_ripdoctor_fbx(rip_payload()), OPTS,
                       "hero.fbx")
check("healthy skinned rip -> clean", item["bucket"] == "clean",
      item["bucket"])
check("clean item has no cleanup block", item["cleanup"] is None)
check("clean item's check status is PASS",
      item["checks"][0]["status"] == "PASS")

scale_flags = ["scene max dimension is 0.0164 m -- the 0.01-scale "
               "class: geometry is ~100x too small (unit mismatch at "
               "export/import)"]
item = td.classify_fbx(td.parse_ripdoctor_fbx(
    rip_payload(geo_status="FAIL", flags=scale_flags)), OPTS, "tiny.fbx")
check("0.01-scale FAIL -> hopeless", item["bucket"] == "hopeless")
check("hopeless reason names the scale class",
      any("0.01-scale" in r for r in item["reasons"]))

item = td.classify_fbx(td.parse_ripdoctor_fbx(
    rip_payload(probe_error="blender produced no probe output: boom")),
    OPTS, "broken.fbx")
check("probe failure -> hopeless", item["bucket"] == "hopeless")

soup_mesh = [{"name": "soup", "verts": 400000, "polys": 450000,
              "dims_m": [1.2, 0.8, 1.9], "armature_modifier": False,
              "vertex_groups": 0, "materials": 1}]
item = td.classify_fbx(td.parse_ripdoctor_fbx(
    rip_payload(meshes=soup_mesh, bones=0, skinning_status="INFO")),
    OPTS, "soup.fbx")
check("unrigged 450k-poly FBX -> cleanup", item["bucket"] == "cleanup",
      item["bucket"])
if item["cleanup"]:
    vox = item["cleanup"]["voxel_size"]
    check("soup FBX voxel in the 0.3-0.4% band of 1.9 m",
          0.003 * 1.9 <= vox <= 0.004 * 1.9, f"got {vox}")
    cmds = item["cleanup"]["commands"]
    check("soup FBX gets a conversion command first",
          len(cmds) == 2 and "fbx_to_glb.py" in cmds[0])
    check("cleanup command carries the computed voxel",
          f"--voxel-size {vox}" in cmds[1])

print("== GLB classification ==")
item = td.classify_glb(glb_probe(), None, OPTS, "raw.glb")
check("generator soup -> cleanup", item["bucket"] == "cleanup")
if item["cleanup"]:
    vox = item["cleanup"]["voxel_size"]
    check("soup voxel == 0.004 for the recorded 1.143 m height",
          vox == 0.004, f"got {vox}")
    check("soup target tris == 25000 budget",
          item["cleanup"]["target_tris"] == 25000)
    check("cleanup command names clean_ai_mesh.py",
          "clean_ai_mesh.py" in item["cleanup"]["commands"][0])

item = td.classify_glb(glb_probe(tris=12000, verts=6000, shells=1,
                                 watertight=True, boundary=0),
                       None, OPTS, "tidy.glb")
check("small watertight GLB -> clean", item["bucket"] == "clean")

item = td.classify_glb(glb_probe(tris=44996, verts=22472, shells=8,
                                 watertight=False, boundary=0),
                       None, OPTS, "almost.glb")
check("over-budget non-watertight GLB -> cleanup",
      item["bucket"] == "cleanup")
if item["cleanup"]:
    check("target tris capped at budget, not raw count",
          item["cleanup"]["target_tris"] == 25000)

item = td.classify_glb(glb_probe(max_dim=0.02, height=0.01), None,
                       OPTS, "tiny.glb")
check("0.01-scale GLB -> hopeless", item["bucket"] == "hopeless")
check("GLB hopeless reason names the scale class",
      any("0.01-scale" in r for r in item["reasons"]))

item = td.classify_glb(glb_probe(verts=0, tris=0), None, OPTS,
                       "empty.glb")
check("zero-vertex GLB -> hopeless", item["bucket"] == "hopeless")

item = td.classify_glb(None, "blender produced no probe output",
                       OPTS, "corrupt.glb")
check("unreadable GLB -> hopeless", item["bucket"] == "hopeless")

print("== worklist assembly ==")
items = [
    td.classify_fbx(td.parse_ripdoctor_fbx(rip_payload()), OPTS, "a.fbx"),
    td.classify_glb(glb_probe(), None, OPTS, "b.glb"),
    td.classify_glb(None, "nope", OPTS, "c.glb"),
]
wl = td.build_worklist(items, ["folder"])
check("counts tally", wl["counts"] == {"models": 3, "clean": 1,
                                       "cleanup": 1, "hopeless": 1},
      str(wl["counts"]))
check("mixed worklist exit code is 1", td.exit_code_for(wl) == 1)
wl2 = td.build_worklist(items[:1], ["folder"])
check("all-clean worklist exit code is 0", td.exit_code_for(wl2) == 0)
md = td.render_markdown(wl)
check("markdown renders all three buckets",
      "✅ clean" in md and "🧹 cleanup" in md and "🛑 hopeless" in md)
cmds = td.render_commands(wl)
check("commands file only contains the cleanup model",
      "b.glb" in cmds and "a.fbx" not in cmds)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
    sys.exit(1)
print("ALL TESTS PASSED")
