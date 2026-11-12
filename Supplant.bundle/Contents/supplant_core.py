"""Supplant command — core logic (v0.4.9 — HTML palette UI).

Workflow:
  1. Click Supplant button → setup dialog to select original and replacement.
  2. On OK, the HTML palette opens showing all joint references.
  3. Click a joint row to highlight the original geometry in the viewport.
  4. Click Pick → on a row to start a pick sub-command; select matching
     face or edge on the replacement, then click OK.
  5. When all joints are mapped, click Execute Supplant in the palette footer.
"""

from __future__ import annotations

import json
import os
import traceback
from typing import Optional

import math

import adsk.core
import adsk.fusion

# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

COMMAND_SETUP_ID    = "Supplant_Setup"
COMMAND_PICK_ID     = "Supplant_Pick"
BATCHINSERT_ID      = "BatchInsert_Command"
PALETTE_ID          = "Supplant_Palette"
PALETTE_TITLE       = "Supplant"
AUTO_EXEC_EVENT_ID  = "supplant_pick_autoexec_v1"


class _AutoExecEventHandler(adsk.core.CustomEventHandler):
    """Deferred doExecute — called on next event loop tick from a custom event."""
    def notify(self, args):
        cmd = _mapping.pop("_pick_cmd_pending", None)
        if cmd:
            try:
                cmd.doExecute(False)
            except Exception:
                pass


_auto_exec_handler = _AutoExecEventHandler()

INPUT_ORIGINAL    = "sp_original"
INPUT_REPLACEMENT = "sp_replacement"
INPUT_PICK_GEOM   = "sp_pick_geom"

_retained: list = []
_palette = None   # active palette instance
_ph      = None   # _PaletteHTMLHandler for the current session (avoid duplicates)

# ---------------------------------------------------------------------------
# Mapping state
# ---------------------------------------------------------------------------

_mapping: dict = {}


def _init_mapping(orig_occ, repl_occ, refs: dict) -> None:
    _mapping.clear()
    _mapping["orig_occ"]      = orig_occ
    _mapping["repl_occ"]      = repl_occ
    _mapping["refs"]          = refs
    _mapping["picking_token"] = None
    items = []
    for joint, side in refs["joints"]:
        try:
            other      = joint.occurrenceTwo if side == "one" else joint.occurrenceOne
            other_name = other.name if other else "other"
        except Exception:
            other_name = "other"
        label = f"{joint.name}  →  {other_name}"
        items.append((joint, side, label))
    for ac, _side in refs.get("assembly_constraints", []):
        try:
            ac.timelineObject.rollTo(True)
            rels = ac.geometricRelationships
            for k in range(rels.count):
                rel = rels.item(k)
                for entity_side in ("one", "two"):
                    raw = rel.entityOne if entity_side == "one" else rel.entityTwo
                    if raw is None:
                        continue
                    try:
                        if not _entity_in_occurrence(raw, orig_occ):
                            continue
                    except Exception:
                        continue
                    other_name = "other"
                    try:
                        other_raw = rel.entityTwo if entity_side == "one" else rel.entityOne
                        if other_raw is not None:
                            ctx = other_raw.assemblyContext
                            if ctx is not None:
                                other_name = ctx.name
                    except Exception:
                        pass
                    rel_key = f"{k}::{entity_side}"
                    label   = f"{ac.name} [rel {k + 1}]  →  {other_name}"
                    # Relationship type name
                    rel_type_name = "Reference"
                    try:
                        is_mate = True
                        try:
                            is_mate = rel.isMate
                        except Exception:
                            pass
                        if isinstance(raw, adsk.fusion.BRepFace):
                            raw_cls = type(raw.geometry).__name__
                            other_raw_l = rel.entityTwo if entity_side == "one" else rel.entityOne
                            other_cls = (type(other_raw_l.geometry).__name__
                                         if isinstance(other_raw_l, adsk.fusion.BRepFace) else "")
                            if not is_mate:
                                # isMate=False: midplane (2F-2F), angle, or Face-Edge tangent
                                off_v = None
                                try:
                                    p = rel.offsetOrAngle
                                    if p is not None:
                                        off_v = p.value
                                except Exception:
                                    off_v = None
                                if off_v is None or abs(off_v) < 1e-10:
                                    if isinstance(other_raw_l, adsk.fusion.BRepFace):
                                        rel_type_name = "2 Faces-2 Faces"
                                    else:
                                        rel_type_name = "Face-Edge tangent"
                                else:
                                    deg = abs(off_v) * 180.0 / math.pi
                                    rel_type_name = f"Face-Face (angle {deg:.0f}°)"
                            elif "Cylinder" in (raw_cls, other_cls):
                                rel_type_name = "Coaxial"
                            elif other_cls:
                                off_v = 0.0
                                try:
                                    p = rel.offsetOrAngle
                                    if p is not None:
                                        off_v = p.value
                                except Exception:
                                    pass
                                rel_type_name = "Face-Face (offset)" if abs(off_v) > 1e-10 else "Face-Face"
                            else:
                                rel_type_name = "Face-Edge"
                        elif isinstance(raw, adsk.fusion.BRepEdge):
                            rel_type_name = "Face-Edge tangent" if not is_mate else "Edge"
                        elif isinstance(raw, adsk.fusion.ConstructionPlane):
                            other_raw_l = rel.entityTwo if entity_side == "one" else rel.entityOne
                            if isinstance(other_raw_l, adsk.fusion.BRepFace):
                                _im2f = True
                                _of2f = False
                                try: _im2f = rel.isMate
                                except: pass
                                try: rel.offsetOrAngle
                                except: _of2f = True
                                rel_type_name = "Plane-2 Faces" if (not _im2f and _of2f) else "Plane-Face"
                            elif isinstance(other_raw_l, adsk.fusion.ConstructionPlane):
                                rel_type_name = "Plane-Plane"
                            else:
                                rel_type_name = "Plane-Reference"
                        elif isinstance(raw, adsk.fusion.ConstructionAxis):
                            rel_type_name = "Axis-Reference"
                        elif isinstance(raw, adsk.fusion.ConstructionPoint):
                            rel_type_name = "Point-Reference"
                    except Exception:
                        pass
                    # Body-local centroid for highlight without rollTo at highlight time
                    local_ctr = None
                    ctr_key   = "planar"
                    inv = orig_occ.transform.copy()
                    inv.invert()
                    try:
                        if isinstance(raw, adsk.fusion.BRepFace):
                            w_ctr   = _face_centroid(raw)
                            ctr_key = "planar" if type(raw.geometry).__name__ in ("Plane",) else "cylinder"
                        elif isinstance(raw, adsk.fusion.BRepEdge):
                            w_ctr   = _edge_ref_point(raw)
                            ctr_key = "edge"
                        elif isinstance(raw, adsk.fusion.ConstructionPlane):
                            # Use origin+normal so XY/XZ/YZ planes (all sharing origin
                            # (0,0,0)) produce distinct representative points.
                            _cpg = raw.geometry
                            _n   = _cpg.normal
                            _o   = _cpg.origin
                            w_ctr = adsk.core.Point3D.create(
                                _o.x + _n.x, _o.y + _n.y, _o.z + _n.z)
                            ctr_key = "construction_plane"
                        elif isinstance(raw, adsk.fusion.ConstructionAxis):
                            w_ctr   = raw.geometry.origin
                            ctr_key = "construction_axis"
                        elif isinstance(raw, adsk.fusion.ConstructionPoint):
                            w_ctr   = raw.geometry.origin
                            ctr_key = "construction_point"
                        elif isinstance(raw, adsk.fusion.JointOrigin):
                            w_ctr   = raw.geometry.origin
                            ctr_key = "joint_origin"
                        else:
                            w_ctr = None
                        if w_ctr is not None:
                            local_ctr = adsk.core.Point3D.create(w_ctr.x, w_ctr.y, w_ctr.z)
                            local_ctr.transformBy(inv)
                    except Exception:
                        local_ctr = None
                    rel_cache = _capture_rel_cache(rel, entity_side, raw)
                    if rel_type_name == "2 Faces-2 Faces" and isinstance(raw, adsk.fusion.BRepFace):
                        items.append((ac, rel_key, label, local_ctr, ctr_key,
                                      "2 Faces-2 Faces (1/2)", rel_cache))
                        try:
                            # raw is an assembly-context proxy: its geometry/boundingBox
                            # are in world space, but body.faces geometry is component-local.
                            # Mixing spaces breaks the antiparallel dot-product check when
                            # orig_occ has any rotation. Use nativeObject so both the
                            # reference face and the candidate faces are component-local.
                            raw_def = getattr(raw, 'nativeObject', None) or raw
                            partner = _find_partner_plane_face(raw_def, raw_def.body)
                            if partner is not None:
                                try:
                                    if (min(raw_def.area, partner.area) > 0 and
                                            max(raw_def.area, partner.area) / min(raw_def.area, partner.area) > 10):
                                        partner = None
                                except Exception:
                                    pass
                            if partner is not None:
                                # partner is a native (component-local) face; its centroid
                                # is already in component-local space — the same space
                                # _highlight_active_joint expects for local_ctr, so no inv
                                # transform is needed here.
                                p_local = _face_centroid(partner)
                                if p_local is None:
                                    p_local = local_ctr
                                items.append((ac, rel_key + ":b", label, p_local, "planar",
                                              "2 Faces-2 Faces (2/2)", None))
                            else:
                                # No antiparallel partner on orig_occ (e.g. single-face tab).
                                # Still emit (2/2) — execution needs the pick to measure tab
                                # thickness T. "no_preview" clears the highlight on click
                                # instead of showing the same face as (1/2).
                                items.append((ac, rel_key + ":b", label, local_ctr, "no_preview",
                                              "2 Faces-2 Faces (2/2)", None))
                        except Exception:
                            items.append((ac, rel_key + ":b", label, local_ctr, "no_preview",
                                          "2 Faces-2 Faces (2/2)", None))
                    else:
                        items.append((ac, rel_key, label, local_ctr, ctr_key, rel_type_name, rel_cache))
        except Exception:
            pass
    # Roll back to end after any rollTo calls for constraints
    try:
        app = adsk.core.Application.get()
        des = adsk.fusion.Design.cast(app.activeProduct)
        des.timeline.markerPosition = des.timeline.count
    except Exception:
        pass
    _mapping["all_items"] = items
    _mapping["done"]      = {}  # token → (source_pt, geom_key, side, label, pick_transform)


def _item_token(item) -> str:
    """Return the done-dict key for an all_items entry.

    Joints use the joint entityToken directly.
    Constraint relationship rows use a synthetic key "{ac_token}::{rel_key}".
    """
    entity, side = item[0], item[1]
    if isinstance(entity, adsk.fusion.AssemblyConstraint):
        return f"{entity.entityToken}::{side}"
    return entity.entityToken


def _build_joint_data() -> list:
    """Return serialisable list for the HTML palette.

    Joints emit a flat {kind:"joint"} entry.
    Assembly constraints are grouped: one {kind:"constraint_group"} per constraint
    feature, with child {kind:"rel"} entries in its "items" list.
    """
    all_items = _mapping.get("all_items", [])
    done      = _mapping.get("done", {})
    result    = []
    group_idx: dict = {}  # ac_name → index in result

    for item in all_items:
        entity, side, label = item[0], item[1], item[2]
        tok     = _item_token(item)
        mapped  = tok in done
        mapped_geom = done[tok][1] if mapped else ""

        if isinstance(entity, adsk.fusion.AssemblyConstraint):
            ac_name       = entity.name
            rel_type_name = item[5] if len(item) > 5 else "Reference"
            parts      = label.split("  →  ")
            other_name = parts[1] if len(parts) > 1 else ""
            if ac_name not in group_idx:
                group_idx[ac_name] = len(result)
                result.append({
                    "kind":  "constraint_group",
                    "name":  ac_name,
                    "items": [],
                })
            grp         = result[group_idx[ac_name]]
            child_n     = len(grp["items"]) + 1
            done_e      = done.get(tok, ())
            flip_ov     = bool(done_e[6]) if len(done_e) > 6 and done_e[6] else False
            grp["items"].append({
                "kind":         "rel",
                "token":        tok,
                "name":         f"{child_n}. {rel_type_name}",
                "type":         "constrain",
                "other":        other_name,
                "mapped":       mapped,
                "mapped_geom":  mapped_geom,
                "flip_override": flip_ov,
            })
        else:
            geom      = entity.geometryOrOriginOne if side == "one" else entity.geometryOrOriginTwo
            gtype_str = "?"
            try:
                gtype = geom.geometryType
                JGT   = adsk.fusion.JointGeometryTypes
                if gtype == JGT.JointBRepEdgeGeometry:
                    gtype_str = "edge"
                elif gtype == JGT.JointPlanarBRepFaceGeometry:
                    gtype_str = "planar"
                elif gtype == JGT.JointNonPlanarBRepFaceGeometry:
                    gtype_str = "nonplanar"
            except Exception:
                pass
            parts      = label.split("  →  ")
            other_name = parts[1] if len(parts) > 1 else ""
            result.append({
                "kind":        "joint",
                "token":       tok,
                "name":        entity.name,
                "type":        gtype_str,
                "other":       other_name,
                "mapped":      mapped,
                "mapped_geom": mapped_geom,
            })
    return result


