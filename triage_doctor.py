#!/usr/bin/env python3
"""
triage_doctor.py -- sort a folder of models into a worklist using
Rip Doctor's diagnosis, and compute ai-mesh-cleanup settings for the
ones that need them.

The bridge between two tools in the 1915rising doctor family:

  * ripdoctor (https://github.com/1915rising/ripdoctor) already
    diagnoses FBX rips and emits the result as JSON: per-mesh vertex /
    polygon counts, world-space bounding boxes, bones, and a scale
    plausibility verdict (PASS / WARN / FAIL).
  * ai-mesh-cleanup (https://github.com/1915rising/ai-mesh-cleanup)
    turns raw generator soup (GLB) into a watertight mesh, but its one
    hard-to-pick parameter -- voxel size -- follows a documented rule
    of thumb: roughly 0.3-0.4% of the model's height. That height is
    exactly what ripdoctor measures.

triage-doctor reads the diagnosis, measures GLBs itself with the same
headless-Blender probe technique (audit math mirrors clean_ai_mesh.py
so the numbers line up), and sorts every model into one of three
buckets:

  clean    -- nothing to do (healthy rip, or a small watertight GLB)
  cleanup  -- run ai-mesh-cleanup with these computed settings
              (voxel size from measured height, target tri count)
  hopeless -- cleanup cannot help yet; the reason says what to fix
              first (the 0.01-scale unit class, an unreadable file,
              an empty mesh...)

It prints a Markdown worklist, can write the same worklist as JSON
(same report conventions as ripdoctor: target / kind / counts /
checks, so pipeline tooling can chain it), can write the cleanup
commands to a shell file (--commands), and can run them (--run).

Usage:
    triage_doctor.py <file_or_dir> [more targets...] [options]

Options:
    --json PATH       write the worklist JSON here
    --md PATH         write the worklist Markdown here
    --commands PATH   write a shell file with the cleanup commands
    --run             actually run cleanup for the cleanup bucket
    --out-dir DIR     where --run writes cleaned GLBs
                      (default: next to each source file)
    --recursive       descend into subfolders when scanning dirs
    --ripdoctor PATH  path to ripdoctor.py (default: $TRIAGE_RIPDOCTOR,
                      ../ripdoctor/ripdoctor.py, ./ripdoctor.py)
    --cleanup PATH    path to clean_ai_mesh.py (default:
                      $TRIAGE_CLEANUP, ../ai-mesh-cleanup/clean_ai_mesh.py)
    --blender PATH    blender executable (default: blender on PATH)
    --voxel-fraction F  voxel size as a fraction of model height
                      (default 0.0035 -- the middle of ai-mesh-cleanup's
                      documented 0.3-0.4% band)
    --target-tris N   triangle budget for cleanup (default 25000,
                      ai-mesh-cleanup's own default)
    --soup-polys N    an unrigged FBX with at least this many polygons
                      counts as generator soup (default 300000)

Exit codes: 0 = every model is clean, 1 = the worklist has cleanup or
hopeless items, 2 = usage error.

Requirements: Python 3.8+ (standard library only) for the driver;
Blender on PATH (or --blender) for the probes; ripdoctor for FBX
inputs; ai-mesh-cleanup only for the emitted commands / --run.

Copyright (c) 2026 1915rising -- MIT License (see LICENSE).
"""

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "1.0.0"

VOXEL_FRACTION_DEFAULT = 0.0035
VOXEL_BAND = (0.003, 0.004)          # ai-mesh-cleanup's documented rule
TARGET_TRIS_DEFAULT = 25000          # ai-mesh-cleanup's own default
SOUP_POLYS_DEFAULT = 300000          # unrigged FBX soup heuristic
SCALE_FAIL_MAX_DIM = 0.05            # ripdoctor's 0.01-scale class bound
CM_WARN_MAX_DIM = 50.0               # ripdoctor's centimeter class bound
MODEL_SUFFIXES = {".fbx", ".glb", ".gltf"}

BUCKET_STATUS = {"clean": "PASS", "cleanup": "WARN", "hopeless": "FAIL"}
ICON = {"clean": "✅ clean", "cleanup": "🧹 cleanup", "hopeless": "🛑 hopeless"}


