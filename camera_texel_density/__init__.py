# SPDX-License-Identifier: GPL-3.0-or-later
"""
Camera Texel Density
====================

Scales the active UV map of the selected mesh objects so that, seen through
the scene's active camera, every object shows the same apparent texel
density (the same checker size on screen).

One press of "Optimize Texel Density" does two things in a single step:

1. Normalization: each object's UV density ratio is measured as
       ratio = total UV area / total world-space surface area
   That ratio is proportional to the square of linear texel density, so the
   normalization scale that brings an object to the active object's density is
       sqrt(ratio_active / ratio_object)

2. Camera-distance scaling: on screen, size falls off as 1 / depth, where
   depth is measured along the camera's view direction. That gives
       reference_depth / object_depth
   where the reference is the active object.

   final UV scale = normalization scale * distance scale

Each UV island is scaled around the centre of its own bounding box, so every
island stays where it was placed. Nothing is moved, rotated or packed.

Why pressing the button again does not compound
-----------------------------------------------
After one run, every object's ratio equals
ratio_active * (depth_active / depth_object)^2. On a second run the
normalization scale is exactly depth_object / depth_active and the distance
scale is its inverse, so the product is 1.0 and nothing changes. The result
depends only on the current geometry, camera and reference, never on how many
times the operator has run, so no stored baseline UVs are needed. The same
holds if the user later makes a different, already-optimized object active.
"""

bl_info = {
    "name": "Camera Texel Density",
    "author": "Daniel Faber",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "3D Viewport / UV Editor > Sidebar (N) > UV Tools",
    "description": "Scale UVs so texel density looks consistent from a fixed camera",
    "category": "UV",
}

import math

import bmesh
import bpy
from bpy.props import EnumProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Vector


# Tolerances ---------------------------------------------------------------

# Depths at or below this value (metres) are at or behind the camera plane.
MIN_DEPTH = 1e-6
# UV or world areas below this value count as zero.
MIN_AREA = 1e-12
# Scales this close to 1.0 are treated as "already optimized". UVs are stored
# as 32-bit floats, so smaller corrections are just rounding noise.
SCALE_TOLERANCE = 1e-5
# Distance scales above this value get a "very close to camera" warning.
CLOSE_WARNING_SCALE = 10.0
# UVs are rounded to this many decimals when deciding whether two faces share
# a UV vertex, i.e. belong to the same island.
UV_KEY_DECIMALS = 6
# Maximum number of different UV map names listed in the panel's mismatch warning.
MAX_UV_GROUPS_SHOWN = 5
# Maximum number of individual warnings sent to the status bar / Info editor.
# The full list is always printed to the system console.
MAX_REPORTED_WARNINGS = 8


# -------------------------------------------------------------------------
# Camera geometry
# -------------------------------------------------------------------------

class CameraView:
    """The camera's position, its view direction and (for perspective
    cameras) the planes of its view frustum, all in world space."""

    def __init__(self, scene, cam_obj):
        cam = cam_obj.data
        mw = cam_obj.matrix_world
        # Object scale has no effect on what a camera sees, so take the
        # rotation only.
        rot = mw.to_3x3().normalized()

        self.origin = mw.translation.copy()
        self.forward = (rot @ Vector((0.0, 0.0, -1.0))).normalized()
        self.is_ortho = cam.type == 'ORTHO'
        self.is_panoramic = cam.type == 'PANO'

        # Inward-facing planes (normal, offset). A point p is inside when
        # normal.dot(p) - offset >= 0 for every plane.
        self.planes = []
        if cam.type == 'PERSP':
            # view_frame() accounts for sensor fit, render aspect ratio and lens shift.
            corners = [self.origin + rot @ c for c in cam.view_frame(scene=scene)]
            for i in range(4):
                a = corners[i] - self.origin
                b = corners[(i + 1) % 4] - self.origin
                n = a.cross(b).normalized()
                if n.dot(self.forward) < 0.0:
                    n.negate()
                self.planes.append((n, n.dot(self.origin)))
            near_point = self.origin + self.forward * cam.clip_start
            self.planes.append((self.forward.copy(), self.forward.dot(near_point)))

    def depth(self, point):
        """Distance from the camera along its view direction."""
        return (point - self.origin).dot(self.forward)

    def is_inside(self, point):
        return all(n.dot(point) - d >= 0.0 for n, d in self.planes)