# ---------------------------------------------------------------------------
# Palette helpers
# ---------------------------------------------------------------------------

def _send_to_palette(action: str, data: dict) -> None:
    global _palette
    # _palette can be None if the closed event fired during a page reload
    # (Fusion sometimes fires closed/open around an in-place reload on restart).
    # Recover by looking the palette up — it's still visible in the UI.
    if _palette is None:
        try:
            _palette = (adsk.core.Application.get()
                        .userInterface.palettes.itemById(PALETTE_ID))
        except Exception:
            pass
    if _palette is not None:
        try:
            _palette.sendInfoToHTML(action, json.dumps(data))
        except Exception:
            pass


def _highlight_active_joint(token: str) -> bool:
    """Highlight the face/edge on orig_occ for the given joint or constraint token."""
    app      = adsk.core.Application.get()
    ui       = app.userInterface
    orig_occ = _mapping.get("orig_occ")
    if orig_occ is None:
        return False
    all_items = _mapping.get("all_items", [])

    found_item = next((item for item in all_items if _item_token(item) == token), None)
    if found_item is None:
        return False
    item_entity, side = found_item[0], found_item[1]

    if isinstance(item_entity, adsk.fusion.AssemblyConstraint):
        # Use body-local centroid captured at init time — no rollTo needed
        local_ctr = found_item[3] if len(found_item) > 3 else None
        ctr_key   = found_item[4] if len(found_item) > 4 else "planar"
        if local_ctr is None:
            return False
        if ctr_key == "no_preview":
            ui.activeSelections.clear()
            return True
        try:
            world_ctr = adsk.core.Point3D.create(local_ctr.x, local_ctr.y, local_ctr.z)
            world_ctr.transformBy(orig_occ.transform)
            if ctr_key == "edge":
                entity = _nearest_edge_to_point(world_ctr, orig_occ)
            elif ctr_key == "construction_plane":
                # component.constructionPlanes objects are component-definition level;
                # activeSelections.add needs assembly-context. Extract it from the
                # relationship directly — its handle IS in assembly context.
                entity = None
                try:
                    ri_s, es = side.split("::", 1)
                    rel_raw = item_entity.geometricRelationships.item(int(ri_s))
                    raw_cp = rel_raw.entityOne if es == "one" else rel_raw.entityTwo
                    if isinstance(raw_cp, adsk.fusion.ConstructionPlane):
                        entity = raw_cp
                except Exception:
                    pass
                if entity is None:
                    entity = _nearest_construction_plane_to_point(world_ctr, orig_occ)
            else:
                entity, _ = _nearest_face_to_point(world_ctr, orig_occ)
            ui.activeSelections.clear()
            if entity is not None:
                ui.activeSelections.add(entity)
                return True
        except Exception:
            pass
        return False

    # It's a Joint
    geom = item_entity.geometryOrOriginOne if side == "one" else item_entity.geometryOrOriginTwo
    if isinstance(geom, adsk.fusion.JointOrigin):
        try:
            geom = geom.geometry
        except Exception:
            pass
    origin = _geom_origin(geom)
    JGT    = adsk.fusion.JointGeometryTypes
    try:
        gtype = geom.geometryType
    except Exception:
        gtype = None
    entity = None
    if gtype == JGT.JointBRepEdgeGeometry:
        entity = _nearest_edge_to_point(origin, orig_occ)
    elif origin:
        entity, _ = _nearest_face_to_point(origin, orig_occ)
    try:
        ui.activeSelections.clear()
        if entity is not None:
            ui.activeSelections.add(entity)
            return True
    except Exception:
        pass
    return False


def _push_data_to_palette() -> None:
    """Send the current mapping data to the palette (idempotent)."""
    orig_name = repl_name = ""
    try:
        orig_name = _mapping.get("orig_occ").name
        repl_name = _mapping.get("repl_occ").name
    except Exception:
        pass
    refs = _mapping.get("refs", {})
    auto = (len(refs.get("as_built_joints", [])) +
            len(refs.get("rigid_groups",    [])) +
            len(refs.get("contact_sets",    [])))
    try:
        joints = _build_joint_data()
    except Exception:
        joints = []
    data = {
        "orig":   orig_name,
        "repl":   repl_name,
        "auto":   auto,
        "joints": joints,
    }
    # Write sidecar JSON so JS can fetch it on page load (belt-and-suspenders
    # for cases where sendInfoToHTML doesn't reach JS after Fusion restart).
    try:
        sidecar = os.path.join(os.path.dirname(__file__),
                               "resources", "supplant", "supplant_data.json")
        with open(sidecar, "w", encoding="utf-8") as _f:
            json.dump(data, _f)
    except Exception:
        pass
    _send_to_palette("data", data)


def _open_palette() -> None:
    global _palette, _ph
    app      = adsk.core.Application.get()
    ui       = app.userInterface
    palettes = ui.palettes
    _palette = palettes.itemById(PALETTE_ID)
    if _palette is None:
        html_path = os.path.join(
            os.path.dirname(__file__), "resources", "supplant", "index.html")
        html_url  = "file:///{}".format(html_path.replace("\\", "/"))
        _palette  = palettes.add(
            PALETTE_ID, PALETTE_TITLE, html_url,
            True, True, True, 320, 440, True)
        _ph = _PaletteHTMLHandler()
        _palette.incomingFromHTML.add(_ph)
        _retained.append(_ph)
        pc = _PaletteClosedHandler()
        _palette.closed.add(pc)
        _retained.append(pc)
    else:
        _palette.isVisible = True
        # Palette was restored by Fusion across a restart: the page reloads but
        # the previous session's Python handler is gone. Re-attach so we receive
        # the JS 'ready' event and can re-send data once the page finishes loading.
        if _ph is None:
            _ph = _PaletteHTMLHandler()
            _palette.incomingFromHTML.add(_ph)
            _retained.append(_ph)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _read_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(__file__), "Supplant.manifest")) as f:
            return json.load(f).get("version", "?")
    except Exception:
        return "?"


def _res_folder() -> str:
    return os.path.join(os.path.dirname(__file__), "resources", "Supplant")


def register(ui, panel, handlers: list) -> None:
    ver = _read_version()

    # Setup command (button in Assemble panel)
    cmd_def = ui.commandDefinitions.itemById(COMMAND_SETUP_ID)
    if cmd_def is None:
        cmd_def = ui.commandDefinitions.addButtonDefinition(
            COMMAND_SETUP_ID, f"Supplant v{ver}",
            f"v{ver} — Replace a component while manually redirecting all joint references.",
            _res_folder())
    h = _SetupCreatedHandler()
    cmd_def.commandCreated.add(h)
    handlers.append(h)
    _retained.append(h)
    ctrl = panel.controls.itemById(COMMAND_SETUP_ID)
    if ctrl is None:
        bi   = panel.controls.itemById(BATCHINSERT_ID)
        ctrl = panel.controls.addCommand(cmd_def, BATCHINSERT_ID, False) if bi \
               else panel.controls.addCommand(cmd_def)
    ctrl.isVisible = True

    # Pick command (internal — no button, launched programmatically)
    pick_def = ui.commandDefinitions.itemById(COMMAND_PICK_ID)
    if pick_def is None:
        pick_def = ui.commandDefinitions.addButtonDefinition(
            COMMAND_PICK_ID, "Supplant Pick", "Pick replacement geometry", "")
    ph = _PickCreatedHandler()
    pick_def.commandCreated.add(ph)
    handlers.append(ph)
    _retained.append(ph)

    # Custom event: defers doExecute out of InputChangedEventHandler context
    try:
        app_inst = adsk.core.Application.get()
        auto_evt = app_inst.registerCustomEvent(AUTO_EXEC_EVENT_ID)
        if auto_evt is not None:
            auto_evt.add(_auto_exec_handler)
            _retained.append(_auto_exec_handler)
    except Exception:
        pass


def unregister(ui, panel) -> None:
    global _palette, _ph
    _ph  = None
    ctrl = panel.controls.itemById(COMMAND_SETUP_ID)
    if ctrl:
        ctrl.deleteMe()
    for cid in (COMMAND_SETUP_ID, COMMAND_PICK_ID):
        cd = ui.commandDefinitions.itemById(cid)
        if cd:
            cd.deleteMe()
    if _palette is not None:
        try:
            _palette.deleteMe()
        except Exception:
            pass
        _palette = None
    try:
        adsk.core.Application.get().unregisterCustomEvent(AUTO_EXEC_EVENT_ID)
    except Exception:
        pass
    _retained.clear()


# ---------------------------------------------------------------------------
# Setup command handlers
# ---------------------------------------------------------------------------

class _SetupCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd    = args.command
            inputs = cmd.commandInputs

            orig_sel = inputs.addSelectionInput(
                INPUT_ORIGINAL, "Original", "Component to replace")
            orig_sel.addSelectionFilter(adsk.core.SelectionFilters.Occurrences)
            orig_sel.setSelectionLimits(1, 1)

            repl_sel = inputs.addSelectionInput(
                INPUT_REPLACEMENT, "Replacement",
                "Replacement (must already be in the assembly)")
            repl_sel.addSelectionFilter(adsk.core.SelectionFilters.Occurrences)
            repl_sel.setSelectionLimits(1, 1)

            vh = _SetupValidateHandler()
            cmd.validateInputs.add(vh)
            _retained.append(vh)

            xh = _SetupExecuteHandler()
            cmd.execute.add(xh)
            _retained.append(xh)
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                traceback.format_exc(), "Supplant startup error")


class _SetupValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            inputs  = args.inputs
            orig_in = inputs.itemById(INPUT_ORIGINAL)
            repl_in = inputs.itemById(INPUT_REPLACEMENT)
            if orig_in.selectionCount == 0 or repl_in.selectionCount == 0:
                args.areInputsValid = False
                return
            args.areInputsValid = not _same_occurrence(
                orig_in.selection(0).entity, repl_in.selection(0).entity)
        except Exception:
            args.areInputsValid = False


class _SetupExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            inputs   = args.command.commandInputs
            orig_occ = inputs.itemById(INPUT_ORIGINAL).selection(0).entity
            repl_occ = inputs.itemById(INPUT_REPLACEMENT).selection(0).entity
            design   = adsk.fusion.Design.cast(adsk.core.Application.get().activeProduct)
            refs     = _collect_references(orig_occ, design)
            _init_mapping(orig_occ, repl_occ, refs)
            _open_palette()
            # Proactively push data in case the palette was already open
            # and its 'ready' event won't fire again.
            _push_data_to_palette()
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                traceback.format_exc(), "Supplant execute error")


# ---------------------------------------------------------------------------
# Palette event handlers
# ---------------------------------------------------------------------------

