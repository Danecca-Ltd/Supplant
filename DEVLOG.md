# Supplant — Development Log

## Current version: 0.4.18

**Source:** `C:\Fusion Gadgets\Supplant\Supplant.bundle\Contents\`  
**Deployed to:** `C:\Users\Aleksei\AppData\Roaming\Autodesk\ApplicationPlugins\Supplant.bundle\Contents\`  
*(NOT `…API\AddIns\` — that path is never loaded by Fusion 360.)*

After editing source, deploy both files and reload in Fusion:
```
copy supplant_core.py + Supplant.manifest to ApplicationPlugins path
```
Reload without restarting via MCP (`http://127.0.0.1:27182/mcp`):
```json
{"featureType":"script","object":{"script":"import sys,importlib\ndef run(_context):\n sc=sys.modules.get('supplant_core')\n if sc: importlib.reload(sc); print('reloaded:'+sc._read_version())\n else: print('not loaded')\n"}}
```

---

## Version history

### 0.4.18 — 2F-2F (2/2) row restored unconditionally; no_preview clears highlight (2026-11-12)

**Bug fixed:**

1. **2F-2F palette showed only (1/2) row** (`_init_mapping`): v0.4.16 suppressed the (2/2) row entirely when `_find_partner_plane_face` returns None. This was overly aggressive — execution still needs the (2/2) pick to measure tab thickness T for the midplane offset. Without T, the offset is 0, which mispositons the replacement. **Fix:** Always emit the (2/2) row. When a partner face exists, `ctr_key="planar"` and the highlight shows the partner face as before. When no partner exists, `ctr_key="no_preview"` — `_highlight_active_joint` now short-circuits on that key with `ui.activeSelections.clear()` and returns True. The highlight clears (no confusingly repeated face) while the row stays in the palette so the user can still pick.

---

### 0.4.17 — Third+ constraints in separate features (2026-11-12)

**Change:**

1. **Constraint solver "conflicting constraints" on 3rd relationship** (`_rebuild_constraint_set`): When more than 2 geometric relationships are bundled in one `AssemblyConstraint` feature, Fusion's solver marks the feature as "conflicting constraints" even when the same relationships created manually succeed. Workaround: rels 0 and 1 go into one shared feature; each additional rel (3rd, 4th, …) gets its own separate `AssemblyConstraint` feature.

**Known remaining issue:** The 3rd+ constraint feature(s) can still fail to compute even as separate features, because all constraints are created at the end of the timeline in a single script run. Fusion's solver applies them all at once; without the design "settling" after constraints 1+2, the solver can't converge for constraint 3. The same constraint created manually after supplant completes works because the design is already positioned by the first two constraints. This is a Fusion API solver sequencing defect.

---

### 0.4.16 — 2F-2F (2/2) suppressed when no slot partner; CP highlight on correct component (2026-11-12)

**Bugs fixed:**

1. **2F-2F (1/2) and (2/2) rows both highlighted the same face** (`_init_mapping`): When `_find_partner_plane_face` returns None (component has no antiparallel face in the constrained direction, e.g., a solid tab with one flat face), the code set `p_local = local_ctr` and still appended a (2/2) row. That row had an identical centroid to (1/2), so `_nearest_face_to_point` returned the same face for both clicks. **Fix:** Only append the (2/2) row when `partner is not None`. When no slot partner exists the constraint is a single-wall midplane and only one pick is needed; the (2/2) row was confusing and its pick was already skipped in execution (the `:b` execution path is skipped for `isMate=False + offsetOrAngle throws` relationships).

2. **CP highlight showed on the replacement component (repl_occ) instead of orig_occ** (`_nearest_construction_plane_to_point`): The fallback CP search iterated over `occ.component.constructionPlanes` and returned a **definition-level** `ConstructionPlane` object (no assembly context). `ui.activeSelections.add(cp_def)` renders the CP at the component's local origin in world space (always world (0,0,0) unless the component is translated), which visually landed on whichever occurrence happened to sit at that world position. When repl_occ was at (0,0,0), the highlight appeared on repl_occ instead of orig_occ. **Fix:** At the end of `_nearest_construction_plane_to_point`, call `best.createForAssemblyContext(occ)` to convert the found CP into an assembly-context proxy before returning it. The proxy carries the occurrence transform, so `activeSelections.add` correctly highlights it on orig_occ at its actual world position.

**Investigation — Supplant-created vs manually-created identical constraint (not a code bug):**