def _clip_polygon(points, planes):
    """Clip a polygon against convex planes (Sutherland-Hodgman). Returns the
    polygon's vertices that lie inside the frustum."""
    for n, d in planes:
        if not points:
            break
        clipped = []
        prev = points[-1]
        prev_dist = n.dot(prev) - d
        for cur in points:
            cur_dist = n.dot(cur) - d
            if cur_dist >= 0.0:
                if prev_dist < 0.0:
                    clipped.append(prev.lerp(cur, prev_dist / (prev_dist - cur_dist)))
                clipped.append(cur)
            elif prev_dist >= 0.0:
                clipped.append(prev.lerp(cur, prev_dist / (prev_dist - cur_dist)))
            prev, prev_dist = cur, cur_dist
        points = clipped
    return points


def nearest_visible_depth(view, world_co, face_vert_indices):
    """Smallest camera depth over the part of the mesh surface the camera
    can see.

    Depth changes linearly across every face, so its minimum over any face is
    always at a corner of that face. For geometry fully inside the frame, the
    nearest vertex is therefore exactly the nearest surface point. Faces that
    cross the frame edge are clipped to the frustum first, so geometry outside
    the shot (a floor running behind the camera, for example) doesn't affect
    the result.

    Returns (depth, visible). visible is False when no geometry is in the
    camera's view; depth then falls back to the nearest vertex overall.
    """
    depths = [view.depth(p) for p in world_co]
    if not depths:
        return math.inf, False
    if not view.planes:
        return min(depths), True

    inside = [view.is_inside(p) for p in world_co]
    if all(inside):
        return min(depths), True

    best = math.inf
    for idx in face_vert_indices:
        if all(inside[i] for i in idx):
            best = min(best, min(depths[i] for i in idx))
            continue
        for p in _clip_polygon([world_co[i] for i in idx], view.planes):
            best = min(best, view.depth(p))

    if best == math.inf:
        return min(depths), False
    return best, True


# -------------------------------------------------------------------------
# Mesh measurement and UV scaling
# -------------------------------------------------------------------------

def world_surface_area(bm, world_co):
    """Total world-space surface area. Measured on triangulated geometry in
    world space, so it's correct for concave polygons, non-planar n-gons and
    non-uniform object scale. Uses Blender's own polygon triangulation."""
    area = 0.0
    for tri in bm.calc_loop_triangles():
        a = world_co[tri[0].vert.index]
        b = world_co[tri[1].vert.index]
        c = world_co[tri[2].vert.index]
        area += (b - a).cross(c - a).length
    return area * 0.5


def uv_surface_area(bm, uv_layer):
    """Total UV area. Each face's area comes from the shoelace formula on its
    own UV polygon. That's exact for any simple polygon, concave ones
    included, and doesn't depend on the 3D triangulation (which can fold over
    in UV space when the UVs are distorted). abs() makes mirrored (flipped)
    islands count positively. Stacked or overlapping islands are each counted,
    which is correct for density: every face uses its own UV area."""
    total = 0.0
    for face in bm.faces:
        uvs = [loop[uv_layer].uv for loop in face.loops]
        s = 0.0
        x0, y0 = uvs[-1]
        for x1, y1 in uvs:
            s += x0 * y1 - x1 * y0
            x0, y0 = x1, y1
        total += abs(s)
    return total * 0.5


