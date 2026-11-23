#!/usr/bin/env python3
"""
fbx_to_glb.py -- convert one FBX to GLB, headless.

Helper for triage-doctor: ai-mesh-cleanup consumes GLB, so an FBX that
triages into the cleanup bucket needs this conversion step first.
triage-doctor emits it as the first command of the model's block.

Usage (note the "--" separator, as with every Blender script):

    blender -b --factory-startup -noaudio --python fbx_to_glb.py -- \
        input.fbx output.glb

Object transforms are applied before export so the GLB carries the
same world-space geometry ripdoctor measured.

Copyright (c) 2026 1915rising -- MIT License (see LICENSE).
"""

import sys

import bpy


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    if len(argv) != 2:
        print("usage: fbx_to_glb.py -- <input.fbx> <output.glb>")
        return 2
    src, dst = argv

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=src)

    meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    if not meshes:
        print(f"ERROR: no mesh objects found in {src}")
        return 1
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=True, rotation=True,
                                   scale=True)
    bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB")
    print(f"DONE -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