class _PaletteHTMLHandler(adsk.core.HTMLEventHandler):
    def notify(self, args):
        try:
            action = args.action
            data   = {}
            try:
                data = json.loads(args.data or "{}")
            except Exception:
                pass

            if action == "ready":
                _push_data_to_palette()

            elif action == "set_active_joint":
                if not _highlight_active_joint(data.get("token", "")):
                    _send_to_palette("highlight_error", {})

            elif action == "start_pick":
                token    = data.get("token", "")
                _mapping["picking_token"] = token
                ui       = adsk.core.Application.get().userInterface
                pick_def = ui.commandDefinitions.itemById(COMMAND_PICK_ID)
                if pick_def:
                    pick_def.execute()
                else:
                    _mapping["picking_token"] = None
                    _send_to_palette("pick_error", {"message": "Pick command not registered."})

            elif action == "toggle_flip":
                token     = data.get("token", "")
                old_entry = _mapping.get("done", {}).get(token)
                if old_entry is not None:
                    entry_list = list(old_entry)
                    while len(entry_list) < 7:
                        entry_list.append(None)
                    entry_list[6] = not bool(entry_list[6])
                    _mapping["done"][token] = tuple(entry_list)
                    flipped = bool(entry_list[6])
                    _build_preview()
                    _send_to_palette("flip_toggled", {"token": token, "flipped": flipped})

            elif action == "execute":
                try:
                    app      = adsk.core.Application.get()
                    design   = adsk.fusion.Design.cast(app.activeProduct)
                    orig_occ = _mapping.get("orig_occ")
                    repl_occ = _mapping.get("repl_occ")
                    report   = _do_supplant(orig_occ, repl_occ, design)
                    app.userInterface.messageBox(report, "Supplant")
                    _send_to_palette("execute_result", {"report": report})
                except Exception as exc:
                    msg = traceback.format_exc()
                    adsk.core.Application.get().userInterface.messageBox(msg, "Supplant error")
                    _send_to_palette("execute_result", {"report": f"Error: {exc}"})

        except Exception:
            pass


class _PaletteClosedHandler(adsk.core.UserInterfaceGeneralEventHandler):
    def notify(self, args):
        global _palette
        _palette = None
        try:
            adsk.core.Application.get().userInterface.activeSelections.clear()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pick command handlers
# ---------------------------------------------------------------------------

class _PickCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            cmd      = args.command
            inputs   = cmd.commandInputs
            repl_occ = _mapping.get("repl_occ")
            hint     = repl_occ.name if repl_occ else "the replacement"

            mp = inputs.addSelectionInput(
                INPUT_PICK_GEOM,
                "Pick geometry",
                f"Select a face or edge on ‘{hint}’")
            for _sf in (
                adsk.core.SelectionFilters.Faces,
                "LinearEdges", "CircularEdges", "Edges",
                "Vertices", "SketchPoints",
                "ConstructionPlanes", "ConstructionLines", "ConstructionPoints",
                "JointOrigins",
            ):
                try:
                    mp.addSelectionFilter(_sf)
                except Exception:
                    pass
            mp.setSelectionLimits(1, 1)

            vh = _PickValidateHandler()
            cmd.validateInputs.add(vh)
            _retained.append(vh)

            xh = _PickExecuteHandler()
            cmd.execute.add(xh)
            _retained.append(xh)

            dh = _PickDestroyHandler()
            cmd.destroy.add(dh)
            _retained.append(dh)

            ich = _PickInputChangedHandler(cmd)
            cmd.inputChanged.add(ich)
            _retained.append(ich)

            if _palette is not None:
                _palette.isVisible = True
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                traceback.format_exc(), "Supplant pick error")


class _PickValidateHandler(adsk.core.ValidateInputsEventHandler):
    def notify(self, args):
        try:
            pick_in = args.inputs.itemById(INPUT_PICK_GEOM)
            if pick_in.selectionCount == 0:
                args.areInputsValid = False
                return
            entity = pick_in.selection(0).entity
            args.areInputsValid = _entity_in_occurrence(entity, _mapping.get("repl_occ"))
        except Exception:
            args.areInputsValid = False


class _PickExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            pick_in = args.command.commandInputs.itemById(INPUT_PICK_GEOM)
            entity  = pick_in.selection(0).entity
            ent, key = _entity_key(entity)
            if ent is None:
                _send_to_palette("pick_error", {"message": "Unsupported geometry type."})
                return

            # Store a stable Point3D rather than the entity object itself —
            # entity references are invalidated when the design is regenerated
            # (e.g. after addExistingComponent in _do_supplant step 1).
            source_pt = _entity_ref_point(ent, key)
            if source_pt is None:
                _send_to_palette("pick_error", {"message": "Could not get geometry reference point."})
                return

            token = _mapping.get("picking_token")
            if not token:
                _send_to_palette("pick_error", {"message": "No joint token active."})
                return

            all_items = _mapping.get("all_items", [])
            entry = next((item for item in all_items if _item_token(item) == token), None)
            if entry is None:
                _send_to_palette("pick_error", {"message": "Joint not found."})
                return

            joint, side, label = entry[0], entry[1], entry[2]
            # Also capture the supplanter's world transform at pick time so that
            # _do_supplant can convert source_pt through local space when the
            # supplanter moves in the timeline (changing its world position).
            pick_transform = None
            try:
                repl_occ = _mapping.get("repl_occ")
                if repl_occ is not None:
                    pick_transform = repl_occ.transform.copy()
            except Exception:
                pass
            # Capture face normal in world space at pick time for later flip correction.
            pick_normal = None
            try:
                if key != "edge" and pick_transform is not None:
                    g = ent.geometry
                    if isinstance(g, adsk.core.Plane):
                        pick_normal = _rotate_by_transform(g.normal, pick_transform)
            except Exception:
                pass
            _mapping["done"][token]   = (source_pt, key, side, label, pick_transform, pick_normal)
            _mapping["picking_token"] = None
            _build_preview()

            # Send pick result then full data refresh
            _send_to_palette("pick_result", {"token": token, "geom": key})
            orig_name = repl_name = ""
            try:
                orig_name = _mapping.get("orig_occ").name
                repl_name = _mapping.get("repl_occ").name
            except Exception:
                pass
            refs = _mapping.get("refs", {})
            auto = (len(refs.get("as_built_joints", [])) +
                    len(refs.get("rigid_groups",    [])) +
                    len(refs.get("contact_sets",    [])))
            _send_to_palette("data", {
                "orig":   orig_name,
                "repl":   repl_name,
                "auto":   auto,
                "joints": _build_joint_data(),
            })
        except Exception as exc:
            _mapping["picking_token"] = None
            _send_to_palette("pick_error", {"message": str(exc)})


class _PickDestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        # If picking_token is still set the user cancelled (execute did not run)
        if _mapping.get("picking_token"):
            _mapping["picking_token"] = None
            _send_to_palette("pick_cancel", {})


class _PickInputChangedHandler(adsk.core.InputChangedEventHandler):
    """Auto-confirm the pick dialog as soon as one valid geometry is selected."""
    def __init__(self, cmd):
        super().__init__()
        self._cmd = cmd

    def notify(self, args):
        try:
            pick_in = args.inputs.itemById(INPUT_PICK_GEOM)
            if pick_in and pick_in.selectionCount == 1:
                entity = pick_in.selection(0).entity
                if _entity_in_occurrence(entity, _mapping.get("repl_occ")):
                    # doExecute() is a no-op when called from InputChangedEventHandler;
                    # defer via custom event to run on the next event loop tick.
                    _mapping["_pick_cmd_pending"] = self._cmd
                    try:
                        adsk.core.Application.get().fireCustomEvent(AUTO_EXEC_EVENT_ID, "")
                    except Exception:
                        self._cmd.doExecute(False)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Utility / geometry helpers
# ---------------------------------------------------------------------------

def _tl_index_of(tl, obj) -> int:
    """Find timeline index of obj by iterating (occ.timelineObject.index fails for external occs)."""
    if obj is None:
        return -1
    try:
        tok = obj.entityToken
    except Exception:
        return -1
    for i in range(tl.count):
        try:
            ent = tl.item(i).entity
            if ent is not None and ent.entityToken == tok:
                return i
        except Exception:
            pass
    return -1


def _pt_via_local(source_pt, pick_transform, current_transform):
    """Convert source_pt (world at pick time) through local space to current world space.

    When the supplanter moves in the timeline its world position changes, making
    the original pick-time world coordinates useless for face lookup.  Reversing
    to local space and re-projecting with the current transform gives coordinates
    that match the face centroids that _nearest_face_to_point iterates over.
    Falls back to the raw source_pt when transforms are unavailable.
    """
    if source_pt is None:
        return None
    if pick_transform is None or current_transform is None:
        return source_pt
    try:
        inv = pick_transform.copy()
        inv.invert()
        local_pt = adsk.core.Point3D.create(source_pt.x, source_pt.y, source_pt.z)
        local_pt.transformBy(inv)
        world_pt = adsk.core.Point3D.create(local_pt.x, local_pt.y, local_pt.z)
        world_pt.transformBy(current_transform)
        return world_pt
    except Exception:
        return source_pt


def _same_occurrence(a, b) -> bool:
    try:
        return a is not None and b is not None and a.entityToken == b.entityToken
    except Exception:
        return False


def _entity_in_occurrence(entity, occ) -> bool:
    try:
        body = entity.body
        for i in range(occ.bRepBodies.count):
            if occ.bRepBodies.item(i).entityToken == body.entityToken:
                return True
    except Exception:
        pass
    # Construction planes, axes, points, joint origins, sketch entities:
    # assemblyContext returns the containing occurrence.
    try:
        ctx = entity.assemblyContext
        if ctx is not None and occ is not None:
            if ctx.entityToken == occ.entityToken:
                return True
    except Exception:
        pass
    return False


def _entity_key(entity) -> tuple:
    try:
        if isinstance(entity, adsk.fusion.BRepFace):
            cls = type(entity.geometry).__name__
            return entity, ("planar" if cls == "Plane"
                            else "cylinder" if cls == "Cylinder"
                            else "nonplanar")
        if isinstance(entity, adsk.fusion.BRepEdge):
            return entity, "edge"
        if isinstance(entity, adsk.fusion.ConstructionPlane):
            return entity, "construction_plane"
        if isinstance(entity, adsk.fusion.ConstructionAxis):
            return entity, "construction_axis"
        if isinstance(entity, adsk.fusion.ConstructionPoint):
            return entity, "construction_point"
        if isinstance(entity, adsk.fusion.JointOrigin):
            return entity, "joint_origin"
        if isinstance(entity, adsk.fusion.SketchPoint):
            return entity, "sketch_point"
        if isinstance(entity, adsk.fusion.BRepVertex):
            return entity, "vertex"
    except Exception:
        pass
    return None, None


def _entity_ref_point(entity, geom_key: str) -> Optional[adsk.core.Point3D]:
    """Return a stable world-space reference point for any supported entity type."""
    try:
        if geom_key == "edge":
            return _edge_ref_point(entity)
        if geom_key == "construction_plane":
            # Use origin+normal so co-origined planes are distinguishable.
            _cpg = entity.geometry
            _n   = _cpg.normal
            _o   = _cpg.origin
            return adsk.core.Point3D.create(_o.x + _n.x, _o.y + _n.y, _o.z + _n.z)
        if geom_key in ("construction_axis", "construction_point", "joint_origin"):
            return entity.geometry.origin
        if geom_key == "sketch_point":
            g = entity.geometry
            return adsk.core.Point3D.create(g.x, g.y, g.z)
        if geom_key == "vertex":
            return entity.geometry
        return _face_centroid(entity)
    except Exception:
        return None


def _nearest_construction_plane_to_point(
        pt: adsk.core.Point3D, occ
) -> Optional[adsk.fusion.ConstructionPlane]:
    """Return the CP on occ whose representative point (origin+normal) is closest to pt.

    Using origin+normal rather than origin alone ensures the three XY/XZ/YZ origin
    planes — which all share the same origin (0,0,0) in component space — are
    distinguishable from each other.
    """
    best, best_d = None, 1e18
    try:
        planes = occ.component.constructionPlanes
        tf = occ.transform
        for i in range(planes.count):
            cp = planes.item(i)
            try:
                g = cp.geometry
                o = g.origin
                n = g.normal
                rep_local = adsk.core.Point3D.create(o.x + n.x, o.y + n.y, o.z + n.z)
                rep_world = adsk.core.Point3D.create(rep_local.x, rep_local.y, rep_local.z)
                rep_world.transformBy(tf)
                d = pt.distanceTo(rep_world)
                if d < best_d:
                    best_d = d
                    best = cp
            except Exception:
                pass
        # Also check origin planes — not in constructionPlanes collection but selectable
        for _attr in ("xYConstructionPlane", "xZConstructionPlane", "yZConstructionPlane"):
            try:
                cp = getattr(occ.component, _attr)
                g = cp.geometry
                o = g.origin
                n = g.normal
                rep_local = adsk.core.Point3D.create(o.x + n.x, o.y + n.y, o.z + n.z)
                rep_world = adsk.core.Point3D.create(rep_local.x, rep_local.y, rep_local.z)
                rep_world.transformBy(tf)
                d = pt.distanceTo(rep_world)
                if d < best_d:
                    best_d = d
                    best = cp
            except Exception:
                pass
    except Exception:
        pass
    if best is not None and occ is not None:
        try:
            return best.createForAssemblyContext(occ)
        except Exception:
            pass
    return best