Extended MCP diagnostics (`diag11_specific.py`, `diag12_native.py`) confirmed the two constraints are **structurally identical**: same relationship count, same entity types (Face-Face, Face-CP, CP-CP), same `isMate=True`, `isFlipped=False`, same offsets (0, −0.5, 0). Different entity origins just reflect different timeline positions. The manual constraint "computes" and the add-in one "fails with conflicting constraints" despite identical structure — this appears to be a Fusion API defect. Worth reporting to Autodesk support with a minimal reproducible case: two identical designs, two constraints created differently, one computes and one doesn't.

---

### 0.4.15 — CP highlight variable swap; 2F-2F individual preview (2026-11-11)

**Bugs fixed:**

1. **CP highlight shows on wrong component** (`_highlight_active_joint`): `es, ri_s = side.split("::", 1)` had the two variables swapped. `side = "2::one"` → `es="2"` (rel index), `ri_s="one"` (entity side). `int("one")` always raised `ValueError`, so the entity was never extracted from the relationship and the fallback `_nearest_construction_plane_to_point` ran. That fallback returns the nearest CP on `orig_occ` by centroid distance, which was correct but landed visually on the wrong component because both components sat at the same position. **Fix:** `ri_s, es = side.split("::", 1)` — now `int(ri_s)` correctly gets the relationship index and `es` correctly selects entityOne/entityTwo. The CP is extracted directly from the relationship handle with its proper assembly context.

2. **2F-2F individual preview — both faces highlighted simultaneously**: `_highlight_active_joint` appended the partner face to `activeSelections` whenever any row with "2 Faces" in its label was clicked. The user wanted only the clicked row's face highlighted. **Fix:** Removed the entire "also highlight partner" block (lines 403–424 in v0.4.14). Each row now highlights exactly its own face.

**Investigation — Constraint health=1 / OK greyed (not a code bug):**

Extended MCP diagnostics confirmed:
- `Constraint Set 4 (1)` has `healthState=1` (WARNING) with empty healthMessage.
- Neither `0-TEST REPLACE:1` nor the busbar `6H8F CELL BUSBAR:1` are grounded.
- Suppress/unsuppress cycling doesn't change health.
- **A fresh standalone Face-Face constraint between target and grounded busbar has health=0 and positions the target correctly** (target moved Δz=1.2 as expected).
- **A fresh constraint on the same target with ungrounded busbar has health=1 and doesn't move anything.**
- Conclusion: Fusion's assembly constraint solver requires at least one grounded reference in the constraint chain. In a production design where the reference part is grounded (or constrained to other grounded parts), Supplant's constraints should work correctly with health=0.
- **OK greyed in Edit Constraints**: This is documented Fusion API behavior (see v0.3.5 entry) — API-created constraints cannot be edited via the Edit Constraints dialog. This is expected and unrelated to whether the constraint is functioning.

---

### 0.4.14 — Six targeted fixes: CP highlight, origin planes, offset sign, Plane-2Faces (2026-11-11)

**Bugs fixed:**

1. **CP highlight split** (`_highlight_active_joint`): `side.split(":")` on `"0::one"` produced 3 tokens; changed to `side.split("::", 1)`. This was why every construction-plane row showed "Could not highlight" — the relationship index was never extracted, the fallback returned None.

2. **Origin planes not found** (`_nearest_construction_plane_to_point`): `component.constructionPlanes` only contains user-created CPs, not XY/XZ/YZ origin planes. Added a secondary search over `component.xYConstructionPlane`, `xZConstructionPlane`, `yZConstructionPlane`. Fixes both origin-plane highlight and Plane-2Faces execution (which was getting `equiv_m = None` and skipping).

3. **Midplane offset sign** (`_do_supplant` Attempt 1): `if dot < 0: T_half = -T_half` → `if dot >= 0: T_half = -T_half`. Fusion's `rcol.add(face, cp, isMate=True, offset)` uses face-normal direction for offset; face normal is anti-parallel to CP normal in a mated constraint, so face on positive-normal side of CP needs a negative offset.

4. **Plane-2 Faces label** (`_init_mapping`): When a CP is paired with a BRepFace and `isMate=False + offsetOrAngle throws`, label is now "Plane-2 Faces" instead of "Plane-Face". Cosmetic only — execution routing uses independent rollTo checks.

5. **Wrong (2/2) partner face**: Added area-ratio guard in the 2F-2F item-creation block: if `max(raw.area, partner.area) / min(raw.area, partner.area) > 10`, reject the partner and fall back to the primary face centroid. Prevents chamfer faces being shown as the second slot wall.

6. **Plane-2Faces execution fallback**: When `geom_key_m == "construction_plane"` and `_nearest_construction_plane_to_point` returns None, fall back to `_nearest_face_to_point` so the constraint is never hard-skipped due to a missing CP equivalent.

