"""Supplant command — core logic (v0.2.0 — HTML palette UI).

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

import adsk.core
import adsk.fusion

# ---------------------------------------------------------------------------
# IDs
# ---------------------------------------------------------------------------

COMMAND_SETUP_ID = "Supplant_Setup"
COMMAND_PICK_ID  = "Supplant_Pick"
BATCHINSERT_ID   = "BatchInsert_Command"
PALETTE_ID       = "Supplant_Palette"
PALETTE_TITLE    = "Supplant"

INPUT_ORIGINAL    = "sp_original"
INPUT_REPLACEMENT = "sp_replacement"
INPUT_PICK_GEOM   = "sp_pick_geom"

_retained: list = []
_palette = None   # active palette instance

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
    _mapping["all_items"] = items
    _mapping["done"]      = {}  # entityToken → (entity, geom_key, side, label)


def _build_joint_data() -> list:
    """Return serialisable joint list for the HTML palette."""
    all_items = _mapping.get("all_items", [])
    done      = _mapping.get("done", {})
    result    = []
    for joint, side, label in all_items:
        geom = joint.geometryOrOriginOne if side == "one" else joint.geometryOrOriginTwo
        try:
            gtype = geom.geometryType
            JGT   = adsk.fusion.JointGeometryTypes
            if gtype == JGT.JointBRepEdgeGeometry:
                gtype_str = "edge"
            elif gtype == JGT.JointPlanarBRepFaceGeometry:
                gtype_str = "planar"
            elif gtype == JGT.JointNonPlanarBRepFaceGeometry:
                gtype_str = "nonplanar"
            else:
                gtype_str = "?"
        except Exception:
            gtype_str = "?"
        parts      = label.split("  →  ")
        other_name = parts[1] if len(parts) > 1 else ""
        tok        = joint.entityToken
        mapped     = tok in done
        mapped_geom = done[tok][1] if mapped else ""
        result.append({
            "token":      tok,
            "name":       joint.name,
            "type":       gtype_str,
            "other":      other_name,
            "mapped":     mapped,
            "mapped_geom": mapped_geom,
        })
    return result


# ---------------------------------------------------------------------------
# Palette helpers
# ---------------------------------------------------------------------------

def _send_to_palette(action: str, data: dict) -> None:
    global _palette
    if _palette is not None:
        try:
            _palette.sendInfoToHTML(action, json.dumps(data))
        except Exception:
            pass


def _highlight_active_joint(token: str) -> bool:
    """Highlight nearest face/edge on orig_occ for the given joint token."""
    app      = adsk.core.Application.get()
    ui       = app.userInterface
    orig_occ = _mapping.get("orig_occ")
    if orig_occ is None:
        return False
    all_items = _mapping.get("all_items", [])
    entry = next(((j, s) for j, s, _ in all_items if j.entityToken == token), None)
    if entry is None:
        return False
    joint, side = entry
    geom = joint.geometryOrOriginOne if side == "one" else joint.geometryOrOriginTwo
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
    _send_to_palette("data", {
        "orig":   orig_name,
        "repl":   repl_name,
        "auto":   auto,
        "joints": joints,
    })


def _open_palette() -> None:
    global _palette
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
        ph = _PaletteHTMLHandler()
        _palette.incomingFromHTML.add(ph)
        _retained.append(ph)
        pc = _PaletteClosedHandler()
        _palette.closed.add(pc)
        _retained.append(pc)
    else:
        _palette.isVisible = True


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


def unregister(ui, panel) -> None:
    global _palette
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

            elif action == "execute":
                try:
                    app      = adsk.core.Application.get()
                    design   = adsk.fusion.Design.cast(app.activeProduct)
                    orig_occ = _mapping.get("orig_occ")
                    repl_occ = _mapping.get("repl_occ")
                    report   = _do_supplant(orig_occ, repl_occ, design)
                    _send_to_palette("execute_result", {"report": report})
                except Exception as exc:
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
            mp.addSelectionFilter(adsk.core.SelectionFilters.Faces)
            mp.addSelectionFilter("LinearEdges")
            mp.addSelectionFilter("CircularEdges")
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

            token = _mapping.get("picking_token")
            if not token:
                _send_to_palette("pick_error", {"message": "No joint token active."})
                return

            all_items = _mapping.get("all_items", [])
            entry = next(((j, s, lbl) for j, s, lbl in all_items
                          if j.entityToken == token), None)
            if entry is None:
                _send_to_palette("pick_error", {"message": "Joint not found."})
                return

            joint, side, label = entry
            _mapping["done"][token]       = (ent, key, side, label)
            _mapping["picking_token"]     = None

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


# ---------------------------------------------------------------------------
# Utility / geometry helpers
# ---------------------------------------------------------------------------

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
    except Exception:
        pass
    return None, None


def _primary_body(occ):
    try:
        if occ.bRepBodies.count > 0:
            return occ.bRepBodies.item(0)
    except Exception:
        pass
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


def _nearest_face_to_point(pt, occ) -> tuple:
    body = _primary_body(occ)
    if body is None or pt is None:
        return None, None
    best, best_d = None, 1e9
    for i in range(body.faces.count):
        face = body.faces.item(i)
        ctr  = _face_centroid(face)
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


def _build_joint_geometry(entity, geom_key: str):
    JG  = adsk.fusion.JointGeometry
    KPT = adsk.fusion.JointKeyPointTypes
    try:
        if geom_key == "planar":
            return JG.createByPlanarFace(entity, None, KPT.CenterJointKeyPoint)
        if geom_key == "cylinder":
            return JG.createByCylinderOrConeFace(entity, KPT.CenterJointKeyPoint)
        if geom_key == "nonplanar":
            return JG.createByNonPlanarFace(entity, KPT.CenterJointKeyPoint)
        if geom_key == "edge":
            return JG.createByEdge(entity, KPT.MiddleJointKeyPoint)
    except Exception:
        pass
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
    refs: dict = {"joints": [], "as_built_joints": [], "rigid_groups": [], "contact_sets": []}
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

    return refs


# ---------------------------------------------------------------------------
# Redirect helpers
# ---------------------------------------------------------------------------

def _apply_joint_mapping(joint, side: str, source_entity, geom_key: str,
                          target_occ, same_occ: bool) -> tuple:
    try:
        if same_occ:
            equiv, equiv_key = source_entity, geom_key
        elif geom_key == "edge":
            equiv = _equivalent_edge_on_occ(source_entity, target_occ)
            if equiv is None:
                return False, f"'{joint.name}': no equivalent edge on new occurrence"
            equiv_key = "edge"
        else:
            equiv, equiv_key = _equivalent_face_on_occ(source_entity, target_occ)
            if equiv is None:
                return False, f"'{joint.name}': no equivalent face on new occurrence"

        new_geom = _build_joint_geometry(equiv, equiv_key)
        if new_geom is None:
            return False, f"'{joint.name}': could not build joint geometry"

        joint.timelineObject.rollTo(True)
        if side == "one":
            joint.geometryOrOriginOne = new_geom
        else:
            joint.geometryOrOriginTwo = new_geom
        return True, joint.name
    except Exception as exc:
        return False, f"'{joint.name}': {exc}"


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
    refs           = _mapping.get("refs") or _collect_references(orig_occ, design)
    all_items      = _mapping.get("all_items", [])
    done           = _mapping.get("done", {})
    orig_comp_name = orig_occ.component.name
    redirected: list = []
    skipped:    list = []

    orig_was_grounded = False
    try:
        orig_was_grounded = orig_occ.isGrounded
    except Exception:
        pass

    # === 1. Timeline repositioning ===
    new_occ = None
    try:
        repl_comp  = repl_occ.component
        repl_xform = repl_occ.transform
        tl_ok      = False
        try:
            orig_occ.timelineObject.rollTo(True)
            tl_ok = True
        except Exception:
            pass
        if not tl_ok:
            try:
                design.timeline.markerPosition = max(0, orig_occ.timelineObject.index)
                tl_ok = True
            except Exception:
                pass
        if not tl_ok and all_items:
            try:
                min_idx = min(j.timelineObject.index for (j, _, _) in all_items)
                design.timeline.markerPosition = max(0, min_idx - 1)
            except Exception:
                pass
        new_occ = design.rootComponent.occurrences.addExistingComponent(
            repl_comp, repl_xform)
        design.timeline.markerPosition = design.timeline.count
        if orig_was_grounded:
            try:
                new_occ.isGrounded = True
            except Exception as e:
                skipped.append(f"Ground: {e}")
    except Exception as e:
        skipped.append(f"Timeline reposition: {e}")
        new_occ = None

    target_occ = new_occ if new_occ is not None else repl_occ
    same_occ   = new_occ is None

    # === 2. Apply manual joint mappings (latest timeline first) ===
    def _tl(jt):
        try:
            return jt[0].timelineObject.index
        except Exception:
            return 0

    for joint, side, _ in sorted(all_items, key=_tl, reverse=True):
        token = joint.entityToken
        if token in done:
            source_entity, geom_key, stored_side, _ = done[token]
            ok, detail = _apply_joint_mapping(
                joint, stored_side, source_entity, geom_key, target_occ, same_occ)
        else:
            ok, detail = False, f"'{joint.name}': not mapped"
        (redirected if ok else skipped).append(detail)

    try:
        design.timeline.markerPosition = design.timeline.count
    except Exception:
        pass

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

    # === 4. Rename, delete originals ===
    try:
        target_occ.component.name = orig_comp_name
    except Exception as e:
        skipped.append(f"Rename: {e}")

    try:
        orig_occ.deleteMe()
    except Exception as e:
        skipped.append(f"Delete original: {e}")

    if new_occ is not None:
        try:
            repl_occ.deleteMe()
        except Exception as e:
            skipped.append(f"Delete old replacement: {e}")

    lines = ["Supplant complete.", f"  Redirected: {len(redirected)}"]
    if skipped:
        lines.append(f"  Needs manual fix ({len(skipped)}):")
        for s in skipped:
            lines.append(f"    • {s}")
    return "\n".join(lines)
