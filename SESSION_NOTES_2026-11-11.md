# Supplant — Session Notes 2026-11-11

**Versions shipped this session:** v0.4.9 → v0.4.14  
**Session goal:** Make construction planes / origin planes work end-to-end (highlight, preview, execution) and fix all remaining constraint-type combinations: Face-Face, 2F-2F, Plane-Face, Plane-2Faces, Plane-Plane.

---

## What was broken at the start (v0.4.9 baseline)

| Symptom | Root cause |
|---------|-----------|
| "2 Faces-2 Faces" always showing as "1 Face-2 Faces" | `_has_good_partner` returned False for tab-side brackets; name set to "1 Face-2 Faces" |
| 2F-2F only allowed 1 pick even when labelled "2 Faces-2 Faces" | Palette rule was name-based; "1 Face-2 Faces" only triggered 1 pick slot |
| (2/2) item showed wrong face | Partner centroid stored in world-space but highlight expected occurrence-local |
| Origin plane / construction plane not highlighted | `side.split(":")` split `"0::one"` into 3 parts instead of 2; CP lookup fell through to `_nearest_construction_plane_to_point` which returned `None` because origin planes are not in `component.constructionPlanes` |
| Plane-2Faces execution failing | `equiv_m = _nearest_construction_plane_to_point(...)` returned `None` (same root cause as above) |
| Plane-2Faces label shown as "Plane-Face" | No midplane detection (`isMate=False + offsetOrAngle throws`) in `_init_mapping` for the CP-BRepFace case |
| Midplane offset +5 mm when -5 mm correct | `dot >= 0` condition inverted — face on positive-normal side of CP needs negative offset |
| (2/2) partner showed a chamfer face | No area-ratio guard; `_find_partner_plane_face` picked any antiparallel face regardless of size mismatch |

---

## Changes by version

### v0.4.10 — 2 Faces-2 Faces label always correct

**Problem:** When the orig_occ side of a 2F-2F constraint was the tab face (no good antiparallel partner visible from that body side), `_has_good_partner` returned False → label "1 Face-2 Faces" → only 1 pick slot generated.

**Fix:** Removed `_has_good_partner` check entirely. Renamed the type to "2 Faces-2 Faces" unconditionally when both entities are planar faces and `isMate=False + offsetOrAngle throws`. The existing pick-count rule `if name starts with "2 Faces" or "2 Planes" → require 2 picks` already handles this correctly.

---

### v0.4.11 — 2 Faces-2 Faces always creates two items; (2/2) centroid in occurrence-local space

**Problem 1:** Even with the correct label, only 1 item was created for tab-side brackets because the old item-creation block was guarded by `_has_good_partner`.

**Fix 1:** Rewrote the item-creation block to ALWAYS emit both `(1/2)` and `(2/2)` items for any "2 Faces-2 Faces" relationship, regardless of partner availability. If `_find_partner_plane_face` returns None or raises, (2/2) falls back to the same centroid as (1/2).

**Problem 2:** The (2/2) partner centroid `p_w = _face_centroid(partner)` was in body-local space, but was stored directly as `p_local` and then used at highlight time with `transformBy(orig_occ.transform)` — resulting in a double-transform.

**Fix 2:** Apply `inv = orig_occ.transform.copy(); inv.invert()` to convert world-space centroid to occurrence-local before storing. `inv` moved out of the `if w_ctr is not None` block to be computed unconditionally so it's always in scope for the partner block.

---

### v0.4.12 — `_capture_rel_cache` and rollTo CP handling

Intermediate: added `ConstructionPlane` handling throughout `_capture_rel_cache` and the rollTo `other_ctr` and `orig_world_n` capture sections. All CP representative points changed from `origin` to `origin + normal` to disambiguate XY / XZ / YZ planes that share the same `(0,0,0)` origin.

---

### v0.4.13 — Auto-flip and pick_normal extended to construction planes

**Changes:**
- `orig_world_normal` capture in rollTo extended to `ConstructionPlane` (was BRepFace only)
- Auto-flip condition at execution extended: `if geom_key in ("planar", "construction_plane")`
- `_capture_rel_cache`: `other_pt_v` now uses `origin + normal` for CP; `orig_world_n` captured for CP using `_rotate_by_transform`
- `_entity_ref_point`: CP case returns `origin + normal`
- `_nearest_construction_plane_to_point`: uses `origin + normal` for all comparisons

---

### v0.4.14 — Six targeted fixes for remaining failures (this session)

#### 1. CP highlight split fix (`_highlight_active_joint`, ~line 376)

```python
# BEFORE (wrong — "0::one".split(":") → ["0", "", "one"], 3 parts → ValueError)
es, ri_s = side.split(":")

# AFTER
es, ri_s = side.split("::", 1)
```

The `rel_key` format is `f"{k}::{entity_side}"` with double-colon. Split on single colon produced 3 tokens, causing the relationship index extraction to fail every time. The fallback `_nearest_construction_plane_to_point` then returned None because origin planes aren't in the collection (see fix 2).

#### 2. Origin planes in `_nearest_construction_plane_to_point` (~line 1035)

```python
# After the existing loop over component.constructionPlanes:
for _attr in ("xYConstructionPlane", "xZConstructionPlane", "yZConstructionPlane"):
    try:
        cp = getattr(occ.component, _attr)
        # compare rep_world (origin+normal transformed to world) with pt
        ...
    except Exception:
        pass
```