---

### 0.4.13 — Auto-flip and pick_normal extended to construction planes (2026-11-11)

- `orig_world_normal` capture in rollTo extended to `ConstructionPlane` (was BRepFace only); uses `_rotate_by_transform` on `orig_ctx.transform`.
- Auto-flip condition at execution: `if geom_key in ("planar", "construction_plane")`.
- `_capture_rel_cache`: `other_pt_v` for CP uses `origin + normal`; `orig_world_n` captured for CP.
- `_entity_ref_point`: CP returns `origin + normal`.
- `_nearest_construction_plane_to_point`: all comparisons use `origin + normal` representative point.

---

### 0.4.11–0.4.12 — 2F-2F always two items; (2/2) centroid in occurrence-local space (2026-11-11)

**Problem 1:** Tab-side brackets caused `_has_good_partner` to return False → label "1 Face-2 Faces" → only 1 pick slot.

**Fix 1:** Removed `_has_good_partner` check entirely. Label is "2 Faces-2 Faces" unconditionally when both entities are planar and `isMate=False + offsetOrAngle throws`. Both (1/2) and (2/2) items are always emitted; (2/2) uses primary centroid as fallback if partner is unavailable.

**Problem 2:** Partner centroid `_face_centroid(partner)` returns occurrence-local coordinates, but was stored directly then multiplied by `orig_occ.transform` at highlight time — wrong world position.

**Fix 2:** Apply `inv = orig_occ.transform.copy(); inv.invert()` unconditionally before the partner block (was inside `if w_ctr is not None`), then `p_local.transformBy(inv)` to store correctly in occurrence-local space.

Also added `ConstructionPlane` handling to `_capture_rel_cache` and rollTo `other_ctr` section; all CP representative points use `origin + normal` to distinguish XY/XZ/YZ.

---

### 0.4.10 — "2 Faces-2 Faces" label always applied (2026-11-11)

**Problem:** When the orig_occ side is the tab face (narrow body with no good antiparallel partner), the `_has_good_partner` check returned False and the constraint was labelled "1 Face-2 Faces", which only allocated one pick slot in the palette.

**Fix:** Renamed the type to "2 Faces-2 Faces" unconditionally when both entities are planar faces and `isMate=False + offsetOrAngle throws`. The existing palette rule (name starting with "2 Faces" or "2 Planes" → require two picks) handles the rest.

---

### 0.4.8–0.4.9 — Perpendicular-distance partner fix; importlib reload (2026-11-11, earlier session)

- `_find_partner_plane_face`: switched from centroid distance to **perpendicular plane distance** for partner selection. The parallel slot wall and the tab face can share a similar centroid distance but have vastly different perpendicular distances — perpendicular is unambiguous.
- Added `importlib.reload(supplant_core)` to `Supplant.py` so Stop → Run picks up edits without full Fusion restart.
- Reverted tangent Face-Edge substitution (attempted Face-Face mate was geometrically incorrect); tangent cases now cleanly skip with a user warning to rebuild manually.

---

### 0.4.4 — 2F-2F second-face highlight fix

**Bug fixed:** The "2 Faces-2 Faces (2/2)" highlight was showing the wrong face.

**Root cause:** Double-transform bug in `_init_mapping`. The partner centroid `pw` from `_face_centroid(partner)` is in body-local space. The old code then applied `inv(orig_occ.transform)` to convert what it thought was world space — but `pw` was already body-local, so the transform was wrong. At highlight time, `orig_occ.transform` was applied, which cancelled the bogus inverse and left `pw` (body-local) being passed to `_nearest_face_to_point` as if it were world coords — finding the wrong face entirely.

**Fix:** Store `p_ctr = _face_centroid(partner)` directly (body-local), consistent with how `local_ctr` for the 1/2 item is stored. Removed the `inv2 = orig_occ.transform.copy(); inv2.invert(); p_ctr.transformBy(inv2)` block.

**Also improved:** `_find_partner_plane_face` now requires the partner face area to be within 5× of the first face's area. This prevents the partner search from picking small chamfers or large unrelated parallel faces.

---

### 0.4.3 — Tangent Face-Edge correctly recognized

**Bug fixed:** `_init_mapping` was labeling Face-Edge tangent constraints as "2 Faces-2 Faces" when the orig_occ side is a face. Root cause: the `isMate=False + offsetOrAngle throws` path always set `rel_type_name = "2 Faces-2 Faces"` without checking whether the other entity was actually a face or an edge.