def uv_islands(bm, uv_layer):
    """Group faces into UV islands. Two faces belong to the same island when
    they share a vertex that has the same UV in both (union-find, linear time)."""
    bm.faces.index_update()
    parent = list(range(len(bm.faces)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_face_for_key = {}
    for face in bm.faces:
        fi = face.index
        for loop in face.loops:
            uv = loop[uv_layer].uv
            key = (loop.vert.index,
                   round(uv.x, UV_KEY_DECIMALS),
                   round(uv.y, UV_KEY_DECIMALS))
            other = first_face_for_key.setdefault(key, fi)
            if other != fi:
                ra, rb = find(fi), find(other)
                if ra != rb:
                    parent[ra] = rb

    islands = {}
    for face in bm.faces:
        islands.setdefault(find(face.index), []).append(face)
    return list(islands.values())


def scale_uv_islands(bm, uv_layer, scale):
    """Scale every UV island uniformly around its own bounding-box centre.
    Returns the number of islands."""
    islands = uv_islands(bm, uv_layer)
    for faces in islands:
        loops = [loop for face in faces for loop in face.loops]
        us = [loop[uv_layer].uv.x for loop in loops]
        vs = [loop[uv_layer].uv.y for loop in loops]
        cu = (min(us) + max(us)) * 0.5
        cv = (min(vs) + max(vs)) * 0.5
        for loop, u, v in zip(loops, us, vs):
            loop[uv_layer].uv = (cu + (u - cu) * scale, cv + (v - cv) * scale)
    return len(islands)


# -------------------------------------------------------------------------
# Per-mesh record
# -------------------------------------------------------------------------

class MeshRecord:
    """One mesh datablock to process. Selected objects that share a mesh
    become a single record, measured with the user closest to the camera."""

    def __init__(self, mesh, users):
        self.mesh = mesh
        self.users = users              # selected objects that use this mesh
        self.obj = None                 # the user the measurements came from
        self.bm = None
        self.owns_bm = False            # True when we must free() the BMesh
        self.uv_layer = None
        self.uv_name = ""
        self.depth = math.inf
        self.ratio = 0.0
        self.skip_reason = None
        self.scale = 1.0

    @property
    def label(self):
        if len(self.users) == 1:
            return self.users[0].name
        return f"{self.obj.name if self.obj else self.users[0].name} (+{len(self.users) - 1} sharing '{self.mesh.name}')"

    def free(self):
        if self.bm is not None and self.owns_bm:
            self.bm.free()
        self.bm = None


def measure_record(rec, view, method, warnings):
    """Fill in rec.depth and rec.ratio, or set rec.skip_reason."""
    me = rec.mesh

    if me.library is not None or me.override_library is not None:
        rec.skip_reason = "mesh is linked from a library and cannot be edited"
        return

    uv = me.uv_layers.active
    if uv is None:
        rec.skip_reason = "has no UV map"
        return
    rec.uv_name = uv.name

    if me.is_editmode:
        rec.bm = bmesh.from_edit_mesh(me)
        rec.owns_bm = False
    else:
        rec.bm = bmesh.new()
        rec.bm.from_mesh(me)
        rec.owns_bm = True
    bm = rec.bm
    bm.verts.index_update()

    rec.uv_layer = bm.loops.layers.uv.get(uv.name)
    if rec.uv_layer is None:
        rec.skip_reason = f"UV map '{uv.name}' not found in mesh data"
        return
    if not bm.faces:
        rec.skip_reason = "has no faces"
        return

    # Measure each selected user and keep the one closest to the camera.
    face_vert_indices = None
    best = None  # (depth, visible, obj, world_co)
    for obj in rec.users:
        mw = obj.matrix_world
        world_co = [mw @ v.co for v in bm.verts]
        if view.is_ortho:
            depth, visible = 1.0, True  # depth doesn't matter for ortho
        elif method == 'ORIGIN':
            depth, visible = view.depth(mw.translation), True
        else:
            if face_vert_indices is None:
                face_vert_indices = [[v.index for v in f.verts] for f in bm.faces]
            depth, visible = nearest_visible_depth(view, world_co, face_vert_indices)
        if best is None or depth < best[0]:
            best = (depth, visible, obj, world_co)

    depth, visible, obj, world_co = best
    rec.obj = obj
    rec.depth = depth

    if len(rec.users) > 1:
        others = ", ".join(o.name for o in rec.users if o is not obj)
        warnings.append(
            f"'{obj.name}' shares mesh '{me.name}' with {others}. A shared mesh can "
            f"only have one UV layout, so it was set for the closest user "
            f"('{obj.name}'). The others will look slightly over-detailed")

    if not visible:
        warnings.append(f"'{obj.name}' is outside the camera view, so its nearest "
                        f"point ignoring the frame was used")

    if not view.is_ortho and depth <= MIN_DEPTH:
        rec.skip_reason = ("is at or behind the camera"
                           + (" (try Nearest Point)" if method == 'ORIGIN' else ""))
        return

    uv_area = uv_surface_area(bm, rec.uv_layer)
    world_area = world_surface_area(bm, world_co)
    if world_area <= MIN_AREA:
        rec.skip_reason = "has zero surface area"
        return
    if uv_area <= MIN_AREA:
        rec.skip_reason = f"UV map '{uv.name}' has zero area (not unwrapped?)"
        return
    rec.ratio = uv_area / world_area


def count_object_users(meshes):
    """How many objects in the file use each of the given meshes."""
    counts = {me: 0 for me in meshes}
    for ob in bpy.data.objects:
        if ob.data in counts:
            counts[ob.data] += 1
    return counts


def active_uv_map_groups(objects):
    """Group mesh objects by the name of their active UV map (the one
    highlighted in the UV Maps list). Objects with no UV map are grouped
    under None. Returns {name: [objects]}, largest group first. More than one
    group means the selection doesn't share the same active UV map."""
    groups = {}
    for ob in objects:
        if ob.type != 'MESH':
            continue
        uv = ob.data.uv_layers.active
        groups.setdefault(uv.name if uv else None, []).append(ob)
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0] or "")))


def uv_map_label(name):
    return name if name is not None else "(no UV map)"