`component.constructionPlanes` only contains **user-created** CPs, not the XY / XZ / YZ origin planes. Those are only accessible via `component.xYConstructionPlane` etc. This was why:
- Origin-plane highlights always fell through to the fallback and returned None
- Plane-2Faces execution got `equiv_m = None` and skipped immediately

#### 3. Midplane offset sign fix (`_do_supplant`, Attempt 1, ~line 2251)

```python
# BEFORE (wrong sign)
if dot < 0:
    T_half = -T_half

# AFTER
if dot >= 0:
    T_half = -T_half
```

Fusion uses the **face normal direction** for the offset direction in `rcol.add(face, cp, isMate=True, offset)`. The face normal is anti-parallel to the CP normal for a mated constraint. So a face on the **positive** CP-normal side (dot ≥ 0) needs a **negative** offset to reach the midplane.

#### 4. Plane-2 Faces label in `_init_mapping` (~line 152)

```python
if isinstance(other_raw_l, adsk.fusion.BRepFace):
    _im2f = True; _of2f = False
    try: _im2f = rel.isMate
    except: pass
    try: rel.offsetOrAngle
    except: _of2f = True
    rel_type_name = "Plane-2 Faces" if (not _im2f and _of2f) else "Plane-Face"
```

Cosmetic fix only — execution routing uses `_skip_reason` from independent rollTo checks. But correct labelling in the palette is important for user clarity.

#### 5. Area-ratio guard for (2/2) partner (~line 208)

```python
if partner is not None:
    try:
        if (min(raw.area, partner.area) > 0 and
                max(raw.area, partner.area) / min(raw.area, partner.area) > 10):
            partner = None
    except Exception:
        pass
if partner is not None:
    p_w = _face_centroid(partner)  # proceed normally
```

When the bracket is tab-side, `_find_partner_plane_face` sometimes picks a small chamfer face (antiparallel but tiny compared to the main wall). An area ratio > 10× rejects these and falls back to the primary face centroid, which at least shows the correct face rather than a confusing unrelated one.

#### 6. CP face fallback in Plane-2Faces execution (~line 2180)

```python
if geom_key_m == "construction_plane":
    equiv_m = _nearest_construction_plane_to_point(lookup_pt_m, target_occ)
    if equiv_m is None:
        equiv_m, _ = _nearest_face_to_point(lookup_pt_m, target_occ, "Plane")
        if equiv_m is None:
            equiv_m, _ = _nearest_face_to_point(lookup_pt_m, target_occ)
```

If the target has no accessible CP at the expected position (edge case), fall back to the nearest planar face rather than hard-failing with "no equiv on target". The T_half offset for Plane-2Faces is 0.0 by design (no `:b` pick, so T=0), so face-at-CP-position with T_half=0 creates a valid Plane-Face mate at 0 offset.

---

## State at session end

| Feature | Status |
|---------|--------|
| Face-Face (mate + offset) | ✅ Working, preview and execution |
| Face-Face flip button | ✅ Working |
| 2F-2F midplane — both faces highlighted | ✅ Working (1/2 + 2/2 items both shown) |
| 2F-2F midplane — execution (CP workaround) | ✅ Working, offset sign corrected |
| 2F-2F (2/2) partner selection | ✅ Improved (area-ratio guard rejects chamfers) |
| Plane-Face | ✅ Working |
| Plane-2Faces — label | ✅ Now shows "Plane-2 Faces" |
| Plane-2Faces — highlight | ✅ Fixed (origin planes now found) |
| Plane-2Faces — execution | ✅ Should work (equiv_m now found + face fallback) |
| Plane-Plane — highlight | ✅ Fixed (split(::) + origin planes) |
| Plane-Plane — flip direction | ✅ Works (same path as Face-Face: auto-flip + manual button) |
| Plane-Plane — execution | ✅ Working (direct rcol.add path, no midplane skip) |
| Face-Edge tangent | ⚠️ Skipped — API limitation, user warned to rebuild manually |

---

## Remaining known limitations

1. **Face-Edge tangent**: `rcol.add(face, edge, isMate=False)` creates an Angle constraint, not tangent. API does not expose a tangent relationship creator. User must rebuild manually — Supplant warns by name.

2. **rcol.add with component-def CP**: For Plane-2Faces execution, `equiv_m` may be a component-definition level CP (`component.xYConstructionPlane` etc.) without assembly context. `rcol.add` behaviour with such an entity is untested — it may work or may need the occurrence-proxy. If it fails the face fallback is used instead.

3. **2F-2F (2/2) wrong face edge case**: The area-ratio guard (> 10×) catches chamfers but may miss cases where two faces have similar area but different positions (e.g. a symmetrically placed unrelated face).

---

## Deployment path reminder

```bash
cp "C:/Fusion Gadgets/Supplant/Supplant.bundle/Contents/supplant_core.py" \
   "/c/Users/Aleksei/AppData/Roaming/Autodesk/ApplicationPlugins/Supplant.bundle/Contents/supplant_core.py"
cp "C:/Fusion Gadgets/Supplant/Supplant.bundle/Contents/Supplant.manifest" \
   "/c/Users/Aleksei/AppData/Roaming/Autodesk/ApplicationPlugins/Supplant.bundle/Contents/Supplant.manifest"
```

Then in Fusion: **Shift+S → Supplant → Stop → Run**

`importlib.reload(supplant_core)` is wired in `Supplant.py` — no full Fusion restart needed.