**Fixes:**
1. In `_init_mapping` `if not is_mate:` block: added `isinstance(other_raw_l, adsk.fusion.BRepFace)` check — only sets "2 Faces-2 Faces" when both sides are faces; otherwise "Face-Edge tangent".
2. In `_init_mapping` `elif isinstance(raw, adsk.fusion.BRepEdge):` block: sets "Face-Edge tangent" when `isMate=False`, "Edge" otherwise.
3. In midplane workaround loop: tangent rows now short-circuit immediately with a clear `skipped` message instead of running the face-based workaround code on edge geometry.

Result: tangent constraints show correctly in the palette (single pick item, not a 1/2 + 2/2 split), and the final report clearly states they cannot be created via API and must be rebuilt manually.

---

### 0.4.2 — Midplane CP approach offset fix

Attempt 1 (construction plane) was using `offset=0` when constraining the tab face to the slot midplane CP. This put the tab FACE at the midplane (not the tab's center). Fixed to compute T (tab thickness from `:b` pick) upfront and use `offset = T/2`.

---

### 0.4.1 — Midplane workaround + grounding fix

**Midplane workaround**: 2F-2F midplane cases (previously skipped entirely) now go through two workaround attempts:
1. **Root-level construction plane** — `comp_root.constructionPlanes` gets a new plane at `W/2` offset from one slot wall (computed from face normal direction). The target face is then constrained face-mate to this CP. Parametric: the CP stays linked to the slot wall, so it updates if the slot moves. No Edit In Place required.
2. **Offset computation fallback** — if the CP approach fails (e.g. API rejects ConstructionPlane in `rcol.add`), computes `(W-T)/2` where W = slot width and T = tab thickness (from the two tab-face picks), and creates a plain face-mate offset constraint. Approximate but functional.

A warning dialog lists any constraint where a workaround was applied: *"Fix manually if the centering is off, or proceed at your own risk."*

**Grounding fix**: `isGrounded` on the replacement is now set **after** `deleteMe()` on the original. Previously it was set while both were present, which caused the grounding to either conflict or silently fail.

---

### 0.4.0 — Live preview + per-relationship Flip button

**Live preview**: After each face pick, `_build_preview()` creates a temporary `SUPPLANT_PREVIEW_<name>` assembly constraint in Fusion. The viewport immediately shows where the replacement component will end up. These constraints are deleted automatically before Execute runs.

**Data pre-caching**: `_capture_rel_cache(rel, entity_side, orig_raw)` is now called at `_init_mapping` time for every constraint relationship (while already rolled to the constraint). It captures `other_occ`, `other_pt`, `other_key`, `is_mate`, `offset_val`, `is_flipped`, `skip_api`, and `orig_world_normal` and stores them as `all_items[6]`. This lets `_build_preview` construct preview constraints without any additional rollTo calls.

**Flip button**: Each mapped relationship row in the palette now shows a "⇅" button. Clicking it:
  1. Sends `toggle_flip` to Python
  2. Python XORs `done_entry[6]` (flip_override)
  3. Python calls `_build_preview()` → viewport updates immediately
  4. Python sends `flip_toggled` to JS → button turns amber
  5. When Execute runs, the flip_override is XORed on top of auto-flip

**`done_entry` is now a 7-tuple**: `(source_pt, geom_key, side, label, pick_transform, pick_normal, flip_override)`.

**`all_items` for constraint rels is now a 7-tuple**: `(ac, rel_key, label, local_ctr, ctr_key, rel_type_name, rel_cache)`. `:b` items get `None` for `rel_cache`.

### 0.3.9 — Auto flip correction
- Added `_rotate_by_transform` and `_face_world_normal` helpers.
- At **pick time**: capture world-space normal of picked face (`done_entry[5] = pick_normal`).
- At **rollTo time**: capture world-space normal of original face on orig_occ (`rel_rows[11] = orig_world_normal`).
- At **execution time**: if `dot(pick_normal, orig_world_normal) < 0` (antiparallel), invert `is_flipped` before calling `rcol.add`. This auto-corrects the most common flip error where the replacement face is oriented opposite to the original.
- **Status:** Works for the common case. Two constraints may still need manual flip — see "Next up" below.

### 0.3.8 — Tangent Face-Edge detection
- Tangent Face-Edge constraints have `isMate=False` and `offsetOrAngle` throws, same as midplane.
- Added second condition: `not is_mate and _offset_threw and (geom_key == "edge" or other_key == "edge")` → skip as tangent.
- Both midplane and tangent-edge are now skipped and listed in the end-of-run warning.

### 0.3.7 — Geometry-type filtering + single warning box
- **`_nearest_face_to_point(pt, occ, geom_filter=None)`**: added `geom_filter` parameter. When `other_key == "cylinder"`, passes `"Cylinder"` filter so the centroid search only considers Cylinder faces. Prevents Coaxial relationships from being rebuilt as Face-Face when a nearby flat face has a similar centroid. Falls back to any face if no typed match is found.
- Applies the same type-filter to the target_occ (equiv) lookup using `geom_key`.
- **Phase B3 simplified**: replaced the per-constraint dialog loop with a single warning `messageBox` listing all affected constraint names. No more auto-opening `DcEditAssemblyMateCmd`.
- Midplane detection tightened: `not is_mate and _offset_threw and geom_key == "planar" and other_key == "planar"` — prevents false positives on tangent Face-Edge (which also has no offset but isn't planar+planar).

### 0.3.6 — Coordinate-space fix for other_occ centroid lookup
- **Root cause discovered:** `_face_centroid(rel.entityTwo)` returns coordinates in native component space (body coordinates), NOT world space. `_nearest_face_to_point` iterates `occ.bRepBodies` whose `boundingBox` IS in world space. Direct comparison was near-random for components with non-identity transforms.
- **Fix:** `adj_ctr.transformBy(other_occ.transform)` converts native centroid to world space before passing to `_nearest_face_to_point`.
- Same fix applied to partner_rows execution.
- Correct deployment path discovered via MCP diagnostic (`sc.__file__`).

### 0.3.5 — Midplane interactive dialog (earlier approach, now replaced)
- Tried auto-opening `DcEditAssemblyMateCmd` for each midplane constraint.
- Discovered: Fusion won't allow adding relationships to API-created constraints via the Edit dialog (OK button greyed out regardless of what geometry is selected).
- Replaced in 0.3.7 with the single warning box approach.

### 0.3.4 / earlier
- 2F-2F midplane detection added.
- `rcol.add(face, face, False, 0)` creates an Angle (0°) constraint, NOT a midplane. API limitation confirmed.

---

## Known Fusion API limitations

### 1. Midplane (2 Faces–2 Faces) cannot be created via API
`GeometricRelationshipCollection.add(face, face, isMate=False, 0)` creates an **Angle** constraint (0°), not a midplane. Supplant detects this case (both entities planar, `isMate=False`, `offsetOrAngle` throws) and skips it. A warning is shown at the end listing affected constraints. The user must double-click the constraint in the timeline and add the midplane relationship manually.

**Further issue:** Fusion does not allow adding new relationships to constraints that were created via the API (OK button is greyed out in the Edit Constraints dialog). This means the user must delete the API-created constraint and recreate it entirely by hand.

**Forum post / API request:** The API needs:
1. A way to create midplane (2F-2F) relationships via `rcol.add`.
2. Constraints created programmatically should be fully editable in the Edit Constraints dialog.

### 2. Tangent Face-Edge cannot be created via API
`rcol.add(face, edge, isMate=False, 0)` creates an **Angle** constraint, not tangent. Supplant detects and skips these too (same detection: `isMate=False` + `offsetOrAngle` throws + one side is an edge).

### 3. Flip direction is not always auto-correctable
The auto-flip in 0.3.9 handles the most common case (replacement component has an equivalent face with opposite world normal). It does NOT handle cases where:
- The component was inserted with a rotation that makes normals appear parallel/antiparallel unexpectedly.
- Both components have the same face orientation but the constraint intent differs.

---

## Technical notes

### Coordinate spaces
- `_face_centroid(face)` on a face from `bRepBodies` → world space.
- `_face_centroid(rel.entityOne/Two)` on a relationship entity → native body space.
- `_face_world_normal(face, occ)` → world-space normal (apply `occ.transform` rotation).
- Always apply `other_occ.transform` to convert other-side centroids before searching.

### MCP server
- Endpoint: `http://127.0.0.1:27182/mcp`
- Must send `notifications/initialized` after `initialize` before any `tools/call`.
- Script tool: `fusion_mcp_execute` with `featureType: "script"`.

### `_pt_via_local` usage
- Used to convert `source_pt` (world space at pick time) through the component's local coordinate space to handle timeline rollTo position changes.
- Required because `target_occ.transform` changes as the timeline moves.

### `is_flipped` after `rcol.add`
- `rcol.add(e1, e2, isMate, offset)` returns a `GeometricConstraintRelationship`.
- Set `gr.isFlipped = is_flipped` immediately after to apply the flip.
- Auto-flip (0.3.9) adjusts `is_flipped` BEFORE calling `rcol.add`.
- Manual flip override (planned) will XOR on top of auto-flip.