# -------------------------------------------------------------------------
# Settings, operator, UI
# -------------------------------------------------------------------------

class CTD_Settings(PropertyGroup):
    distance_method: EnumProperty(
        name="Distance",
        description="How each object's distance from the camera is measured "
                    "(always as depth along the camera's view direction)",
        items=(
            ('NEAREST', "Nearest Point",
             "Depth of the object's surface point nearest the camera, "
             "counting only the part inside the camera view"),
            ('ORIGIN', "Object Origin",
             "Depth of the object's origin"),
        ),
        default='NEAREST',
    )


class UV_OT_camera_texel_density(Operator):
    """Normalize the selected objects' texel density to the active object, then
    scale each object's UVs by how far it is from the active camera"""
    bl_idname = "uv.camera_texel_density"
    bl_label = "Optimize Texel Density"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if context.mode not in {'OBJECT', 'EDIT_MESH'}:
            cls.poll_message_set("Only works in Object Mode or mesh Edit Mode")
            return False
        return True

    def execute(self, context):
        scene = context.scene
        method = scene.camera_texel_density.distance_method

        # --- Validate -----------------------------------------------------
        cam_obj = scene.camera
        if cam_obj is None:
            self.report({'ERROR'}, "The scene has no active camera. Set one in "
                                   "Scene Properties > Camera")
            return {'CANCELLED'}
        if cam_obj.type != 'CAMERA':
            self.report({'ERROR'}, f"The scene camera '{cam_obj.name}' is not a camera object")
            return {'CANCELLED'}

        active = context.active_object
        if active is None or active.type != 'MESH':
            self.report({'ERROR'}, "The active object must be a mesh. It's the "
                                   "reference for texel density and distance")
            return {'CANCELLED'}

        selected = [o for o in context.selected_objects if o.type == 'MESH']
        if active not in selected:
            self.report({'ERROR'}, f"The active object '{active.name}' must be selected")
            return {'CANCELLED'}

        view = CameraView(scene, cam_obj)
        warnings = []
        if view.is_ortho:
            warnings.append("Orthographic camera: distance doesn't change on-screen "
                            "size, so only density normalization was applied")
        elif view.is_panoramic:
            warnings.append("Panoramic camera: depth along the view direction only "
                            "approximates on-screen size")

        uv_groups = active_uv_map_groups(selected)
        if len(uv_groups) > 1:
            listing = ", ".join(f"'{uv_map_label(n)}' ({len(obs)})" for n, obs in uv_groups.items())
            warnings.insert(0, f"Selected objects have different active UV maps: {listing}. "
                               f"Each object's own active UV map was scaled")

        # --- Group by mesh datablock (shared meshes are processed once) ---
        by_mesh = {}
        for o in selected:
            by_mesh.setdefault(o.data, []).append(o)
        records = [MeshRecord(me, users) for me, users in by_mesh.items()]

        try:
            # --- Measure ------------------------------------------------
            for rec in records:
                measure_record(rec, view, method, warnings)

            ref = next(r for r in records if active in r.users)
            if ref.skip_reason:
                self.report({'ERROR'}, f"Reference object '{active.name}' {ref.skip_reason}")
                return {'CANCELLED'}
            if ref.obj is not active:
                warnings.append(f"Reference mesh is shared, so the closest user "
                                f"'{ref.obj.name}' was used as the reference distance")

            user_counts = count_object_users([r.mesh for r in records if not r.skip_reason])

            # --- Compute and apply scales ---------------------------------
            changed = unchanged = skipped = islands_total = 0
            log = []
            for rec in records:
                if rec.skip_reason:
                    skipped += 1
                    warnings.append(f"Skipped '{rec.users[0].name}': {rec.skip_reason}")
                    continue

                norm_scale = math.sqrt(ref.ratio / rec.ratio)
                dist_scale = 1.0 if view.is_ortho else ref.depth / rec.depth
                rec.scale = norm_scale * dist_scale

                if dist_scale > CLOSE_WARNING_SCALE:
                    warnings.append(f"'{rec.obj.name}' is very close to the camera "
                                    f"({rec.depth:.3f} m); its UVs grow {dist_scale:.1f}x")
                extra_users = user_counts[rec.mesh] - len(rec.users)
                if extra_users > 0 and abs(rec.scale - 1.0) > SCALE_TOLERANCE:
                    warnings.append(f"Mesh '{rec.mesh.name}' is also used by {extra_users} "
                                    f"unselected object(s); their UVs change too")

                depth_txt = "   n/a" if view.is_ortho else f"{rec.depth:8.3f}"
                log.append(f"  {rec.label:<40} depth {depth_txt}  "
                           f"norm x{norm_scale:.4f}  dist x{dist_scale:.4f}  "
                           f"final x{rec.scale:.4f}  [{rec.uv_name}]")

                if abs(rec.scale - 1.0) <= SCALE_TOLERANCE:
                    unchanged += 1
                    continue

                islands_total += scale_uv_islands(rec.bm, rec.uv_layer, rec.scale)
                if rec.owns_bm:
                    rec.bm.to_mesh(rec.mesh)
                    rec.mesh.update()
                else:
                    bmesh.update_edit_mesh(rec.mesh, loop_triangles=False, destructive=False)
                changed += 1
        finally:
            for rec in records:
                rec.free()

        # --- Report -------------------------------------------------------
        print(f"\n[Camera Texel Density] camera '{cam_obj.name}', reference "
              f"'{ref.obj.name}' at depth {ref.depth:.3f} m, distance: {method}")
        for line in log:
            print(line)
        for w in warnings:
            print("  WARNING:", w)

        for w in warnings[:MAX_REPORTED_WARNINGS]:
            self.report({'WARNING'}, w)
        if len(warnings) > MAX_REPORTED_WARNINGS:
            self.report({'WARNING'}, f"... {len(warnings) - MAX_REPORTED_WARNINGS} more "
                                     f"warning(s) in the system console")

        uv_names = sorted({r.uv_name for r in records if not r.skip_reason})
        summary = (f"Optimized {changed} mesh(es), {islands_total} UV islands; "
                   f"{unchanged} already at target")
        if skipped:
            summary += f"; {skipped} skipped"
        summary += f". UV map: {', '.join(repr(n) for n in uv_names)}"
        self.report({'INFO'}, summary)

        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {'VIEW_3D', 'IMAGE_EDITOR'}:
                    area.tag_redraw()
        return {'FINISHED'}


