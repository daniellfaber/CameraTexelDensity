# Camera Texel Density

A Blender 5.0+ add-on that scales the UVs of selected objects so that, seen through the scene's
active camera, every object shows the same on-screen checker density. Objects far from a fixed
camera get less UV space in the atlas, and objects close to it get more.

## Install

Blender > Edit > Preferences > Get Extensions > ⌄ (top right) > **Install from Disk…** and pick
`dist/camera_texel_density-1.0.0.zip`.

## Use

1. Set the scene camera (Scene Properties > Camera).
2. Select the mesh objects. Make the **reference** object active (it keeps its UVs unchanged).
3. Sidebar (N) > **UV Tools** > **Camera Texel Density**, in the 3D Viewport or UV Editor.
4. Choose the camera distance method and press **Optimize Texel Density**.

Works in Object Mode and (multi-object) Edit Mode. It never switches modes or changes the
selection, and it's undoable with Ctrl+Z.

| Option | Measures |
|---|---|
| **Nearest Point** (default) | Depth of the object's surface point nearest the camera, counting only the part inside the camera view |
| **Object Origin** | Depth of the object's origin |

Depth is always measured **along the camera's view direction**, not as straight-line distance. On-screen
size falls off as 1/depth, so objects near the edge of frame come out correct even with wide lenses.

## What it does

For each selected object:

```
ratio            = UV area / world-space surface area      (∝ texel density²)
normalization    = sqrt(ratio_active / ratio_object)
distance scale   = depth_active / depth_object
final UV scale   = normalization × distance scale
```

* **UV map:** only the *active* UV map is changed (the one highlighted in the UV Maps list and shown
  in the UV Editor), never the render (camera-icon) map unless it is also the active one. The
  panel and the report show which map was used.
* **Islands:** each UV island is scaled around its own bounding-box centre, so islands stay where you
  placed them. Nothing is moved, rotated or packed.
* **Areas:** world area uses Blender's triangulation in world space, which is correct for concave and
  non-planar n-gons and for non-uniform object scale. UV area uses the shoelace formula on each UV
  polygon, which is exact for concave polygons. Both are measured on the base mesh, before modifiers.

### Pressing the button again doesn't compound

After one run, every object's ratio is `ratio_active × (depth_active / depth_object)²`. Running again
gives a normalization of exactly `depth_object / depth_active`, which cancels the distance scale, so
the final scale is 1.0. The result depends only on the current geometry, camera and reference, so
no stored "original UVs" are needed. This also holds if you later make a different,
already-optimized object active. If you edit UVs by hand, the next run simply works from your new UVs.

### Special cases

| Situation | Behaviour |
|---|---|
| Objects sharing one mesh | Processed once, for the user closest to the camera, with a warning (one mesh can only hold one UV layout) |
| Mesh also used by unselected objects | Warning: their UVs change too |
| Object partly behind the camera (e.g. a floor) | Only the part inside the camera view counts |
| Object entirely out of shot | Uses its nearest point anyway, with a warning |
| Object at or behind the camera plane | Skipped, with a warning |
| Very close objects | Warning when the distance scale exceeds 10×; values are not clamped |
| Orthographic camera | Normalization only (distance doesn't change on-screen size), with a warning |
| No UV map / zero UV area / linked library mesh | Skipped, with a warning |

## Limitations

* **One scale per object.** An object that recedes a long way from the camera (a long wall, a
  floor) has correct density at its nearest visible point and progressively more than it needs further
  away. Splitting very deep objects gives tighter results.
* **Density, not distortion.** Uniform scaling can't fix stretched UVs. An object with non-uniform
  object scale and an unscaled unwrap (for example a plane scaled 3 × 0.15) gets the right *average*
  density but its checker stays stretched. Apply scale and re-unwrap first.
* **Surface orientation is ignored.** Faces seen at a grazing angle show fewer texels on screen in one
  direction. That's inherent to a single uniform scale per object.
* UVs may go outside 0–1 or overlap after scaling. Packing is left to you (pack with island
  scaling disabled, or the result is undone).

## Building the zip

```bash
blender -b --command extension build --source-dir camera_texel_density --output-dir dist
```
