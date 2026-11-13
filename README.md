# Supplant

A Fusion 360 add-in that replaces a component in an assembly while automatically redirecting all assembly constraints and joints to the replacement.

## What it does

When you need to swap one component for another in a complex assembly, Supplant:

1. Scans all assembly constraints and joints referencing the original component
2. Opens a palette listing every reference — faces, edges, construction planes
3. Lets you map each reference to the equivalent geometry on the replacement
4. Recreates all constraints and joints on the replacement, preserving offsets, orientations and mate/align settings
5. Removes the original component from the assembly

This avoids manually recreating dozens of constraints after a component swap.

## Supported constraint types

- Face–Face (planar)
- Face–Edge (tangent) — warns user to recreate manually (Fusion API limitation)
- Plane–Face and Plane–2 Faces (midplane via construction plane)
- 2 Faces–2 Faces (midplane)
- Rigid joints, slider joints, revolute joints, and others

## Installation

1. Download or clone this repository
2. Copy `Supplant.bundle` to your Fusion 360 add-ins directory:
   - **Windows:** `%APPDATA%\Autodesk\ApplicationPlugins\`
   - **macOS:** `~/Library/Application Support/Autodesk/ApplicationPlugins/`
3. Restart Fusion 360 (or use the Scripts and Add-Ins dialog to load it)

## Usage

1. Open an assembly containing the component you want to replace
2. Run **Supplant** from the Add-Ins panel
3. In the palette, set **From** (the component to replace) and **To** (the replacement)
4. For each listed reference, click **Pick →** and select the equivalent geometry on the replacement
5. Click **Execute Supplant**

## Requirements

- Autodesk Fusion 360
- Python 3 (bundled with Fusion)

## Development

See [`DEVLOG.md`](DEVLOG.md) for the full version history and technical notes.

## License

Copyright © Danecca Ltd. All rights reserved.