# ---------------------------------------------------------------------------
# voxel math -- the one number this bridge exists to compute
# ---------------------------------------------------------------------------

def compute_voxel_size(height_m, fraction=VOXEL_FRACTION_DEFAULT):
    """Voxel size for ai-mesh-cleanup: `fraction` of the model's height,
    rounded to 4 decimals (the precision cleanup's own examples use)."""
    if height_m <= 0:
        return 0.0
    return max(0.0001, round(fraction * height_m, 4))


def compute_target_tris(raw_tris, target_arg=TARGET_TRIS_DEFAULT):
    """Triangle budget: the caller's target, but never above the raw
    count -- asking decimate for more triangles than exist is a no-op
    that only confuses the report."""
    if raw_tris and raw_tris > 0:
        return min(target_arg, raw_tris)
    return target_arg


# ---------------------------------------------------------------------------
# GLB probe (Blender headless) -- audit math mirrors clean_ai_mesh.py
# (shell counting by union-find over unique edges, boundary / non-manifold
# edge counts, zero-area triangles) so triage's "before" numbers are the
# same numbers ai-mesh-cleanup will report when it runs.
# ---------------------------------------------------------------------------

GLB_PROBE = r"""
import bpy, json, sys
import numpy as np
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
path = argv[0]

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=path)


def count_shells(ukeys, tris, nv):
    parent = np.arange(nv, dtype=np.int64)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    eu = np.stack([ukeys // nv, ukeys % nv], axis=1)
    for i in range(len(eu)):
        ra, rb = find(int(eu[i, 0])), find(int(eu[i, 1]))
        if ra != rb:
            parent[ra] = rb
    used = np.unique(tris)
    roots = np.array([find(int(v)) for v in used])
    return int(len(np.unique(roots)))


def audit(me):
    me.calc_loop_triangles()
    nv = len(me.vertices)
    ntris = len(me.loop_triangles)
    out = {"verts": nv, "tris": ntris, "boundary_edges": 0,
           "nonmanifold_edges": 0, "zero_area_tris": 0, "shells": 0,
           "watertight": nv > 0 and ntris > 0}
    if nv == 0 or ntris == 0:
        out["watertight"] = False
        return out
    tris = np.empty(ntris * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tris)
    tris = tris.reshape(-1, 3)
    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    e.sort(axis=1)
    keys = e[:, 0] * nv + e[:, 1]
    ukeys, counts = np.unique(keys, return_counts=True)
    boundary = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())
    verts = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    a = verts[tris[:, 1]] - verts[tris[:, 0]]
    b = verts[tris[:, 2]] - verts[tris[:, 0]]
    cr = np.cross(a, b)
    area2 = np.sqrt((cr ** 2).sum(axis=1))
    out.update({"boundary_edges": boundary,
                "nonmanifold_edges": nonmanifold,
                "zero_area_tris": int((area2 < 1e-12).sum()),
                "shells": count_shells(ukeys, tris, nv),
                "watertight": bool(boundary == 0 and nonmanifold == 0)})
    return out


meshes = []
all_corners = []
tot = {"verts": 0, "tris": 0, "boundary_edges": 0, "nonmanifold_edges": 0,
       "zero_area_tris": 0, "shells": 0}
all_watertight = True
for obj in bpy.context.scene.objects:
    if obj.type != "MESH":
        continue
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    dims = [0.0, 0.0, 0.0]
    if corners:
        for i in range(3):
            vals = [c[i] for c in corners]
            dims[i] = max(vals) - min(vals)
        all_corners.extend(corners)
    # audit in world space is unnecessary: topology/areas are
    # scale-consistent per object for a watertight verdict; audit the
    # object after applying its world transform to a copy is overkill --
    # audit local data (generator GLBs ship untransformed roots).
    a = audit(obj.data)
    meshes.append({"name": obj.name, "verts": a["verts"], "tris": a["tris"],
                   "dims_m": [round(d, 4) for d in dims],
                   "shells": a["shells"], "watertight": a["watertight"]})
    for k in tot:
        tot[k] += a[k]
    all_watertight = all_watertight and a["watertight"]

scene_dims = [0.0, 0.0, 0.0]
if all_corners:
    for i in range(3):
        vals = [c[i] for c in all_corners]
        scene_dims[i] = round(max(vals) - min(vals), 4)

tot["watertight"] = bool(meshes) and all_watertight
print("TRIAGE_JSON_BEGIN")
print(json.dumps({"meshes": meshes, "scene_dims_m": scene_dims,
                  "totals": tot}))
print("TRIAGE_JSON_END")
"""


