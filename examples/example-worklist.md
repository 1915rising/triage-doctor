# TRIAGE WORKLIST

**Targets:** `models/`  
**Models:** 3 -- ✅ 1 clean · 🧹 1 cleanup · 🛑 1 hopeless

## 🧹 cleanup (1)

### `generated_fullbody_raw.glb`
- 891,360 verts, 1,499,788 tris, 8,053 shells, watertight: NO, height 1.143 m
- not watertight (8,053 shells, 218,610 boundary edges); 1,499,788 tris over the 25,000 budget; voxel 0.004 (= 0.35% of measured height 1.143 m), target 25,000 tris
- run: `blender --background --python clean_ai_mesh.py -- --input generated_fullbody_raw.glb --output generated_fullbody_raw_cleaned.glb --target-tris 25000 --voxel-size 0.004 --report generated_fullbody_raw_cleaned.glb.report.json`

## 🛑 hopeless (1)

### `rip_at_wrong_scale.fbx`
- 15,197 verts, 24,000 polys, 145 bones, height 1.6396 m
- ripdoctor FAIL -- scene max dimension is 0.0164 m -- the 0.01-scale class: geometry is ~100x too small (unit mismatch at export/import)

## ✅ clean (1)

### `hero.fbx`
- 15,197 verts, 24,000 polys, 145 bones, height 1.6396 m
- ripdoctor PASS -- healthy rip; voxel cleanup would only destroy its UVs/skinning