def _primary_body(occ):
    try:
        if occ.bRepBodies.count > 0:
            return occ.bRepBodies.item(0)
    except Exception:
        pass
    return None


def _rotate_by_transform(vec, t) -> Optional[adsk.core.Vector3D]:
    """Return vec rotated by the rotation component of transform matrix t."""
    try:
        p0 = adsk.core.Point3D.create(0, 0, 0)
        p1 = adsk.core.Point3D.create(vec.x, vec.y, vec.z)
        p0.transformBy(t)
        p1.transformBy(t)
        return adsk.core.Vector3D.create(p1.x - p0.x, p1.y - p0.y, p1.z - p0.z)
    except Exception:
        return None


def _face_world_normal(face, occ) -> Optional[adsk.core.Vector3D]:
    """Return the world-space normal of a planar BRepFace given its occurrence."""
    try:
        g = face.geometry
        if not isinstance(g, adsk.core.Plane):
            return None
        t = occ.transform
        return _rotate_by_transform(g.normal, t)
    except Exception:
        return None


def _face_centroid(face) -> Optional[adsk.core.Point3D]:
    try:
        bb = face.boundingBox
        return adsk.core.Point3D.create(
            (bb.minPoint.x + bb.maxPoint.x) / 2,
            (bb.minPoint.y + bb.maxPoint.y) / 2,
            (bb.minPoint.z + bb.maxPoint.z) / 2)
    except Exception:
        return None


def _edge_ref_point(edge) -> Optional[adsk.core.Point3D]:
    try:
        g = edge.geometry
        if type(g).__name__ == "Circle3D":
            return g.center
        sv = edge.startVertex.geometry
        ev = edge.endVertex.geometry
        return adsk.core.Point3D.create(
            (sv.x + ev.x) / 2, (sv.y + ev.y) / 2, (sv.z + ev.z) / 2)
    except Exception:
        return None


def _geom_origin(geom) -> Optional[adsk.core.Point3D]:
    try:
        if isinstance(geom, adsk.fusion.JointOrigin):
            return geom.geometry.origin
        pt = geom.origin
        if isinstance(pt, adsk.core.Vector3D):
            return adsk.core.Point3D.create(pt.x, pt.y, pt.z)
        return pt
    except Exception:
        return None


def _nearest_face_to_point(pt, occ, geom_filter=None) -> tuple:
    """Find the face on occ whose centroid is nearest to pt.
    geom_filter: if "Plane" or "Cylinder", only consider faces of that geometry type.
    Falls back to any face when no match is found with the filter active.
    """
    body = _primary_body(occ)
    if body is None or pt is None:
        return None, None
    best, best_d = None, 1e9
    for i in range(body.faces.count):
        face = body.faces.item(i)
        if geom_filter is not None:
            try:
                if type(face.geometry).__name__ != geom_filter:
                    continue
            except Exception:
                continue
        ctr = _face_centroid(face)
        if ctr is None:
            continue
        d = pt.distanceTo(ctr)
        if d < best_d:
            best_d = d
            best   = face
    if best is None:
        return None, None
    cls = type(best.geometry).__name__
    return best, ("planar" if cls == "Plane" else "cylinder" if cls == "Cylinder" else "nonplanar")


def _nearest_edge_to_point(pt, occ):
    body = _primary_body(occ)
    if body is None or pt is None:
        return None
    best, best_d = None, 1e9
    for i in range(body.edges.count):
        edge = body.edges.item(i)
        rp   = _edge_ref_point(edge)
        if rp is None:
            continue
        d = pt.distanceTo(rp)
        if d < best_d:
            best_d = d
            best   = edge
    return best


def _capture_rel_cache(rel, entity_side: str, orig_raw) -> Optional[dict]:
    """Capture other-side + property data from a relationship for preview use.

    Called at _init_mapping time (while rolled to the constraint) so we can
    build preview constraints later without a second rollTo.
    """
    try:
        other_raw = rel.entityTwo if entity_side == "one" else rel.entityOne
        other_occ_v = None
        other_pt_v  = None
        other_key_v = "planar"
        if other_raw is not None:
            try:
                other_occ_v = other_raw.assemblyContext
            except Exception:
                pass
            if isinstance(other_raw, adsk.fusion.BRepFace):
                other_pt_v  = _face_centroid(other_raw)
                cls         = type(other_raw.geometry).__name__
                other_key_v = ("planar" if cls == "Plane"
                               else "cylinder" if cls == "Cylinder"
                               else "nonplanar")
            elif isinstance(other_raw, adsk.fusion.BRepEdge):
                other_pt_v  = _edge_ref_point(other_raw)
                other_key_v = "edge"
            elif isinstance(other_raw, adsk.fusion.ConstructionPlane):
                try:
                    _cpg2 = other_raw.geometry
                    _n2   = _cpg2.normal
                    _o2   = _cpg2.origin
                    other_pt_v = adsk.core.Point3D.create(
                        _o2.x + _n2.x, _o2.y + _n2.y, _o2.z + _n2.z)
                except Exception:
                    other_pt_v = None
                other_key_v = "construction_plane"

        is_mate_v, offset_val_v, is_flipped_v = True, 0.0, False
        offset_threw_v = False
        try:
            is_mate_v = rel.isMate
        except Exception:
            pass
        try:
            p = rel.offsetOrAngle
            if p is not None:
                offset_val_v = p.value
        except Exception:
            offset_threw_v = True
        try:
            is_flipped_v = rel.isFlipped
        except Exception:
            pass

        # Flag relationships the API cannot create (midplane / tangent Face-Edge).
        skip_api_v = not is_mate_v and offset_threw_v

        # Capture orig-side world normal for auto-flip comparison at execute time.
        orig_world_n = None
        if isinstance(orig_raw, adsk.fusion.BRepFace):
            try:
                orig_ctx = orig_raw.assemblyContext
                if orig_ctx is not None:
                    orig_world_n = _face_world_normal(orig_raw, orig_ctx)
            except Exception:
                pass
        elif isinstance(orig_raw, adsk.fusion.ConstructionPlane):
            try:
                orig_ctx = orig_raw.assemblyContext
                n_local  = orig_raw.geometry.normal
                tf       = orig_ctx.transform if orig_ctx is not None else None
                orig_world_n = (_rotate_by_transform(n_local, tf) if tf is not None
                                else adsk.core.Vector3D.create(n_local.x, n_local.y, n_local.z))
            except Exception:
                pass

        return {
            "other_occ":        other_occ_v,
            "other_pt":         other_pt_v,
            "other_key":        other_key_v,
            "is_mate":          is_mate_v,
            "offset_val":       offset_val_v,
            "is_flipped":       is_flipped_v,
            "skip_api":         skip_api_v,
            "orig_world_normal": orig_world_n,
        }
    except Exception:
        return None


def _delete_preview_constraints(comp=None) -> None:
    """Remove all SUPPLANT_PREVIEW_* constraints from the root component."""
    if comp is None:
        try:
            app  = adsk.core.Application.get()
            des  = adsk.fusion.Design.cast(app.activeProduct)
            comp = des.rootComponent
        except Exception:
            return
    to_delete = []
    try:
        for i in range(comp.assemblyConstraints.count):
            ac = comp.assemblyConstraints.item(i)
            try:
                if ac.name.startswith("SUPPLANT_PREVIEW_"):
                    to_delete.append(ac)
            except Exception:
                pass
    except Exception:
        pass
    for ac in to_delete:
        try:
            ac.deleteMe()
        except Exception:
            pass


def _build_preview() -> None:
    """Build temporary preview constraints so the user sees the live result of their picks.

    Creates one SUPPLANT_PREVIEW_<name> constraint per constraint group that has
    at least one mapped pick.  Deleted/rebuilt on each pick and flip-toggle so the
    viewport always reflects the current mapping + flip state.
    """
    try:
        app        = adsk.core.Application.get()
        des        = adsk.fusion.Design.cast(app.activeProduct)
        comp       = des.rootComponent
        target_occ = _mapping.get("repl_occ")
        all_items  = _mapping.get("all_items", [])
        done       = _mapping.get("done", {})

        _delete_preview_constraints(comp)

        if target_occ is None or not all_items or not done:
            return

        # Group picked items by constraint name.
        groups: dict = {}
        for item in all_items:
            entity = item[0]
            if not isinstance(entity, adsk.fusion.AssemblyConstraint):
                continue
            if item[1].endswith(":b"):
                continue  # :b partner picks handled via their primary rel
            rel_cache = item[6] if len(item) > 6 else None
            if rel_cache is None or rel_cache.get("skip_api"):
                continue
            tok = _item_token(item)
            if tok not in done:
                continue
            ac_name = entity.name
            if ac_name not in groups:
                groups[ac_name] = []
            groups[ac_name].append((item, done[tok], rel_cache))

        _GEOM_CLS = {"planar": "Plane", "cylinder": "Cylinder"}

        for ac_name, entries in groups.items():
            try:
                ci          = comp.assemblyConstraints.createInput()
                rcol        = ci.geometricRelationships
                created_any = False
                for item, done_entry, rel_cache in entries:
                    rel_key     = item[1]  # e.g. "0::one"
                    entity_side = rel_key.rsplit("::", 1)[-1]

                    source_pt      = done_entry[0]
                    geom_key       = done_entry[1]
                    pick_transform = done_entry[4] if len(done_entry) > 4 else None
                    pick_normal    = done_entry[5] if len(done_entry) > 5 else None
                    flip_override  = bool(done_entry[6]) if len(done_entry) > 6 and done_entry[6] else False

                    is_mate          = rel_cache.get("is_mate", True)
                    offset_val       = rel_cache.get("offset_val", 0.0)
                    is_flipped       = rel_cache.get("is_flipped", False)
                    other_occ        = rel_cache.get("other_occ")
                    other_pt         = rel_cache.get("other_pt")
                    other_key        = rel_cache.get("other_key", "planar")
                    orig_world_normal = rel_cache.get("orig_world_normal")

                    # Apply auto-flip (same logic as _do_supplant).
                    if geom_key == "planar" and pick_normal is not None and orig_world_normal is not None:
                        try:
                            if pick_normal.dotProduct(orig_world_normal) < 0:
                                is_flipped = not is_flipped
                        except Exception:
                            pass
                    # Apply manual flip override.
                    if flip_override:
                        is_flipped = not is_flipped

                    # Find equiv on target_occ.
                    lookup_pt = _pt_via_local(source_pt, pick_transform, target_occ.transform)
                    if geom_key == "edge":
                        equiv = _nearest_edge_to_point(lookup_pt, target_occ)
                    else:
                        _gf   = _GEOM_CLS.get(geom_key)
                        equiv, _ = _nearest_face_to_point(lookup_pt, target_occ, _gf)
                        if equiv is None and _gf is not None:
                            equiv, _ = _nearest_face_to_point(lookup_pt, target_occ)
                    if equiv is None:
                        continue

                    # Find other_equiv on other_occ.
                    other_equiv = None
                    if other_occ is not None and other_pt is not None:
                        adj_ctr = adsk.core.Point3D.create(other_pt.x, other_pt.y, other_pt.z)
                        adj_ctr.transformBy(other_occ.transform)
                        if other_key == "edge":
                            other_equiv = _nearest_edge_to_point(adj_ctr, other_occ)
                        else:
                            _ogf = _GEOM_CLS.get(other_key)
                            other_equiv, _ = _nearest_face_to_point(adj_ctr, other_occ, _ogf)
                            if other_equiv is None and _ogf is not None:
                                other_equiv, _ = _nearest_face_to_point(adj_ctr, other_occ)
                    if other_equiv is None:
                        continue

                    e1 = equiv      if entity_side == "one" else other_equiv
                    e2 = other_equiv if entity_side == "one" else equiv
                    offset_vi = adsk.core.ValueInput.createByReal(offset_val)
                    try:
                        gr = rcol.add(e1, e2, is_mate, offset_vi)
                        if gr is not None:
                            try:
                                gr.isFlipped = is_flipped
                            except Exception:
                                pass
                            created_any = True
                    except Exception:
                        pass

                if created_any:
                    new_ac = comp.assemblyConstraints.add(ci)
                    if new_ac is not None:
                        try:
                            new_ac.name = f"SUPPLANT_PREVIEW_{ac_name}"
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass


