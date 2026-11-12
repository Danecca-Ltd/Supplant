# Supplant v0.3.0 — Session Notes 2026-11-10

## Session goal
Fix: palette shows "Waiting for data…" after Fusion restarts (data never re-populates).

---

## Root cause chain

1. When Fusion restarts it **restores** the palette from its persisted URL — but this triggers a
   CEF page reload that fires the JS `ready` event before Python has reattached its HTML event
   handler (`_ph`).
2. `_ph` is `None` on a fresh module load (Python globals reset at restart). Nobody receives
   `ready`, so `_push_data_to_palette()` is never called from the JS side.
3. The user then runs Setup → `_open_palette()` reattaches `_ph`, then `_push_data_to_palette()`
   calls `_send_to_palette("data", ...)` → `sendInfoToHTML("data", ...)`.
4. **`sendInfoToHTML` delivers silently without exception, but JS never receives it.** This was
   the persistent mystery. Working hypothesis: CEF has a persistent disk cache and is serving
   the *old* version of `index.html` (before v0.3.0's `window.fusionJavaScriptHandler` wiring).

---

## Changes made this session

### `supplant_core.py` — Python

#### 1. `_send_to_palette()` self-heal (earlier session)
If `_palette is None` (because `_PaletteClosedHandler` fired during page reload), recover it
via `ui.palettes.itemById(PALETTE_ID)` before calling `sendInfoToHTML`.

#### 2. `_open_palette()` — re-attach `_ph` on existing palette (earlier session)
When the palette already exists (Fusion-restored), check `if _ph is None` and create + attach a
new `_PaletteHTMLHandler` so future `ready` events are received.

#### 3. `_push_data_to_palette()` — write sidecar JSON (THIS SESSION)
Writes the full data dict to
`resources/supplant/supplant_data.json` alongside `index.html`, **in addition to** the
`sendInfoToHTML` call. This gives JS a reliable fallback source that doesn't depend on the
Python→JS bridge.

```python
# new block at the top of _push_data_to_palette(), before _send_to_palette():
try:
    sidecar = os.path.join(os.path.dirname(__file__),
                           "resources", "supplant", "supplant_data.json")
    with open(sidecar, "w", encoding="utf-8") as _f:
        json.dump(data, _f)
except Exception:
    pass
```

---

### `index.html` — JavaScript

#### 1. Diagnostic loading-row (earlier session)
Initial `<td>` text changed from "Waiting for data…" to `JS loading…`. IIFE immediately
updates it to `JS OK — waiting for Python data…`. On `handle('data')` it becomes
`data received — N item(s)`. Errors in event-listener setup are caught and displayed there.

#### 2. `NodeList.forEach` → `Array.prototype.forEach.call` (earlier session)
Older Fusion CEF doesn't support `NodeList.prototype.forEach`.

#### 3. `_loadSidecar()` fallback (THIS SESSION)
Added to the bottom of the IIFE, after the `_sendReady` retries:

```javascript
var _dataLoaded = false;
function _loadSidecar() {
  if (_dataLoaded) return;
  try {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', 'supplant_data.json', false); // synchronous, relative to index.html
    xhr.send();
    var ok = (xhr.status === 200 || xhr.status === 0);
    if (ok && xhr.responseText) {
      var d = JSON.parse(xhr.responseText);
      if (d && d.orig) {  // non-empty data
        window.fusionJavaScriptHandler.handle('data', xhr.responseText);
        _dataLoaded = true;
      }
    }
  } catch(e) {}
}
setTimeout(_loadSidecar, 800);
setTimeout(_loadSidecar, 2000);
```

`_dataLoaded` is also set to `true` at the top of `handle('data')` so that if
`sendInfoToHTML` DOES work and delivers data via the bridge, the sidecar fetch is skipped.

---

## State at session end

### Via MCP (Fusion MCP server at 127.0.0.1:27182):

- `_mapping` has 6 `all_items` items (orig = `0-TEST:1`)
- Old palette was `deleteMe()`-d and a fresh one created (`isVisible=True`)
- Module was `importlib.reload()`-d to pick up the new sidecar write
- `_push_data_to_palette()` ran and wrote `supplant_data.json` with `orig=0-TEST:1 joints=2`
- Palette is **currently visible in Fusion** with fresh HTML loaded from disk

The new HTML has `_loadSidecar` which will fire at 800 ms and 2000 ms after page load.
If the sidecar XHR works in Fusion CEF, the palette should now show data **without needing
`sendInfoToHTML` to work**.

### Deployed file locations (both updated identically):
- **Source**: `C:\Fusion Gadgets\Supplant\Supplant.bundle\Contents\`
- **Installed**: `C:\Users\Aleksei\AppData\Roaming\Autodesk\ApplicationPlugins\Supplant.bundle\Contents\`

Relevant files:
- `supplant_core.py` — sidecar write in `_push_data_to_palette()`
- `resources/supplant/index.html` — `_loadSidecar` + diagnostic loading-row
- `resources/supplant/supplant_data.json` — written at runtime; may be stale across design changes

---

## Key open questions

1. **Does `_loadSidecar` XHR work in Fusion CEF?**
   - `file://` XHR is blocked in standard Chromium, but Fusion's CEF may allow it.
   - If it does: the restart problem is solved permanently.
   - If blocked: `xhr.status` will be 0 and `xhr.responseText` empty; the guard `if (ok && xhr.responseText)` means nothing happens (no error thrown).

2. **Does `sendInfoToHTML` reliably work from the Setup execute handler?**
   - It seems to run without exception, but JS `handle()` doesn't fire.
   - Possible cause: CEF serving cached old HTML that lacks `window.fusionJavaScriptHandler`.
   - After today's `deleteMe()` + fresh recreate, the cache should be busted for this palette instance.

3. **What does the user see in the palette RIGHT NOW?**
   - If `JS OK — waiting for Python data…` → sidecar XHR didn't work, bridge broken
   - If data rows are visible → one of the two mechanisms worked ✓
   - If `data received — N item(s)` but no rows → `renderTable()` has a bug (less likely)

---

## Next steps (if data still not showing after restart)

### Option A — Verify XHR in CEF
Add a minimal XHR test that writes the result to `loading-msg`:
```javascript
(function() {
  var el = document.getElementById('loading-msg');
  try {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', 'supplant_data.json', false);
    xhr.send();
    if (el) el.textContent = 'XHR status=' + xhr.status + ' len=' + xhr.responseText.length;
  } catch(e) {
    if (el) el.textContent = 'XHR ERROR: ' + e.message;
  }
})();
```
Run this immediately after the IIFE start (after the `JS OK` line). Deploy and check.

### Option B — Embed data in URL hash
When `_open_palette()` calls `palettes.add()`, encode the data as a URL hash:
```python
html_url = "file:///{}#{}".format(html_path.replace("\\", "/"),
                                  urllib.parse.quote(json.dumps(data)))
```
JS reads `window.location.hash` on load:
```javascript
var hashData = window.location.hash.slice(1);
if (hashData) window.fusionJavaScriptHandler.handle('data', decodeURIComponent(hashData));
```
**Limitation**: only usable at palette creation time, not on update. Too large for complex mappings.

### Option C — Use Fusion's `navigatingURL` event (read-only property)
Not viable — it's an event observer, not a settable property.

### Option D — Persist mapping across restarts
Write `_mapping`-equivalent data (token→geom pairs and occ names) to a JSON file on every
mapping change. After restart, read it back when `_open_palette()` is called.
This lets the palette show stale-but-useful data immediately on restart before the user
runs Setup.

---

## Useful MCP pattern for future debugging

```powershell
# 1. Initialize MCP session
$r = Invoke-WebRequest -Uri "http://127.0.0.1:27182/mcp" -Method Post `
  -ContentType "application/json" `
  -Headers @{ "Accept" = "application/json, text/event-stream" } `
  -Body '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"cc","version":"1"}}}' `
  -UseBasicParsing
$sid = $r.Headers["MCP-Session-Id"]

# 2. Send notifications/initialized
Invoke-WebRequest -Uri "http://127.0.0.1:27182/mcp" -Method Post `
  -ContentType "application/json" `
  -Headers @{ "Accept" = "application/json, text/event-stream"; "MCP-Session-Id" = $sid } `
  -Body '{"jsonrpc":"2.0","method":"notifications/initialized"}' -UseBasicParsing | Out-Null

# 3. Run a script
$script = [System.IO.File]::ReadAllText("C:\path\to\script.py", [System.Text.Encoding]::UTF8)
$payload = @{
    jsonrpc = "2.0"; id = 2; method = "tools/call"
    params = @{
        name = "fusion_mcp_execute"
        arguments = @{
            featureType = "script"
            object = @{ script = $script; readOnly = $false }  # false = write access
        }
    }
} | ConvertTo-Json -Depth 10
(Invoke-WebRequest -Uri "http://127.0.0.1:27182/mcp" -Method Post `
  -ContentType "application/json" `
  -Headers @{ "Accept" = "application/json, text/event-stream"; "MCP-Session-Id" = $sid } `
  -Body $payload -UseBasicParsing).Content
```

**Gotchas:**
- `readOnly: true` = safe read-only context; `sendInfoToHTML` may not deliver from this context
- `readOnly: false` = write access; must be used for any state changes or palette operations
- Scripts must be pure ASCII (no em-dashes, curly quotes, etc.) — Fusion's Python parser rejects
  non-printable characters. The Write tool adds UTF-8 BOM; use
  `[System.IO.File]::ReadAllText(path, [System.Text.Encoding]::UTF8)` to strip it.
- `palettes.itemById(PALETTE_ID)` always does a fresh lookup; don't rely on cached `_palette`
- `deleteMe()` works on Palette objects in this Fusion version
- `navigateTo()` does NOT exist on Palette
- `navigatingURL` is a NavigationEvent (observer), not a settable property

---

## MCP diagnostic scripts written this session
(all in `C:\Users\Aleksei\AppData\Local\Temp\`)

| File | Purpose |
|------|---------|
| `diag.py` | First diagnostic: check `_palette`, `_ph`, `_mapping` state |
| `push_data.py` | Recover `_palette`, call `_push_data_to_palette()` |
| `push2.py` | Full reload + re-init + push |
| `js_test.py` | Send `execute_result` to test bridge direction |
| `reload_palette.py` | Attempt `navigateTo` (failed: method doesn't exist) |
| `show_and_push.py` | Make palette visible + push data |
| `inspect_props.py` | List all Palette properties |
| `recreate_palette.py` | `deleteMe()` + `_open_palette()` + push |
| `reload_and_recreate.py` | Reload module + deleteMe + recreate + push + verify sidecar |
