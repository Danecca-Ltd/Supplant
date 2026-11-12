"""Supplant — replace a component while redirecting all assembly references.

Adds "Supplant" to the Assemble panel in the Design workspace,
placed after Batch Insert when present.
"""

import adsk.core
import adsk.fusion
import traceback
import os
import sys
import importlib

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import supplant_core  # noqa: E402
importlib.reload(supplant_core)  # pick up edits without restarting Fusion

handlers: list = []

_WORKSPACE_ID = "FusionSolidEnvironment"
_PANEL_IDS    = ["SolidAssemblePanel", "AssemblePanel"]


def run(context) -> None:
    app = adsk.core.Application.get()
    ui  = app.userInterface
    try:
        workspace = ui.workspaces.itemById(_WORKSPACE_ID)
        if workspace is None:
            ui.messageBox(
                f"Workspace '{_WORKSPACE_ID}' not found.\n"
                "Open a design and reload the add-in.",
                "Supplant",
            )
            return

        panel = _find_panel(workspace, ui)
        if panel is None:
            ui.messageBox(
                "Assemble panel not found in the toolbar.\n"
                "Supplant could not be registered.",
                "Supplant",
            )
            return

        supplant_core.register(ui, panel, handlers)

    except Exception:
        ui.messageBox(traceback.format_exc(), "Supplant — startup error")


def stop(context) -> None:
    try:
        app = adsk.core.Application.get()
        ui  = app.userInterface
        workspace = ui.workspaces.itemById(_WORKSPACE_ID)
        panel = _find_panel(workspace, ui) if workspace else None
        if panel:
            supplant_core.unregister(ui, panel)
    except Exception:
        pass
    finally:
        handlers.clear()


def _find_panel(
    workspace: adsk.core.Workspace,
    ui: adsk.core.UserInterface,
) -> adsk.core.ToolbarPanel:
    for pid in _PANEL_IDS:
        panel = workspace.toolbarPanels.itemById(pid)
        if panel:
            return panel
    for pid in _PANEL_IDS:
        panel = ui.allToolbarPanels.itemById(pid)
        if panel:
            return panel
    return None