def _find_partner_plane_face(face, body):
    """Return the slot-partner antiparallel plane face on the same body.

    Scores candidates by area similarity first, then perpendicular distance as a
    tiebreaker: score = area_ratio * 1000 + perp.  This prevents tiny chamfer
    faces (same perp distance, far smaller area) from beating the true slot wall.
    """
    try:
        g1 = face.geometry
        if not isinstance(g1, adsk.core.Plane):
            return None
        n1       = g1.normal
        o1       = g1.origin
        tok1     = face.entityToken
        ref_area = max(face.area, 1e-12)
        best, best_score = None, 1e18
        for i in range(body.faces.count):
            f = body.faces.item(i)
            try:
                if f.entityToken == tok1:
                    continue
                if not isinstance(f.geometry, adsk.core.Plane):
                    continue
                n2 = f.geometry.normal
                dot = n1.x * n2.x + n1.y * n2.y + n1.z * n2.z
                if dot > -0.95:
                    continue
                o2 = f.geometry.origin
                dx, dy, dz = o2.x - o1.x, o2.y - o1.y, o2.z - o1.z
                perp = abs(dx * n1.x + dy * n1.y + dz * n1.z)
                fa   = max(f.area, 1e-12)
                area_ratio = max(fa, ref_area) / min(fa, ref_area)
                score = area_ratio * 1000.0 + perp
                if score < best_score:
                    best_score = score
                    best = f
            except Exception:
                pass
        return best
    except Exception:
        return None


def _has_good_partner(face, body, ratio_threshold: float = 5.0) -> bool:
    """Return True when face has an antiparallel partner with area ratio < threshold.

    Used to decide whether the orig_occ side of a 2F-2F constraint truly has two
    slot walls (→ ask for two picks) or just one tab face (→ ask for one pick).
    """
    try:
        g1 = face.geometry
        if not isinstance(g1, adsk.core.Plane):
            return False
        n1       = g1.normal
        o1       = g1.origin
        tok1     = face.entityToken
        ref_area = max(face.area, 1e-12)
        for i in range(body.faces.count):
            f = body.faces.item(i)
            try:
                if f.entityToken == tok1:
                    continue
                if not isinstance(f.geometry, adsk.core.Plane):
                    continue
                n2  = f.geometry.normal
                dot = n1.x * n2.x + n1.y * n2.y + n1.z * n2.z
                if dot > -0.95:
                    continue
                fa = max(f.area, 1e-12)
                area_ratio = max(fa, ref_area) / min(fa, ref_area)
                if area_ratio < ratio_threshold:
                    return True
            except Exception:
                pass
        return False
    except Exception:
        return False


def _build_joint_geometry(entity, geom_key: str):
    """Raises on failure so callers can surface the real Fusion error."""
    JG  = adsk.fusion.JointGeometry
    KPT = adsk.fusion.JointKeyPointTypes
    JQA = adsk.fusion.JointQuadrantAngleTypes
    if geom_key == "planar":
        return JG.createByPlanarFace(entity, None, KPT.CenterKeyPoint)
    if geom_key == "cylinder":
        return JG.createByCylinderOrConeFace(
            entity, JQA.StartJointQuadrantAngleType, KPT.CenterKeyPoint)
    if geom_key == "nonplanar":
        return JG.createByNonPlanarFace(entity, KPT.CenterKeyPoint)
    if geom_key == "edge":
        # Try createByCurve directly on the BRepEdge
        try:
            return JG.createByCurve(entity, KPT.CenterKeyPoint)
        except Exception:
            pass
        # Fallback: adjacent cylindrical face → planar → any face
        for face in entity.faces:
            if type(face.geometry).__name__ in ("Cylinder", "Cone"):
                return JG.createByCylinderOrConeFace(
                    face, JQA.StartJointQuadrantAngleType, KPT.CenterKeyPoint)
        for face in entity.faces:
            if type(face.geometry).__name__ == "Plane":
                return JG.createByPlanarFace(face, None, KPT.CenterKeyPoint)
        for face in entity.faces:
            return JG.createByNonPlanarFace(face, KPT.CenterKeyPoint)
    return None


def _equivalent_face_on_occ(source_face, target_occ) -> tuple:
    ctr = _face_centroid(source_face)
    return _nearest_face_to_point(ctr, target_occ)


def _equivalent_edge_on_occ(source_edge, target_occ):
    rp = _edge_ref_point(source_edge)
    return _nearest_edge_to_point(rp, target_occ)


# ---------------------------------------------------------------------------
# Reference collection
# ---------------------------------------------------------------------------

def _pt_in_bbox(pt, bbox, tol: float = 0.1) -> bool:
    return (bbox.minPoint.x - tol <= pt.x <= bbox.maxPoint.x + tol and
            bbox.minPoint.y - tol <= pt.y <= bbox.maxPoint.y + tol and
            bbox.minPoint.z - tol <= pt.z <= bbox.maxPoint.z + tol)


def _joint_referencing_side(joint, orig_occ) -> Optional[str]:
    try:
        if _same_occurrence(joint.occurrenceOne, orig_occ):
            return "one"
        if _same_occurrence(joint.occurrenceTwo, orig_occ):
            return "two"
    except AttributeError:
        pass
    try:
        bbox = orig_occ.boundingBox
        pt1  = _geom_origin(joint.geometryOrOriginOne)
        pt2  = _geom_origin(joint.geometryOrOriginTwo)
        in1  = pt1 is not None and _pt_in_bbox(pt1, bbox)
        in2  = pt2 is not None and _pt_in_bbox(pt2, bbox)
        if in1 and not in2:
            return "one"
        if in2 and not in1:
            return "two"
    except Exception:
        pass
    return None


def _collect_references(orig_occ, design) -> dict:
    refs: dict = {"joints": [], "as_built_joints": [], "rigid_groups": [], "contact_sets": [],
                  "assembly_constraints": []}
    comp = design.rootComponent

    for i in range(comp.joints.count):
        j    = comp.joints.item(i)
        side = _joint_referencing_side(j, orig_occ)
        if side:
            refs["joints"].append((j, side))

    for i in range(comp.asBuiltJoints.count):
        j = comp.asBuiltJoints.item(i)
        if _same_occurrence(j.occurrenceOne, orig_occ):
            refs["as_built_joints"].append((j, "one"))
        elif _same_occurrence(j.occurrenceTwo, orig_occ):
            refs["as_built_joints"].append((j, "two"))

    for i in range(comp.rigidGroups.count):
        rg = comp.rigidGroups.item(i)
        for k in range(rg.occurrences.count):
            if _same_occurrence(rg.occurrences.item(k), orig_occ):
                refs["rigid_groups"].append(rg)
                break

    try:
        for i in range(comp.contactSets.count):
            cs = comp.contactSets.item(i)
            ob = cs.occurencesAndBodies
            for k in range(ob.count):
                try:
                    if _same_occurrence(ob.item(k), orig_occ):
                        refs["contact_sets"].append(cs)
                        break
                except Exception:
                    pass
    except AttributeError:
        pass

    try:
        for i in range(comp.assemblyConstraints.count):
            ac = comp.assemblyConstraints.item(i)
            try:
                # Must rollTo to get entity references on existing constraints.
                ac.timelineObject.rollTo(True)
                rels = ac.geometricRelationships
                found = False
                for k in range(rels.count):
                    if found:
                        break
                    rel = rels.item(k)
                    try:
                        if _entity_in_occurrence(rel.entityOne, orig_occ):
                            refs["assembly_constraints"].append((ac, "one"))
                            found = True
                    except Exception:
                        pass
                    if not found:
                        try:
                            if _entity_in_occurrence(rel.entityTwo, orig_occ):
                                refs["assembly_constraints"].append((ac, "two"))
                                found = True
                        except Exception:
                            pass
            except Exception:
                pass
    except AttributeError:
        pass
    finally:
        try:
            design.timeline.markerPosition = design.timeline.count
        except Exception:
            pass

    return refs


# ---------------------------------------------------------------------------
# Redirect helpers
# ---------------------------------------------------------------------------


def _copy_joint_motion(src_joint, dst_input) -> None:
    """Copy the motion type, offset, angle, and flip from src_joint to dst_input."""
    JT = adsk.fusion.JointTypes
    try:
        jtype  = src_joint.jointType
        motion = src_joint.jointMotion
        if jtype == JT.RigidJointType:
            dst_input.setAsRigidJointMotion()
        elif jtype == JT.RevoluteJointType:
            dst_input.setAsRevoluteJointMotion(motion.rotationAxis)
        elif jtype == JT.SliderJointType:
            dst_input.setAsSliderJointMotion(motion.slideDirection)
        elif jtype == JT.CylindricalJointType:
            dst_input.setAsCylindricalJointMotion(motion.rotationAxis)
        elif jtype == JT.PinSlotJointType:
            dst_input.setAsPinSlotJointMotion(motion.rotationAxis, motion.slideDirection)
        elif jtype == JT.PlanarJointType:
            dst_input.setAsPlanarJointMotion(motion.normalDirection)
        elif jtype == JT.BallJointType:
            dst_input.setAsBallJointMotion()
        else:
            dst_input.setAsRigidJointMotion()
    except Exception:
        dst_input.setAsRigidJointMotion()
    # Copy geometric alignment offset and angle (readable without rollTo)
    try:
        param = src_joint.offset
        if param is not None:
            dst_input.offset = adsk.core.ValueInput.createByReal(param.value)
    except Exception:
        pass
    try:
        param = src_joint.angle
        if param is not None:
            dst_input.angle = adsk.core.ValueInput.createByReal(param.value)
    except Exception:
        pass
    try:
        dst_input.isFlipped = src_joint.isFlipped
    except Exception:
        pass


def _redirect_as_built_joint(joint, side: str, repl_occ) -> tuple:
    try:
        if side == "one":
            joint.occurrenceOne = repl_occ
        else:
            joint.occurrenceTwo = repl_occ
        return True, joint.name
    except Exception as exc:
        return False, f"As-built '{joint.name}': {exc}"


def _redirect_rigid_group(rg, orig_occ, repl_occ) -> tuple:
    try:
        new_list = [repl_occ if _same_occurrence(rg.occurrences.item(i), orig_occ)
                    else rg.occurrences.item(i)
                    for i in range(rg.occurrences.count)]
        rg.setOccurrences(new_list)
        return True, rg.name
    except Exception as exc:
        return False, f"Rigid group '{rg.name}': {exc}"


def _redirect_contact_set(cs, orig_occ, repl_occ) -> tuple:
    try:
        ob       = cs.occurencesAndBodies
        new_list = []
        for i in range(ob.count):
            try:
                item = ob.item(i)
                new_list.append(repl_occ if _same_occurrence(item, orig_occ) else item)
            except Exception:
                pass
        cs.occurencesAndBodies = new_list
        return True, cs.name
    except Exception as exc:
        return False, f"Contact set '{cs.name}': {exc}"


# ---------------------------------------------------------------------------
# Main swap
# ---------------------------------------------------------------------------