def probe_glb(path, blender):
    """Run the Blender probe on a GLB/GLTF; return (probe_dict, error)."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as tf:
        tf.write(GLB_PROBE)
        script = tf.name
    try:
        proc = subprocess.run(
            [blender, "-b", "--factory-startup", "-noaudio",
             "--python", script, "--", str(path)],
            capture_output=True, text=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"blender probe failed: {exc}"
    finally:
        os.unlink(script)
    m = re.search(r"TRIAGE_JSON_BEGIN\s*(\{.*?\})\s*TRIAGE_JSON_END",
                  proc.stdout, re.S)
    if not m:
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return None, f"blender produced no probe output: {tail.strip()}"
    return json.loads(m.group(1)), None


# ---------------------------------------------------------------------------
# ripdoctor invocation + JSON parsing
# ---------------------------------------------------------------------------

def find_sibling_tool(env_var, flag_value, sibling_rel):
    if flag_value:
        return Path(flag_value).expanduser()
    env = os.environ.get(env_var)
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    for cand in (here.parent / sibling_rel, here / Path(sibling_rel).name):
        if cand.exists():
            return cand
    return None


def run_ripdoctor(fbx_path, ripdoctor_py, blender):
    """Run ripdoctor on one FBX and parse its --json payload.
    Returns (payload_dict, error). ripdoctor exits 1 on a FAIL verdict --
    that is a diagnosis, not an invocation error."""
    env = dict(os.environ)
    if blender and blender != "blender":
        bdir = str(Path(blender).expanduser().resolve().parent)
        env["PATH"] = bdir + os.pathsep + env.get("PATH", "")
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as tf:
        json_path = tf.name
    try:
        proc = subprocess.run(
            [sys.executable, str(ripdoctor_py), str(fbx_path),
             "--json", json_path],
            capture_output=True, text=True, timeout=600, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"ripdoctor failed to run: {exc}"
    finally:
        pass
    try:
        with open(json_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        tail = (proc.stderr or proc.stdout or "")[-400:]
        return None, f"ripdoctor produced no JSON report: {tail.strip()}"
    finally:
        os.unlink(json_path)
    if isinstance(payload, dict) and "targets" in payload:
        payload = payload["targets"][0]
    return payload, None


def parse_ripdoctor_fbx(payload):
    """Reduce a ripdoctor loose-FBX payload to the metrics triage needs.

    Height note: ripdoctor reports per-mesh world-space dims; a model's
    height is the tallest mesh's Z extent. For character rips (one body
    mesh standing at the origin) that is the model's height; for
    multi-part rips it is the documented approximation this bridge
    uses, and it is the same number a human would read off the report.
    """
    checks = {c["id"]: c for c in payload.get("checks", [])}
    geo = checks.get("fbx_geometry", {})
    probe_check = checks.get("fbx_probe", {})
    meshes = (geo.get("details") or {}).get("meshes", [])
    height = max((m["dims_m"][2] for m in meshes), default=0.0)
    max_dim = max((max(m["dims_m"]) for m in meshes), default=0.0)
    polys = sum(m.get("polys", 0) for m in meshes)
    skin = checks.get("fbx_skinning", {})
    return {
        "meshes": payload.get("counts", {}).get("meshes", len(meshes)),
        "verts": payload.get("counts", {}).get("verts", 0),
        "polys": polys,
        "bones": payload.get("counts", {}).get("bones", 0),
        "height_m": round(height, 4),
        "max_dim_m": round(max_dim, 4),
        "geometry_status": geo.get("status"),
        "geometry_summary": geo.get("summary", ""),
        "geometry_flags": (geo.get("details") or {}).get("flags", []),
        "probe_error": probe_check.get("summary") if probe_check else None,
        "skinning_status": skin.get("status"),
        "skinning_summary": skin.get("summary", ""),
        "mesh_details": meshes,
    }


# ---------------------------------------------------------------------------
# classification -- pure functions over measured metrics (unit-tested)
# ---------------------------------------------------------------------------

def _item(target, kind, bucket, reasons, notes, metrics, cleanup=None):
    status = BUCKET_STATUS[bucket]
    summary = "; ".join(reasons) if reasons else bucket
    return {
        "target": str(target),
        "kind": kind,
        "bucket": bucket,
        "reasons": reasons,
        "notes": notes,
        "metrics": metrics,
        "cleanup": cleanup,
        "checks": [{
            "id": "triage", "title": "Triage classification",
            "status": status, "summary": summary,
            "details": {"bucket": bucket},
        }],
    }


def _cleanup_block(source_path, input_glb, voxel, target_tris, blender,
                   cleanup_py, out_dir, convert_from=None):
    out_dir = Path(out_dir) if out_dir else Path(source_path).parent
    out_glb = out_dir / (Path(input_glb).stem + "_cleaned.glb")
    commands = []
    if convert_from is not None:
        helper = Path(__file__).resolve().parent / "fbx_to_glb.py"
        commands.append(
            f"{shlex.quote(blender)} -b --factory-startup -noaudio "
            f"--python {shlex.quote(str(helper))} -- "
            f"{shlex.quote(str(convert_from))} {shlex.quote(str(input_glb))}")
    cleanup_cmd = (
        f"{shlex.quote(blender)} --background --python "
        f"{shlex.quote(str(cleanup_py))} -- "
        f"--input {shlex.quote(str(input_glb))} "
        f"--output {shlex.quote(str(out_glb))} "
        f"--target-tris {target_tris} --voxel-size {voxel} "
        f"--report {shlex.quote(str(out_glb) + '.report.json')}")
    commands.append(cleanup_cmd)
    return {"voxel_size": voxel, "target_tris": target_tris,
            "input": str(input_glb), "output": str(out_glb),
            "commands": commands}


def classify_fbx(metrics, opts, source_path):
    """Sort one FBX from its ripdoctor metrics."""
    notes = []
    if metrics["probe_error"]:
        return _item(source_path, "fbx", "hopeless",
                     [f"ripdoctor could not probe this FBX: "
                      f"{metrics['probe_error']}"],
                     notes, metrics)
    if metrics["geometry_status"] == "FAIL":
        reason = ("; ".join(metrics["geometry_flags"])
                  or metrics["geometry_summary"])
        return _item(source_path, "fbx", "hopeless",
                     [f"ripdoctor FAIL -- {reason}"],
                     notes, metrics)
    if metrics["verts"] == 0 or metrics["meshes"] == 0:
        return _item(source_path, "fbx", "hopeless",
                     ["no geometry: ripdoctor found zero vertices"],
                     notes, metrics)
    for flag in metrics["geometry_flags"]:
        if "plausible bounds" not in flag:
            notes.append(f"ripdoctor: {flag}")
    if metrics["skinning_status"] in ("WARN", "INFO"):
        notes.append(f"ripdoctor: {metrics['skinning_summary']}")

    height = metrics["height_m"]
    # Generator soup that happens to be saved as FBX: unrigged and huge.
    # Voxel cleanup is the fix; cleanup eats GLB, so the command list
    # starts with an FBX -> GLB conversion.
    if metrics["bones"] == 0 and metrics["polys"] >= opts["soup_polys"]:
        voxel = compute_voxel_size(height, opts["voxel_fraction"])
        if voxel <= 0:
            return _item(source_path, "fbx", "hopeless",
                         ["soup-sized but height measured as zero -- "
                          "cannot compute a voxel size"], notes, metrics)
        target = compute_target_tris(0, opts["target_tris"])
        converted = Path(source_path).with_suffix(".triage.glb")
        cleanup = _cleanup_block(
            source_path, converted, voxel, target, opts["blender"],
            opts["cleanup_py"], opts["out_dir"],
            convert_from=source_path)
        return _item(
            source_path, "fbx", "cleanup",
            [f"unrigged and {metrics['polys']:,} polygons -- reads as "
             f"generator soup, not a game rip; voxel {voxel} "
             f"(= {opts['voxel_fraction']:.2%} of measured height "
             f"{height} m)"],
            notes, metrics, cleanup)

    verdict = metrics["geometry_status"] or "PASS"
    return _item(
        source_path, "fbx", "clean",
        [f"ripdoctor {verdict} -- healthy rip; voxel cleanup would "
         f"only destroy its UVs/skinning"],
        notes, metrics)


def classify_glb(probe, error, opts, source_path):
    """Sort one GLB/GLTF from the built-in probe."""
    if probe is None:
        return _item(source_path, "glb", "hopeless",
                     [f"could not be probed: {error}"], [], {})
    tot = probe["totals"]
    dims = probe["scene_dims_m"]
    metrics = {
        "meshes": len(probe["meshes"]),
        "verts": tot["verts"], "tris": tot["tris"],
        "shells": tot["shells"],
        "boundary_edges": tot["boundary_edges"],
        "nonmanifold_edges": tot["nonmanifold_edges"],
        "zero_area_tris": tot["zero_area_tris"],
        "watertight": tot["watertight"],
        "height_m": dims[2],
        "max_dim_m": max(dims) if dims else 0.0,
        "scene_dims_m": dims,
    }
    notes = []
    if tot["verts"] == 0 or not probe["meshes"]:
        return _item(source_path, "glb", "hopeless",
                     ["no geometry: probe found zero vertices"],
                     notes, metrics)
    if metrics["max_dim_m"] and metrics["max_dim_m"] < SCALE_FAIL_MAX_DIM:
        return _item(
            source_path, "glb", "hopeless",
            [f"scene max dimension is {metrics['max_dim_m']:.4f} m -- "
             f"the 0.01-scale unit class: geometry is ~100x too small; "
             f"correct the scale before cleanup, a voxel size computed "
             f"at this scale is meaningless"],
            notes, metrics)
    if metrics["max_dim_m"] > CM_WARN_MAX_DIM:
        notes.append(f"scene max dimension is {metrics['max_dim_m']:.1f} m "
                     f"-- centimeter units suspected; voxel size is "
                     f"relative to height so it still applies, but fix "
                     f"units for downstream")

    target = compute_target_tris(tot["tris"], opts["target_tris"])
    if tot["watertight"] and tot["tris"] <= opts["target_tris"]:
        return _item(
            source_path, "glb", "clean",
            [f"watertight and {tot['tris']:,} tris is within the "
             f"{opts['target_tris']:,} budget -- nothing for cleanup "
             f"to fix"], notes, metrics)

    voxel = compute_voxel_size(metrics["height_m"], opts["voxel_fraction"])
    if voxel <= 0:
        return _item(source_path, "glb", "hopeless",
                     ["height measured as zero -- cannot compute a "
                      "voxel size"], notes, metrics)
    why = []
    if not tot["watertight"]:
        why.append(f"not watertight ({tot['shells']:,} shells, "
                   f"{tot['boundary_edges']:,} boundary edges)")
    if tot["tris"] > opts["target_tris"]:
        why.append(f"{tot['tris']:,} tris over the "
                   f"{opts['target_tris']:,} budget")
    cleanup = _cleanup_block(source_path, source_path, voxel, target,
                             opts["blender"], opts["cleanup_py"],
                             opts["out_dir"])
    return _item(
        source_path, "glb", "cleanup",
        ["; ".join(why) +
         f"; voxel {voxel} (= {opts['voxel_fraction']:.2%} of measured "
         f"height {metrics['height_m']} m), target {target:,} tris"],
        notes, metrics, cleanup)


# ---------------------------------------------------------------------------
# worklist assembly + rendering
# ---------------------------------------------------------------------------

def collect_targets(targets, recursive):
    files = []
    for t in targets:
        p = Path(t).expanduser()
        if p.is_file() and p.suffix.lower() in MODEL_SUFFIXES:
            files.append(p)
        elif p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            files.extend(f for f in it
                         if f.is_file() and f.suffix.lower()
                         in MODEL_SUFFIXES)
    seen, out = set(), []
    for f in sorted(files):
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def build_worklist(items, targets):
    counts = {"models": len(items),
              "clean": sum(1 for i in items if i["bucket"] == "clean"),
              "cleanup": sum(1 for i in items if i["bucket"] == "cleanup"),
              "hopeless": sum(1 for i in items if i["bucket"] == "hopeless")}
    return {"tool": "triage-doctor", "version": VERSION,
            "target": ", ".join(str(t) for t in targets),
            "counts": counts, "items": items}


def exit_code_for(worklist):
    c = worklist["counts"]
    return 0 if c["cleanup"] == 0 and c["hopeless"] == 0 else 1


def render_markdown(worklist):
    c = worklist["counts"]
    lines = [
        "# TRIAGE WORKLIST",
        "",
        f"**Targets:** `{worklist['target']}`  ",
        f"**Models:** {c['models']} -- ✅ {c['clean']} clean · "
        f"🧹 {c['cleanup']} cleanup · 🛑 {c['hopeless']} hopeless",
        "",
    ]
    for bucket in ("cleanup", "hopeless", "clean"):
        items = [i for i in worklist["items"] if i["bucket"] == bucket]
        if not items:
            continue
        lines.append(f"## {ICON[bucket]} ({len(items)})")
        lines.append("")
        for i in items:
            m = i["metrics"]
            lines.append(f"### `{i['target']}`")
            bits = []
            if m:
                if i["kind"] == "fbx":
                    bits.append(f"{m.get('verts', 0):,} verts, "
                                f"{m.get('polys', 0):,} polys, "
                                f"{m.get('bones', 0)} bones, "
                                f"height {m.get('height_m', 0)} m")
                else:
                    bits.append(f"{m.get('verts', 0):,} verts, "
                                f"{m.get('tris', 0):,} tris, "
                                f"{m.get('shells', 0):,} shells, "
                                f"watertight: "
                                f"{'YES' if m.get('watertight') else 'NO'}, "
                                f"height {m.get('height_m', 0)} m")
            if bits:
                lines.append(f"- {bits[0]}")
            for r in i["reasons"]:
                lines.append(f"- {r}")
            for n in i["notes"]:
                lines.append(f"- note: {n}")
            if i["cleanup"]:
                for cmd in i["cleanup"]["commands"]:
                    lines.append(f"- run: `{cmd}`")
            lines.append("")
    return "\n".join(lines)


def render_commands(worklist):
    lines = ["#!/bin/sh",
             "# triage-doctor cleanup commands -- one block per model "
             "in the cleanup bucket.",
             "# Generated from measured heights; voxel sizes follow "
             "ai-mesh-cleanup's",
             "# 0.3-0.4%-of-height rule. Review before running.",
             ""]
    for i in worklist["items"]:
        if i["bucket"] != "cleanup" or not i["cleanup"]:
            continue
        lines.append(f"# {i['target']}")
        lines.extend(i["cleanup"]["commands"])
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="triage-doctor",
        description="Sort models into a clean / cleanup / hopeless "
                    "worklist using ripdoctor's diagnosis, with "
                    "ai-mesh-cleanup settings computed from measured "
                    "heights.")
    ap.add_argument("targets", nargs="+",
                    help="model files (.fbx/.glb/.gltf) or folders of them")
    ap.add_argument("--json", dest="json_out", help="write worklist JSON here")
    ap.add_argument("--md", dest="md_out", help="write worklist Markdown here")
    ap.add_argument("--commands", dest="commands_out",
                    help="write a shell file with the cleanup commands")
    ap.add_argument("--run", action="store_true",
                    help="run cleanup for every cleanup-bucket model")
    ap.add_argument("--out-dir", help="where --run writes cleaned GLBs "
                                      "(default: next to each source)")
    ap.add_argument("--recursive", action="store_true",
                    help="descend into subfolders when scanning folders")
    ap.add_argument("--ripdoctor", help="path to ripdoctor.py")
    ap.add_argument("--cleanup", help="path to clean_ai_mesh.py")
    ap.add_argument("--blender", default="blender",
                    help="blender executable (default: blender)")
    ap.add_argument("--voxel-fraction", type=float,
                    default=VOXEL_FRACTION_DEFAULT,
                    help="voxel size as a fraction of height "
                         "(default 0.0035)")
    ap.add_argument("--target-tris", type=int, default=TARGET_TRIS_DEFAULT,
                    help="cleanup triangle budget (default 25000)")
    ap.add_argument("--soup-polys", type=int, default=SOUP_POLYS_DEFAULT,
                    help="unrigged-FBX soup threshold in polygons "
                         "(default 300000)")
    args = ap.parse_args(argv)

    files = collect_targets(args.targets, args.recursive)
    if not files:
        print("error: no .fbx/.glb/.gltf files found in the given "
              "targets", file=sys.stderr)
        return 2

    ripdoctor_py = find_sibling_tool("TRIAGE_RIPDOCTOR", args.ripdoctor,
                                     "ripdoctor/ripdoctor.py")
    cleanup_py = find_sibling_tool("TRIAGE_CLEANUP", args.cleanup,
                                   "ai-mesh-cleanup/clean_ai_mesh.py")
    if any(f.suffix.lower() == ".fbx" for f in files) and not ripdoctor_py:
        print("error: FBX inputs need ripdoctor -- pass --ripdoctor, set "
              "TRIAGE_RIPDOCTOR, or clone ripdoctor next to this repo",
              file=sys.stderr)
        return 2
    cleanup_resolved = cleanup_py is not None and cleanup_py.exists()
    if cleanup_py is None:
        cleanup_py = Path("clean_ai_mesh.py")  # placeholder in commands

    opts = {"voxel_fraction": args.voxel_fraction,
            "target_tris": args.target_tris,
            "soup_polys": args.soup_polys,
            "blender": args.blender,
            "cleanup_py": cleanup_py,
            "out_dir": args.out_dir}
    if not (VOXEL_BAND[0] <= args.voxel_fraction <= VOXEL_BAND[1]):
        print(f"note: --voxel-fraction {args.voxel_fraction} is outside "
              f"ai-mesh-cleanup's documented {VOXEL_BAND[0]:.1%}-"
              f"{VOXEL_BAND[1]:.1%} band; using it anyway",
              file=sys.stderr)

    items = []
    for f in files:
        if f.suffix.lower() == ".fbx":
            payload, err = run_ripdoctor(f, ripdoctor_py, args.blender)
            if payload is None:
                items.append(_item(f, "fbx", "hopeless",
                                   [f"ripdoctor failed: {err}"], [], {}))
            else:
                items.append(classify_fbx(parse_ripdoctor_fbx(payload),
                                          opts, f))
        else:
            probe, err = probe_glb(f, args.blender)
            items.append(classify_glb(probe, err, opts, f))

    worklist = build_worklist(items, args.targets)

    if args.run:
        for i in items:
            if i["bucket"] != "cleanup" or not i["cleanup"]:
                continue
            if not cleanup_resolved:
                print("error: --run needs ai-mesh-cleanup -- pass "
                      "--cleanup, set TRIAGE_CLEANUP, or clone it next "
                      "to this repo", file=sys.stderr)
                return 2
            ok = True
            for cmd in i["cleanup"]["commands"]:
                print(f"+ {cmd}", flush=True)
                proc = subprocess.run(cmd, shell=True)
                if proc.returncode != 0:
                    ok = False
                    break
            i["run"] = {"ok": ok,
                        "output": i["cleanup"]["output"],
                        "output_exists": Path(
                            i["cleanup"]["output"]).exists()}

    md = render_markdown(worklist)
    print(md)
    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(worklist, fh, indent=2, default=str)
    if args.commands_out:
        with open(args.commands_out, "w", encoding="utf-8") as fh:
            fh.write(render_commands(worklist) + "\n")
    return exit_code_for(worklist)


if __name__ == "__main__":
    sys.exit(main())
