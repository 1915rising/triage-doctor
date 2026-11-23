# Triage Doctor

The bridge between [Rip Doctor](https://github.com/1915rising/ripdoctor)
and [ai-mesh-cleanup](https://github.com/1915rising/ai-mesh-cleanup):
point it at a folder of models and it sorts every one into a worklist —
**clean**, **cleanup** (with the settings computed for you), or
**hopeless** (with the reason) — instead of leaving you a wall of
undifferentiated FAILs.

## Why this exists

Rip Doctor diagnoses FBX rips and reports the numbers as JSON:
per-mesh counts, world-space bounding boxes, and a scale verdict.
ai-mesh-cleanup fixes generator soup, but its one hard-to-pick
parameter — voxel size — follows a documented rule of thumb:
**roughly 0.3–0.4% of the model's height**. That height is exactly
what Rip Doctor measures. Nobody had connected the two.

The motivating pile: a census of 23 game rips that came back
**20 FAIL / 3 WARN / 0 PASS**. Read as a worklist instead of a wall of
red, most of those FAILs are one specific, fixable-up-front problem
(the 0.01-scale unit class) — and the triage is what tells you that
*before* you burn time cleaning the wrong thing in the wrong order.

## What it does

For every `.fbx` / `.glb` / `.gltf` you give it (files, or folders of
them):

| Input | Diagnosis source |
|---|---|
| `.fbx` | Runs **ripdoctor** and reads its `--json` report: geometry/scale verdict, per-mesh dims, bones, skinning. |
| `.glb` / `.gltf` | A built-in headless-Blender probe whose audit math (shells, boundary / non-manifold edges, zero-area tris, watertight) mirrors `clean_ai_mesh.py`, so triage's "before" numbers are the numbers cleanup itself will report. |

Then each model lands in exactly one bucket:

- **✅ clean** — a healthy rip (voxel cleanup would only destroy its
  UVs and skinning), or a GLB that is already watertight and within
  the triangle budget.
- **🧹 cleanup** — generator soup, or soup saved as FBX. Comes with
  the full ai-mesh-cleanup command line: voxel size computed from the
  measured height at 0.35% (configurable inside the 0.3–0.4% band),
  and a target tri count that never exceeds the raw count. An FBX in
  this bucket gets an FBX→GLB conversion step first (cleanup eats
  GLB), using the bundled `fbx_to_glb.py`.
- **🛑 hopeless** — cleanup cannot help *yet*, and the reason says
  what to fix first: the 0.01-scale unit class (~100× too small —
  correct the scale before anything, a voxel size computed at that
  scale is meaningless), an unreadable/corrupt file, an empty mesh.

Output is a Markdown worklist (cleanup first, then hopeless, then
clean), plus — with `--json` — the same worklist in the report
conventions the doctor family shares (per-target `kind` / `counts` /
`checks`), so pipeline tooling like the Doctor Chain can consume it.
`--commands` writes the cleanup commands to a shell file;
`--run` executes them.

## Usage

```bash
# Triage a folder of rips and generator exports:
python3 triage_doctor.py models/ --json worklist.json --commands cleanup.sh

# Just look (default): prints the worklist, changes nothing.
python3 triage_doctor.py hero.fbx generated_raw.glb

# Do the cleanup bucket for real:
python3 triage_doctor.py models/ --run --out-dir cleaned/
```

Key options: `--recursive`, `--voxel-fraction F` (default 0.0035),
`--target-tris N` (default 25000), `--soup-polys N` (default 300000 —
an unrigged FBX at or above this polygon count counts as soup),
`--blender PATH`, `--ripdoctor PATH`, `--cleanup PATH`.

Exit codes: `0` = every model clean, `1` = the worklist has cleanup
or hopeless items, `2` = usage error.

## How the numbers are computed

- **Height (FBX):** ripdoctor reports per-mesh world-space dims; the
  model's height is the tallest mesh's Z extent. For character rips
  (one body mesh standing at the origin) that is the model's height;
  for multi-part rips it is the documented approximation — and it is
  the same number a human would read off the ripdoctor report.
- **Height (GLB):** the probe's scene Z extent, measured the same way
  ripdoctor measures FBX scenes.
- **Voxel size:** `round(0.0035 × height, 4)`, clamped to ≥ 0.0001.
  Sanity anchor: for a 1.143-unit-tall generator export this yields
  **0.004** — the voxel size ai-mesh-cleanup's own testing arrived at
  by hand for that class of model.
- **Target tris:** `min(--target-tris, raw tris)` — never ask
  decimate for triangles that don't exist.

## Requirements

- Python 3.8+, standard library only (the driver).
- Blender on `PATH` (or `--blender`) for the probes.
- [ripdoctor](https://github.com/1915rising/ripdoctor) for FBX
  inputs — found via `--ripdoctor`, `$TRIAGE_RIPDOCTOR`, or a sibling
  `../ripdoctor/` checkout (the layout of the doctor family repos).
- [ai-mesh-cleanup](https://github.com/1915rising/ai-mesh-cleanup)
  only for the emitted commands / `--run` (same discovery rules,
  `--cleanup` / `$TRIAGE_CLEANUP` / sibling checkout). Without it,
  commands are still emitted with a placeholder path.

## Tests

```bash
python3 tests/run_tests.py
```

The classifiers are pure functions over measured metrics, so the
suite needs no Blender: it feeds them fixtures shaped like the real
tools' output (including the recorded audit of a real 1.5M-triangle
generator export, and a real character rip's ripdoctor numbers) and
asserts the buckets, the voxel math, and the emitted commands.

## Known limits (v1, stated on purpose)

- **Files, not AssetRipper project exports.** Rip Doctor's
  shader/material checks on an `ExportedProject` folder are its own
  job; triage consumes the per-FBX diagnosis. A model can triage
  "clean" here and still carry a dummy shader — run ripdoctor on the
  export itself for that layer.
- **The soup heuristic is a heuristic.** An unrigged FBX over the
  polygon threshold is *probably* generator soup; a legitimate
  high-poly static sculpt would land in the same bucket. The reason
  string shows the numbers so a human can overrule it.
- **Height for multi-mesh FBXs is the tallest part**, not a scene
  union (ripdoctor's JSON reports per-mesh dims). Voxel sizes derived
  from it are rule-of-thumb inputs, not precision values.
- **Cleanup output is geometry only** — ai-mesh-cleanup destroys UVs
  by construction (voxel remesh). Triage inherits that limit; it does
  not hide it.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 1915rising.