def _do_supplant(orig_occ, repl_occ, design) -> str:
    _delete_preview_constraints()
    refs      = _mapping.get("refs") or _collect_references(orig_occ, design)
    all_items = _mapping.get("all_items", [])
    done      = _mapping.get("done", {})
    redirected: list = []
    skipped:    list = []
    mp_workaround_names: list = []  # constraints where midplane workaround was applied

    # Snapshot stable identifiers BEFORE any design changes.
    joint_snapshot:    dict = {}  # name → (token, side, tl_index)
    constraint_groups: dict = {}  # ac_name → {"ac", "tl_idx", "picks": {rel_key → done_entry}}
    for item in all_items:
        entity, side = item[0], item[1]
        try:
            if isinstance(entity, adsk.fusion.AssemblyConstraint):
                ac_name = entity.name
                if ac_name not in constraint_groups:
                    tl_idx = -1
                    try:
                        tl_idx = entity.timelineObject.index
                    except Exception:
                        pass
                    constraint_groups[ac_name] = {"ac": entity, "tl_idx": tl_idx, "picks": {}}
                item_tok = _item_token(item)
                if item_tok in done:
                    constraint_groups[ac_name]["picks"][side] = done[item_tok]
            else:
                snap = (entity.entityToken, side, entity.timelineObject.index)
                joint_snapshot[entity.name] = snap
        except Exception:
            pass

    orig_was_grounded = False
    try:
        orig_was_grounded = orig_occ.isGrounded
    except Exception:
        pass

    # === 1. Move supplanter to right before suplantee in the timeline ===
    tl = design.timeline
    try:
        orig_tl_idx = _tl_index_of(tl, orig_occ)
        repl_tl_idx = _tl_index_of(tl, repl_occ)
        if orig_tl_idx >= 0 and repl_tl_idx >= 0 and repl_tl_idx > orig_tl_idx:
            tl.item(repl_tl_idx).reorder(orig_tl_idx)
        # else: supplanter is already before suplantee or positions not found
    except Exception as e:
        skipped.append(f"Move supplanter: {e}")

    target_occ = repl_occ

    # Roll to end so target_occ geometry is accessible.
    try:
        design.timeline.markerPosition = tl.count
    except Exception:
        pass

    # === 2. Redirect joints ===
    comp_root = design.rootComponent
    sorted_joints = sorted(joint_snapshot.items(), key=lambda kv: kv[1][2], reverse=True)

    # Phase A: build all replacement joint geometries at end of timeline.
    geom_map: dict = {}  # jname → (new_geom, stored_side)
    for jname, (token, _side, _tl_idx) in sorted_joints:
        if token not in done:
            skipped.append(f"'{jname}': not mapped")
            continue
        done_entry = done[token]
        source_pt, geom_key, stored_side = done_entry[0], done_entry[1], done_entry[2]
        pick_transform = done_entry[4] if len(done_entry) > 4 else None
        lookup_pt = _pt_via_local(source_pt, pick_transform, target_occ.transform)
        if geom_key == "edge":
            equiv     = _nearest_edge_to_point(lookup_pt, target_occ)
            equiv_key = "edge"
        else:
            equiv, equiv_key = _nearest_face_to_point(lookup_pt, target_occ)
        if equiv is None:
            skipped.append(f"'{jname}': no equivalent geometry on target")
            continue
        try:
            new_geom = _build_joint_geometry(equiv, equiv_key)
        except Exception as exc:
            skipped.append(f"'{jname}': build_geom failed: {exc}")
            continue
        if new_geom is None:
            skipped.append(f"'{jname}': build_geom returned None")
            continue
        geom_map[jname] = (new_geom, stored_side)

    # Phase B: create new joint, copy motion + offset/angle, delete old joint.
    phase_b_redirected: list = []
    for jname, (new_geom, stored_side) in geom_map.items():
        joint = next((comp_root.joints.item(i)
                      for i in range(comp_root.joints.count)
                      if comp_root.joints.item(i).name == jname), None)
        if joint is None:
            skipped.append(f"'{jname}': not found")
            continue
        try:
            if stored_side == "one":
                other_geom = joint.geometryOrOriginTwo
                ji = comp_root.joints.createInput(new_geom, other_geom)
            else:
                other_geom = joint.geometryOrOriginOne
                ji = comp_root.joints.createInput(other_geom, new_geom)
            _copy_joint_motion(joint, ji)
            new_joint = comp_root.joints.add(ji)
            try:
                new_joint.name = jname
            except Exception:
                pass
            joint.deleteMe()
            redirected.append(jname)
            phase_b_redirected.append((new_joint, jname))
        except Exception as exc:
            skipped.append(f"'{jname}': {exc}")

    # === 2b. Redirect assembly constraints ===
    # Each constraint feature is rebuilt relationship-by-relationship using the
    # individual picks stored per rel_key in constraint_groups[cname]["picks"].
    phase_b2_redirected: list = []
    sorted_constraints = sorted(constraint_groups.items(),
                                key=lambda kv: kv[1]["tl_idx"], reverse=True)
    for cname, cgrp in sorted_constraints:
        ac    = cgrp["ac"]
        picks = cgrp["picks"]  # rel_key → done_entry
        if not picks:
            skipped.append(f"Constraint '{cname}': not mapped")
            continue

        # Roll to to read relationships and their other-side entity data.
        # rel_rows: (k, entity_side, source_pt, geom_key, pick_transform,
        #            other_occ, other_ctr, other_key, is_mate, offset_val, is_flipped)
        rel_rows: list = []
        midplane_rows: list = []  # 2F-2F midplane cases handled via workaround
        # partner_rows: data for ":b" (2nd-face) picks in 2F-2F constraints
        # (entity_side, done_entry, is_mate, offset_val, other_ctr, other_key, other_occ)
        partner_rows: list = []
        had_midplane_skip = False
        rolled = False
        try:
            ac.timelineObject.rollTo(True)
            rolled = True
            rels = ac.geometricRelationships
            for k in range(rels.count):
                rel = rels.item(k)
                for entity_side in ("one", "two"):
                    rel_key = f"{k}::{entity_side}"
                    if rel_key not in picks:
                        continue
                    done_entry = picks[rel_key]
                    source_pt      = done_entry[0]
                    geom_key       = done_entry[1]
                    pick_transform = done_entry[4] if len(done_entry) > 4 else None
                    # Read other-side entity
                    other_raw = rel.entityTwo if entity_side == "one" else rel.entityOne
                    other_occ = None
                    other_ctr = None
                    other_key = "planar"
                    if other_raw is not None:
                        try:
                            other_occ = other_raw.assemblyContext
                        except Exception:
                            pass
                        if isinstance(other_raw, adsk.fusion.BRepFace):
                            other_ctr = _face_centroid(other_raw)
                            cls       = type(other_raw.geometry).__name__
                            other_key = ("planar" if cls == "Plane"
                                         else "cylinder" if cls == "Cylinder"
                                         else "nonplanar")
                        elif isinstance(other_raw, adsk.fusion.BRepEdge):
                            other_ctr = _edge_ref_point(other_raw)
                            other_key = "edge"
                        elif isinstance(other_raw, adsk.fusion.ConstructionPlane):
                            try:
                                _cpg3 = other_raw.geometry
                                _n3   = _cpg3.normal
                                _o3   = _cpg3.origin
                                other_ctr = adsk.core.Point3D.create(
                                    _o3.x + _n3.x, _o3.y + _n3.y, _o3.z + _n3.z)
                            except Exception:
                                other_ctr = None
                            other_key = "construction_plane"
                    is_mate     = True
                    offset_val  = 0.0
                    is_flipped  = False
                    is_midplane = False
                    try:
                        is_mate = rel.isMate
                    except Exception:
                        pass
                    _offset_threw = False
                    try:
                        param = rel.offsetOrAngle
                        if param is not None:
                            offset_val = param.value
                    except Exception:
                        _offset_threw = True
                    # isMate=False + offsetOrAngle throws → relationship type the API cannot create:
                    #   both sides planar  → 2F-2F midplane  (rcol.add creates Angle instead)
                    #   one side is edge   → tangent Face-Edge (rcol.add creates Angle instead)
                    _no_offset_unmate = not is_mate and _offset_threw
                    _skip_reason = None
                    if _no_offset_unmate and geom_key == "planar" and other_key == "planar":
                        _skip_reason = "midplane (2 Faces–2 Faces)"
                    elif _no_offset_unmate and (
                            (geom_key == "construction_plane" and other_key == "planar") or
                            (geom_key == "planar" and other_key == "construction_plane")):
                        _skip_reason = "midplane (Plane-2Faces)"
                    elif _no_offset_unmate and (geom_key == "edge" or other_key == "edge"):
                        _skip_reason = "tangent (Face–Edge)"
                    if _skip_reason:
                        is_midplane = True
                    try:
                        is_flipped = rel.isFlipped
                    except Exception:
                        pass
                    if is_midplane:
                        if _skip_reason == "tangent (Face–Edge)":
                            had_midplane_skip = True
                            midplane_rows.append({
                                'k': k, 'skip_reason': _skip_reason,
                            })
                            continue
                        # Plane-2 Faces and 2F-2F midplane: cannot be created via
                        # rcol.add(e1, e2, False, 0) — that creates Angle instead.
                        # Use setByTwoPlanes in a separate feature with original isFlipped.
                        _mp_world_normal = None
                        try:
                            _orig_raw = rel.entityOne if entity_side == "one" else rel.entityTwo
                            if isinstance(_orig_raw, adsk.fusion.BRepFace):
                                _orig_ctx = _orig_raw.assemblyContext
                                if _orig_ctx is not None:
                                    _mp_world_normal = _face_world_normal(_orig_raw, _orig_ctx)
                            elif isinstance(_orig_raw, adsk.fusion.ConstructionPlane):
                                _orig_ctx = _orig_raw.assemblyContext
                                _n_loc    = _orig_raw.geometry.normal
                                _tf       = _orig_ctx.transform if _orig_ctx is not None else None
                                _mp_world_normal = (
                                    _rotate_by_transform(_n_loc, _tf) if _tf is not None
                                    else adsk.core.Vector3D.create(_n_loc.x, _n_loc.y, _n_loc.z))
                        except Exception:
                            pass
                        midplane_rows.append({
                            'k': k,
                            'skip_reason': _skip_reason,
                            'entity_side': entity_side,
                            'source_pt': source_pt,
                            'geom_key': geom_key,
                            'pick_transform': pick_transform,
                            'other_occ': other_occ,
                            'other_ctr': other_ctr,
                            'other_key': other_key,
                            'is_flipped': is_flipped,
                            'orig_world_normal': _mp_world_normal,
                        })
                        continue
                    # Capture world-space normal of the orig_occ side entity so execution
                    # can auto-correct isFlipped when the equivalent has opposite orientation.
                    orig_world_normal = None
                    try:
                        orig_raw = rel.entityOne if entity_side == "one" else rel.entityTwo
                        if isinstance(orig_raw, adsk.fusion.BRepFace):
                            orig_ctx = orig_raw.assemblyContext
                            if orig_ctx is not None:
                                orig_world_normal = _face_world_normal(orig_raw, orig_ctx)
                        elif isinstance(orig_raw, adsk.fusion.ConstructionPlane):
                            orig_ctx = orig_raw.assemblyContext
                            n_loc    = orig_raw.geometry.normal
                            tf       = orig_ctx.transform if orig_ctx is not None else None
                            orig_world_normal = (
                                _rotate_by_transform(n_loc, tf) if tf is not None
                                else adsk.core.Vector3D.create(n_loc.x, n_loc.y, n_loc.z))
                    except Exception:
                        pass
                    rel_rows.append((k, entity_side, source_pt, geom_key, pick_transform,
                                     other_occ, other_ctr, other_key,
                                     is_mate, offset_val, is_flipped, orig_world_normal))
            # Collect data for ":b" partner picks (2nd face of 2F-2F)
            for rb_key in list(picks.keys()):
                if not rb_key.endswith(":b"):
                    continue
                base_key = rb_key[:-2]
                b_parts = base_key.split("::")
                if len(b_parts) != 2:
                    continue
                try:
                    k_b = int(b_parts[0])
                except ValueError:
                    continue
                entity_side_b = b_parts[1]
                done_entry_b = picks[rb_key]
                rel_b = rels.item(k_b)
                is_mate_b = False
                offset_val_b = 0.0
                try:
                    is_mate_b = rel_b.isMate
                except Exception:
                    pass
                _offset_threw_b = False
                try:
                    param_b = rel_b.offsetOrAngle
                    if param_b is not None:
                        offset_val_b = param_b.value
                except Exception:
                    _offset_threw_b = True
                # Skip partner entry if the base relationship is a 2F-2F midplane
                if not is_mate_b and _offset_threw_b:
                    continue
                other_raw_b = rel_b.entityTwo if entity_side_b == "one" else rel_b.entityOne
                other_occ_b = None
                try:
                    other_occ_b = other_raw_b.assemblyContext
                except Exception:
                    pass
                other_ctr_b = None
                try:
                    partner_other = _find_partner_plane_face(other_raw_b, other_raw_b.body)
                    if partner_other is not None:
                        other_ctr_b = _face_centroid(partner_other)
                    else:
                        other_ctr_b = _face_centroid(other_raw_b)
                except Exception:
                    try:
                        other_ctr_b = _face_centroid(other_raw_b)
                    except Exception:
                        pass
                partner_rows.append((entity_side_b, done_entry_b, is_mate_b, offset_val_b,
                                     other_ctr_b, "planar", other_occ_b))
        except Exception as exc:
            skipped.append(f"Constraint '{cname}': rollTo failed: {exc}")
        finally:
            if rolled:
                try:
                    design.timeline.markerPosition = tl.count
                except Exception:
                    pass

        if not rel_rows and not midplane_rows:
            skipped.append(f"Constraint '{cname}': no pickable relationships found")
            continue

        # Build new constraint input at end of timeline.
        try:
            any_redirected = False
            _GEOM_CLS = {"planar": "Plane", "cylinder": "Cylinder"}

            # --- Stage regular + partner rels before deciding how to batch them ---
            # Each entry: (e1, e2, is_mate, offset_vi, is_flipped, other_occ)
            staged_rels: list = []

            for _row in rel_rows:
                (k, entity_side, source_pt, geom_key, pick_transform,
                 other_occ, other_ctr, other_key,
                 is_mate, offset_val, is_flipped) = _row[:11]
                orig_world_normal = _row[11] if len(_row) > 11 else None
                done_entry    = picks.get(f"{k}::{entity_side}", ())
                pick_normal   = done_entry[5] if len(done_entry) > 5 else None
                flip_override = bool(done_entry[6]) if len(done_entry) > 6 and done_entry[6] else False
                lookup_pt = _pt_via_local(source_pt, pick_transform, target_occ.transform)
                if geom_key == "edge":
                    equiv = _nearest_edge_to_point(lookup_pt, target_occ)
                elif geom_key == "construction_plane":
                    equiv = _nearest_construction_plane_to_point(lookup_pt, target_occ)
                else:
                    _gf = _GEOM_CLS.get(geom_key)
                    equiv, _ = _nearest_face_to_point(lookup_pt, target_occ, _gf)
                    if equiv is None and _gf is not None:
                        equiv, _ = _nearest_face_to_point(lookup_pt, target_occ)
                if equiv is None:
                    skipped.append(f"Constraint '{cname}' rel {k}: no equiv on target")
                    continue
                if geom_key in ("planar", "construction_plane") and pick_normal is not None and orig_world_normal is not None:
                    try:
                        if pick_normal.dotProduct(orig_world_normal) < 0:
                            is_flipped = not is_flipped
                    except Exception:
                        pass
                if flip_override:
                    is_flipped = not is_flipped
                other_equiv = None
                if other_occ is not None and other_ctr is not None:
                    adj_ctr = adsk.core.Point3D.create(other_ctr.x, other_ctr.y, other_ctr.z)
                    adj_ctr.transformBy(other_occ.transform)
                    if other_key == "edge":
                        other_equiv = _nearest_edge_to_point(adj_ctr, other_occ)
                    elif other_key == "construction_plane":
                        other_equiv = _nearest_construction_plane_to_point(adj_ctr, other_occ)
                    else:
                        _ogf = _GEOM_CLS.get(other_key)
                        other_equiv, _ = _nearest_face_to_point(adj_ctr, other_occ, _ogf)
                        if other_equiv is None and _ogf is not None:
                            other_equiv, _ = _nearest_face_to_point(adj_ctr, other_occ)
                if other_equiv is None:
                    skipped.append(f"Constraint '{cname}' rel {k}: no equiv on other")
                    continue
                e1 = equiv      if entity_side == "one" else other_equiv
                e2 = other_equiv if entity_side == "one" else equiv
                staged_rels.append((e1, e2, is_mate,
                                     adsk.core.ValueInput.createByReal(offset_val),
                                     is_flipped, other_occ))

            # Process 2nd-face picks for 2F-2F constraints
            for (entity_side_b, done_entry_b, is_mate_b, offset_val_b,
                 other_ctr_b, other_key_b, other_occ_b) in partner_rows:
                source_pt_b   = done_entry_b[0]
                pick_tf_b     = done_entry_b[4] if len(done_entry_b) > 4 else None
                lookup_pt_b   = _pt_via_local(source_pt_b, pick_tf_b, target_occ.transform)
                equiv_b, _    = _nearest_face_to_point(lookup_pt_b, target_occ)
                if equiv_b is None:
                    skipped.append(f"Constraint '{cname}' partner: no equiv on target")
                    continue
                other_equiv_b = None
                if other_occ_b is not None and other_ctr_b is not None:
                    adj_ctr_b = adsk.core.Point3D.create(other_ctr_b.x, other_ctr_b.y, other_ctr_b.z)
                    adj_ctr_b.transformBy(other_occ_b.transform)
                    other_equiv_b, _ = _nearest_face_to_point(adj_ctr_b, other_occ_b)
                if other_equiv_b is None:
                    skipped.append(f"Constraint '{cname}' partner: no equiv on other")
                    continue
                e1_b = equiv_b       if entity_side_b == "one" else other_equiv_b
                e2_b = other_equiv_b if entity_side_b == "one" else equiv_b
                staged_rels.append((e1_b, e2_b, is_mate_b,
                                     adsk.core.ValueInput.createByReal(offset_val_b),
                                     False, other_occ_b))

            # --- Flush staged_rels: all go into one AssemblyConstraint feature.
            ci_shared   = comp_root.assemblyConstraints.createInput()
            rcol_shared = ci_shared.geometricRelationships
            shared_occs: list = []
            shared_count = 0

            for _idx, (_e1, _e2, _im, _ovi, _ifl, _oocc) in enumerate(staged_rels):
                try:
                    _gr = rcol_shared.add(_e1, _e2, _im, _ovi)
                    if _gr is not None:
                        try: _gr.isFlipped = _ifl
                        except Exception: pass
                        shared_count += 1
                        if _oocc is not None:
                            shared_occs.append(_oocc)
                except Exception as _exc:
                    skipped.append(f"Constraint '{cname}' rel {_idx}: {_exc}")

            # Midplane rows: add to the SAME ci_shared (one feature total).
            # API cannot create 2F-2F / Plane-2 Faces midplane via rcol.add directly.
            # Workaround: root-level CP at W/2 from slot wall + mate(equiv, cp, T_half).
            for mrow in midplane_rows:
                if mrow.get('skip_reason') == "tangent (Face–Edge)":
                    skipped.append(
                        f"Constraint '{cname}' tangent rel {mrow['k']}: "
                        f"Face-Edge tangent cannot be created via API; rebuild manually")
                    continue
                try:
                    _mp_es        = mrow['entity_side']
                    _mp_src_pt    = mrow['source_pt']
                    _mp_geom_key  = mrow['geom_key']
                    _mp_pick_tf   = mrow.get('pick_transform')
                    _mp_other_occ = mrow['other_occ']
                    _mp_other_ctr = mrow['other_ctr']
                    _mp_other_key = mrow['other_key']
                    # Restore original isFlipped + auto-flip + manual flip.
                    _mp_is_fl = mrow['is_flipped']
                    _mp_wn    = mrow.get('orig_world_normal')
                    _mp_de    = picks.get(f"{mrow['k']}::{_mp_es}", ())
                    _mp_pn    = _mp_de[5] if len(_mp_de) > 5 else None
                    _mp_fo    = bool(_mp_de[6]) if len(_mp_de) > 6 and _mp_de[6] else False
                    if _mp_pn is not None and _mp_wn is not None:
                        try:
                            if _mp_pn.dotProduct(_mp_wn) < 0:
                                _mp_is_fl = not _mp_is_fl
                        except Exception:
                            pass
                    if _mp_fo:
                        _mp_is_fl = not _mp_is_fl
                    # No inversion: original isFlipped (after auto-flip) maps directly.

                    # Find equiv on target_occ (tab face or orig CP).
                    _mp_lp = _pt_via_local(_mp_src_pt, _mp_pick_tf, target_occ.transform)
                    if _mp_geom_key == "construction_plane":
                        _mp_equiv = _nearest_construction_plane_to_point(_mp_lp, target_occ)
                        if _mp_equiv is None:
                            _mp_equiv, _ = _nearest_face_to_point(_mp_lp, target_occ, "Plane")
                            if _mp_equiv is None:
                                _mp_equiv, _ = _nearest_face_to_point(_mp_lp, target_occ)
                    else:
                        _mp_equiv, _ = _nearest_face_to_point(_mp_lp, target_occ, "Plane")
                        if _mp_equiv is None:
                            _mp_equiv, _ = _nearest_face_to_point(_mp_lp, target_occ)
                    if _mp_equiv is None:
                        skipped.append(
                            f"Constraint '{cname}' midplane rel {mrow['k']}: no equiv on target")
                        continue

                    # Find other_equiv on other_occ (slot wall face or CP).
                    if _mp_other_occ is None or _mp_other_ctr is None:
                        skipped.append(
                            f"Constraint '{cname}' midplane rel {mrow['k']}: no other_occ/ctr")
                        continue
                    _mp_adj = adsk.core.Point3D.create(
                        _mp_other_ctr.x, _mp_other_ctr.y, _mp_other_ctr.z)
                    _mp_adj.transformBy(_mp_other_occ.transform)
                    if _mp_other_key == "construction_plane":
                        _mp_other_equiv = _nearest_construction_plane_to_point(
                            _mp_adj, _mp_other_occ)
                    else:
                        _mp_other_equiv, _ = _nearest_face_to_point(
                            _mp_adj, _mp_other_occ, "Plane")
                        if _mp_other_equiv is None:
                            _mp_other_equiv, _ = _nearest_face_to_point(
                                _mp_adj, _mp_other_occ)
                    if _mp_other_equiv is None:
                        skipped.append(
                            f"Constraint '{cname}' midplane rel {mrow['k']}: no equiv on other")
                        continue

                    # Compute W_half using proxy face geometry (world-space) throughout.
                    # nativeObject returns local-space geometry; local normals may be
                    # antiparallel to world normals, flipping the sign of W_half and
                    # placing the CP on the wrong side of the slot wall.
                    _mp_wall_normal = None
                    _mp_W_half = 0.0
                    _mp_diag = [f"rel={mrow['k']} geom={_mp_geom_key} isfl_orig={mrow['is_flipped']} isfl_pre={_mp_is_fl}"]
                    try:
                        _mp_wall_normal = _mp_other_equiv.geometry.normal
                        _mp_diag.append(f"wall_norm=({_mp_wall_normal.x:.3f},{_mp_wall_normal.y:.3f},{_mp_wall_normal.z:.3f})")
                        if isinstance(_mp_other_equiv, adsk.fusion.BRepFace):
                            # Proxy face: world-space origins and normals, same frame
                            # as setByOffset — no coordinate transform needed.
                            _mp_pwall = _find_partner_plane_face(_mp_other_equiv, _mp_other_equiv.body)
                            if _mp_pwall is not None:
                                _wo = _mp_other_equiv.geometry.origin  # world
                                _po = _mp_pwall.geometry.origin        # world
                                _wn = _mp_other_equiv.geometry.normal  # world
                                _dW = (_po.x - _wo.x) * _wn.x + \
                                      (_po.y - _wo.y) * _wn.y + \
                                      (_po.z - _wo.z) * _wn.z
                                _mp_W_half = _dW / 2.0
                                _mp_diag.append(f"dW={_dW:.4f} W_half={_mp_W_half:.4f}")
                            else:
                                _mp_diag.append("pwall=None")
                    except Exception as _we:
                        _mp_W_half = 0.0
                        _mp_diag.append(f"W_err={_we}")

                    # Create midplane construction plane at W/2 from slot wall.
                    _mp_cp = None
                    try:
                        _ci_cp = comp_root.constructionPlanes.createInput()
                        _ci_cp.setByOffset(_mp_other_equiv,
                                           adsk.core.ValueInput.createByReal(_mp_W_half))
                        _mp_cp = comp_root.constructionPlanes.add(_ci_cp)
                        _mp_diag.append(f"CP_ok W_half={_mp_W_half:.4f}")
                        # Geometry-based flip: for isMate=True, if equiv and midplane CP
                        # normals are antiparallel, the component ends up on the wrong
                        # side. Flip isFlipped to correct this.
                        try:
                            _ng_en = _mp_equiv.geometry.normal
                            _ng_cn = _mp_cp.geometry.normal
                            _ng_dot = _ng_en.dotProduct(_ng_cn)
                            if _ng_dot < 0:
                                _mp_is_fl = not _mp_is_fl
                            # For Plane-2 Faces (orig CP entity): the original constraint
                            # used isMate=False; workaround uses isMate=True. Converting
                            # isMate inverts the isFlipped semantics, so flip here.
                            if _mp_geom_key == "construction_plane":
                                _mp_is_fl = not _mp_is_fl
                                _mp_diag.append(f"ng={_ng_dot:.2f} CP_isMate_inv isfl={_mp_is_fl}")
                            else:
                                _mp_diag.append(f"ng={_ng_dot:.2f} isfl={_mp_is_fl}")
                        except Exception as _nge:
                            _mp_diag.append(f"ng_err={_nge}")
                    except Exception as _cpe:
                        _mp_diag.append(f"CP_err={_cpe}")

                    # Compute T_half for 2F-2F case.
                    # Use proxy face directly (world-space) so _find_partner_plane_face
                    # gets valid entityToken comparisons and world-space origins/normals.
                    # For Plane-2 Faces (orig CP), T_half stays 0: CP flush to midplane CP.
                    _mp_T_half = 0.0
                    # Probe A: try reading offsetOrAngle on the ACTIVE constraint (joint-offset
                    # analogy — joint.offset.value is readable without rollTo; test whether
                    # rel.offsetOrAngle works here now that ac is fully evaluated).
                    try:
                        _ac_rels_active = ac.geometricRelationships
                        _ac_rel_active  = _ac_rels_active.item(mrow['k'])
                        _ac_param       = _ac_rel_active.offsetOrAngle
                        if _ac_param is not None:
                            _mp_diag.append(f"probeA_active_offset={_ac_param.value:.6f}")
                        else:
                            _mp_diag.append("probeA_active_offset=None")
                    except Exception as _ae:
                        _mp_diag.append(f"probeA_err={str(_ae)[:60]}")
                    if _mp_cp is not None and _mp_geom_key == "planar" and _mp_wall_normal is not None:
                        try:
                            # Search ALL bodies of target_occ (not just _mp_equiv.body).
                            # target_occ may have multiple BRep bodies; the partner face
                            # could be in a different body from the selected face.
                            # Accept both antiparallel (dot<-0.95) and parallel (dot>0.95):
                            # some components present the partner face with the same normal.
                            _mp_ptab = None
                            _pt_best = 1e18
                            _pt_ref_n = _mp_equiv.geometry.normal
                            _pt_ref_o = _mp_equiv.geometry.origin
                            _pt_ref_area = max(_mp_equiv.area, 1e-12)
                            try:
                                _pt_ref_tok = _mp_equiv.entityToken
                            except Exception:
                                _pt_ref_tok = None
                            _pt_nbodies = target_occ.bRepBodies.count
                            _pt_total_fc = 0
                            _pt_dots_all = []
                            for _bi in range(_pt_nbodies):
                                _bdy = target_occ.bRepBodies.item(_bi)
                                _pt_total_fc += _bdy.faces.count
                                for _fi in range(_bdy.faces.count):
                                    _ff = _bdy.faces.item(_fi)
                                    try:
                                        if not isinstance(_ff.geometry, adsk.core.Plane):
                                            _pt_dots_all.append("?")
                                            continue
                                        try:
                                            if _pt_ref_tok and _ff.entityToken == _pt_ref_tok:
                                                continue
                                        except Exception:
                                            pass
                                        _fn = _ff.geometry.normal
                                        _d = (_pt_ref_n.x*_fn.x +
                                              _pt_ref_n.y*_fn.y +
                                              _pt_ref_n.z*_fn.z)
                                        _pt_dots_all.append(f"{_d:.2f}")
                                        if abs(_d) < 0.95:
                                            continue
                                        _fo = _ff.geometry.origin
                                        _fa = max(_ff.area, 1e-12)
                                        _ar = max(_fa, _pt_ref_area) / min(_fa, _pt_ref_area)
                                        _pdx = _fo.x - _pt_ref_o.x
                                        _pdy = _fo.y - _pt_ref_o.y
                                        _pdz = _fo.z - _pt_ref_o.z
                                        _perp = abs(_pdx*_pt_ref_n.x +
                                                    _pdy*_pt_ref_n.y +
                                                    _pdz*_pt_ref_n.z)
                                        _sc = _ar * 1000 + _perp
                                        if _sc < _pt_best:
                                            _pt_best = _sc
                                            _mp_ptab = _ff
                                    except Exception:
                                        _pt_dots_all.append("!")
                            if _mp_ptab is not None:
                                _to  = _mp_equiv.geometry.origin   # world
                                _pto = _mp_ptab.geometry.origin    # world
                                _tn  = _mp_equiv.geometry.normal   # world
                                _dT = (_pto.x - _to.x) * _tn.x + \
                                      (_pto.y - _to.y) * _tn.y + \
                                      (_pto.z - _to.z) * _tn.z
                                _mp_T_half = abs(_dT) / 2.0
                                _mp_en = _mp_equiv.geometry.normal
                                _dot_sign = _mp_en.dotProduct(_mp_wall_normal)
                                if _dot_sign >= 0:
                                    _mp_T_half = -_mp_T_half
                                _mp_diag.append(
                                    f"dT={_dT:.4f} T_half={_mp_T_half:.4f} dot={_dot_sign:.3f} "
                                    f"bodies={_pt_nbodies} fc={_pt_total_fc}")
                            else:
                                _dots_str = ','.join(_pt_dots_all[:30])
                                _mp_diag.append(
                                    f"ptab=None bodies={_pt_nbodies} fc={_pt_total_fc} "
                                    f"dots=[{_dots_str}] T_half=0")
                        except Exception as _te:
                            _mp_T_half = 0.0
                            _mp_diag.append(f"T_err={_te}")
                    _mp_line = "SUPPLANT_MP " + " | ".join(_mp_diag)
                    print(_mp_line)
                    try:
                        with open(r"C:\Users\Aleksei\AppData\Local\Temp\supplant_mp_diag.txt", "a") as _mf:
                            _mf.write(_mp_line + "\n")
                    except Exception:
                        pass

                    # Add to the shared relationship collection (not a new feature).
                    if _mp_cp is None:
                        skipped.append(
                            f"Constraint '{cname}' midplane rel {mrow['k']}: CP creation failed")
                    else:
                        try:
                            _mp_gr = rcol_shared.add(
                                _mp_equiv, _mp_cp, True,
                                adsk.core.ValueInput.createByReal(_mp_T_half))
                            if _mp_gr is not None:
                                try: _mp_gr.isFlipped = _mp_is_fl
                                except Exception: pass
                                shared_count += 1
                                if _mp_other_occ is not None:
                                    shared_occs.append(_mp_other_occ)
                        except Exception as _ace:
                            skipped.append(
                                f"Constraint '{cname}' midplane rel {mrow['k']}: "
                                f"rcol_shared.add failed: {_ace}")
                except Exception as _mpe:
                    skipped.append(
                        f"Constraint '{cname}' midplane rel {mrow['k']}: {_mpe}")

            if shared_count > 0:
                _ac_sh = comp_root.assemblyConstraints.add(ci_shared)
                if _ac_sh is None:
                    skipped.append(f"Constraint '{cname}': add() returned None for main rels")
                else:
                    try: _ac_sh.name = cname
                    except Exception: pass
                    any_redirected = True
                    phase_b2_redirected.append((_ac_sh, cname, shared_occs, []))

            if not any_redirected:
                skipped.append(f"Constraint '{cname}': could not build any relationships")
                continue

            ac.deleteMe()
            redirected.append(cname)
        except Exception as exc:
            skipped.append(f"Constraint '{cname}': {exc}")

    # === Phase C: move new joints/constraints near their last dependency ===
    for new_j, jname in phase_b_redirected:
        if new_j is None:
            skipped.append(f"PhaseC '{jname}': joint not found")
            continue
        max_dep = -1
        for attr in ("occurrenceOne", "occurrenceTwo"):
            try:
                occ = getattr(new_j, attr)
                if occ is not None:
                    idx = _tl_index_of(tl, occ)
                    if idx > max_dep:
                        max_dep = idx
            except Exception:
                pass
        if max_dep < 0:
            skipped.append(f"PhaseC '{jname}': could not find occurrence positions")
            continue
        target_before = max_dep + 1
        try:
            new_j_idx = new_j.timelineObject.index
            if new_j_idx > target_before and target_before < tl.count:
                tl.item(new_j_idx).reorder(target_before)
        except Exception as exc:
            skipped.append(f"PhaseC '{jname}': reorder failed: {exc}")

    for new_ac, cname, other_occs, cps in phase_b2_redirected:
        if new_ac is None:
            skipped.append(f"PhaseC constraint '{cname}': not found")
            continue
        max_dep = -1
        # Use the occurrences we tracked during Phase B: target_occ + other_occs
        for dep_occ in [target_occ] + other_occs:
            try:
                idx = _tl_index_of(tl, dep_occ)
                if idx > max_dep:
                    max_dep = idx
            except Exception:
                pass
        if max_dep < 0:
            continue
        target_before = max_dep + 1
        try:
            # If CPs were created for the midplane workaround, reorder them first.
            # The constraint depends on the CPs, so moving the constraint past the CP fails.
            cp_offset = 0
            for cp in cps:
                try:
                    cp_idx = cp.timelineObject.index
                    dest = target_before + cp_offset
                    if cp_idx > dest and dest < tl.count:
                        tl.item(cp_idx).reorder(dest)
                        cp_offset += 1
                except Exception:
                    pass
            new_ac_idx = new_ac.timelineObject.index
            ac_target = target_before + cp_offset
            if new_ac_idx > ac_target and ac_target < tl.count:
                tl.item(new_ac_idx).reorder(ac_target)
        except Exception as exc:
            skipped.append(f"PhaseC constraint '{cname}': reorder failed: {exc}")

    # === 3. Auto-redirect non-joint references ===
    for joint, side in refs["as_built_joints"]:
        ok, detail = _redirect_as_built_joint(joint, side, target_occ)
        (redirected if ok else skipped).append(detail)

    for rg in refs["rigid_groups"]:
        ok, detail = _redirect_rigid_group(rg, orig_occ, target_occ)
        (redirected if ok else skipped).append(detail)

    for cs in refs["contact_sets"]:
        ok, detail = _redirect_contact_set(cs, orig_occ, target_occ)
        (redirected if ok else skipped).append(detail)

    # === 4. Delete original ===
    try:
        orig_occ.deleteMe()
    except Exception as e:
        skipped.append(f"Delete original: {e}")

    # Ground the replacement after the original is gone to avoid grounding conflicts.
    if orig_was_grounded:
        try:
            target_occ.isGrounded = True
            redirected.append("Grounded replacement")
        except Exception as e:
            skipped.append(f"Ground: {e}")

    lines = ["Supplant complete.", f"  Redirected: {len(redirected)}"]
    if mp_workaround_names:
        lines.append(f"  Midplane workarounds ({len(mp_workaround_names)}): "
                     f"{', '.join(mp_workaround_names)}")
    if skipped:
        lines.append(f"  Needs manual fix ({len(skipped)}):")
        for s in skipped:
            lines.append(f"    • {s}")
    return "\n".join(lines)