class CTD_PanelBase:
    bl_region_type = 'UI'
    bl_category = "UV Tools"
    bl_label = "Camera Texel Density"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.camera_texel_density

        col = layout.column(align=True)
        col.label(text="Camera Distance")
        col.row(align=True).prop(settings, "distance_method", expand=True)

        layout.operator(UV_OT_camera_texel_density.bl_idname, icon='UV')

        # Show what will be used, so it's never ambiguous.
        box = layout.box()
        col = box.column(align=True)
        cam = context.scene.camera
        col.label(text=f"Camera: {cam.name if cam else 'None'}",
                  icon='CAMERA_DATA' if cam else 'ERROR')
        act = context.active_object
        uv_groups = active_uv_map_groups(context.selected_objects)
        mixed = len(uv_groups) > 1
        if act is not None and act.type == 'MESH':
            col.label(text=f"Reference: {act.name}", icon='OBJECT_DATA')
            if mixed:
                col.label(text="UV Map: mixed", icon='ERROR')
            else:
                uv = act.data.uv_layers.active
                col.label(text=f"UV Map: {uv.name if uv else 'None'}",
                          icon='GROUP_UVS' if uv else 'ERROR')
        else:
            col.label(text="Reference: no active mesh", icon='ERROR')

        # Warn when the selection doesn't share one active UV map, e.g. half
        # on Atlas_UV and half on Lightmap_UV.
        if mixed:
            box = layout.box()
            col = box.column(align=True)
            col.alert = True
            col.label(text="Active UV maps don't match", icon='ERROR')
            shown = list(uv_groups.items())[:MAX_UV_GROUPS_SHOWN]
            for name, obs in shown:
                col.label(text=f"{uv_map_label(name)}: {len(obs)} object{'s' if len(obs) != 1 else ''}")
            if len(uv_groups) > MAX_UV_GROUPS_SHOWN:
                col.label(text=f"...and {len(uv_groups) - MAX_UV_GROUPS_SHOWN} more")
            col.label(text="Make the same UV map active on all")


class VIEW3D_PT_camera_texel_density(CTD_PanelBase, Panel):
    bl_idname = "VIEW3D_PT_camera_texel_density"
    bl_space_type = 'VIEW_3D'


class IMAGE_PT_camera_texel_density(CTD_PanelBase, Panel):
    bl_idname = "IMAGE_PT_camera_texel_density"
    bl_space_type = 'IMAGE_EDITOR'


# -------------------------------------------------------------------------
# Registration
# -------------------------------------------------------------------------

classes = (
    CTD_Settings,
    UV_OT_camera_texel_density,
    VIEW3D_PT_camera_texel_density,
    IMAGE_PT_camera_texel_density,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.camera_texel_density = PointerProperty(type=CTD_Settings)


def unregister():
    del bpy.types.Scene.camera_texel_density
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
