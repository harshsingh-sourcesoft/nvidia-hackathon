# Per-Floor Wall Height — End-to-End

How a multi-floor building lets **each floor carry its own wall (storey) height**, from the
first-time "how tall is a floor" prompt, through per-floor overrides while uploading, to
editing any floor's height later — and how that number flows into the stacked 3D model and
the saved project.

All code lives in **`app/public/editor.html`** (one file). Line numbers below are approximate —
search by the function / element id.

---

## 1. Concept

* There is **one global default**, `_wallHeightM` (metres). A single-floor building only ever
  uses this.
* A multi-floor building additionally lets **any individual floor override** the default with
  its own `wallHeightM`. A floor that never sets one transparently falls back to `_wallHeightM`,
  so nothing changes for buildings/floors that don't touch the feature.
* The 3D builder reads every floor's *effective* height once, in floor order, and **stacks the
  floors cumulatively** — floor N+1 sits on top of floor N's actual height, not a fixed step.
  So a building can be `9 ft / 9 ft / 20 ft (atrium) / 9 ft` and the geometry is correct.

```
        ┌───────────────┐  ← roof at topFloorY
        │   Floor 3     │  wallHeightM = 6.0   (this floor overrides)
        ├───────────────┤  ← yBase[3] = Σ(h+slab) for floors 0..2
        │   Floor 2     │  wallHeightM = null → _wallHeightM (2.74)
        ├───────────────┤  ← yBase[2]
        │   Floor 1     │  wallHeightM = 2.74  (explicit, == default)
        ├───────────────┤  ← yBase[1]
        │   Floor 0/GF  │  wallHeightM = null → _wallHeightM
        └───────────────┘  ← yBase[0] = 0
```

---

## 2. Data model — where a floor's height lives

There are two multi-floor storage schemes; the height override lives on **whichever floor
object is active for that scheme**:

| Mode | Selector | Per-floor object | Override field |
|---|---|---|---|
| Multi-**building** upload (`_totalFloors ≥ 2`) | `_floorPlans[floorNum]` / `_stagedFloors[floorNum]` | one per uploaded/staged floor plan | `.wallHeightM` |
| Multi-**section** ("Floors" layout) | `_sections[idx]` | one per section | `.wallHeightM` |
| Single floor | — | — | uses `_wallHeightM` only |

`floorKey` is the key into whichever map is active: a 1-based `floorNum` in multi-building
mode, a 0-based section index in floor-mode, and `null` for a single-floor building.

```js
let _wallHeightM = 2.7432;   // global default, 9 ft in metres  (editor.html:1702)
// _floorPlans[3]  = { apiData, img, file, floorNum:3, wallHeightM: 6.0 }   ← override
// _floorPlans[2]  = { apiData, img, file, floorNum:2 }                     ← no override → falls back
```

---

## 3. Core helpers

```js
// editor.html ~4662 — resolve a floor's EFFECTIVE height (override or global fallback)
function _floorWallHeightM(floorKey){
  if(floorKey == null) return _wallHeightM;                       // single-floor building
  const store = _isMultiBldgUploadMode()
      ? (_floorPlans[floorKey] || _stagedFloors[floorKey])
      : _sections[floorKey];
  return (store && store.wallHeightM != null) ? store.wallHeightM : _wallHeightM;
}

// editor.html ~4667 — write a floor's override
function _setFloorWallHeightM(floorKey, valM){
  const store = _isMultiBldgUploadMode()
      ? (_floorPlans[floorKey] || _stagedFloors[floorKey])
      : _sections[floorKey];
  if(!store) return;
  store.wallHeightM = valM;
}

// editor.html ~4672 — unit conversion (UI works in ft/m/in, storage is always metres)
function _wallHtToUnit(m, unit){   return unit==='ft' ? m/0.3048 : unit==='in' ? m/0.0254 : m; }
function _wallHtFromUnit(v, unit){ return unit==='ft' ? v*0.3048 : unit==='in' ? v*0.0254 : v; }
```

`_setActiveFloorWallHeightM(valM)` (editor.html ~6866) is the "edit whatever floor is on
screen" wrapper — writes `_wallHeightM` when `_currentFloorKey()` is `null`, otherwise
`_setFloorWallHeightM(fk, valM)`. This is what the Wall Height panel's preset / custom
inputs call, so one panel edits "the building" or "the active floor" by context.

---

## 4. Flow A — Choosing height at building setup (the initial prompt)

When the user starts a multi-floor building, the **Building Setup modal** (`#buildingSetupModal`,
editor.html ~14168) asks for total floors + a single **Floor height (wall height)** with a
ft/m unit select:

```html
<div class="bsm-field">
  <label>Floor height (wall height)</label>
  <div class="bsm-row">
    <input type="number" id="modalFloorHeightVal" min="1" max="100" step="0.1" value="9" />
    <select id="modalFloorHeightUnit"><option value="ft" selected>ft</option><option value="m">m</option></select>
  </div>
</div>
```

`configureBuildingSetup()` (editor.html ~2258) turns that into the **global default** for the
whole building — it does **not** write per-floor overrides, so every floor starts equal and
inherits:

```js
function configureBuildingSetup(){
  const n     = Math.max(2, parseInt($('modalTotalFloors').value) || 2);
  const hVal  = parseFloat($('modalFloorHeightVal').value) || 9;
  const hUnit = $('modalFloorHeightUnit').value;
  const hm    = hUnit==='ft' ? hVal*0.3048 : hUnit==='in' ? hVal*0.0254 : hVal;

  _totalFloors = n;
  _wallHeightM = hm;                       // ← global default for the building
  // …stage the uploaded images per floor, showBuildingMgrUI()…
}
```

So: **the setup prompt sets the baseline every floor uses until you override one.**

---

## 5. Flow B — Per-floor height while uploading (Floor Manager grid)

Each floor card in the Floor Manager grid (`renderFloorMgrGrid()`, editor.html ~2319) has its
own height row (`.fmg-ht-row`) — a number input + ft/m/in select, pre-filled with that
floor's *effective* height:

```js
// editor.html ~2365
const syncHtInput = () => { htInput.value = _wallHtToUnit(_floorWallHeightM(fn), htUnit.value).toFixed(2); };
syncHtInput();
htInput.onchange = e => {
  e.stopPropagation();
  const raw = parseFloat(htInput.value);
  if(isNaN(raw) || raw <= 0){ syncHtInput(); return; }
  _setFloorWallHeightM(fn, _wallHtFromUnit(raw, htUnit.value));   // ← write THIS floor's override
  markStale();                                                    // ← queue a 3D rebuild
  if(fn === _selectedMgrFloor) updateWallHtDisplay();             // keep the Height panel in sync
};
```

Notes:
* Works on **staged** floors too (`_stagedFloors[fn]`), so a height set *before* clicking
  **Analyze** is preserved.
* The staged → analyzed handoff carries the value through:
  `handleFloorFile(file, floorNum, wallHeightM)` (editor.html ~2502) stores
  `wallHeightM: wallHeightM ?? _floorPlans[floorNum]?.wallHeightM` on the new
  `_floorPlans[floorNum]` object, and `btnMgrAnalyze` passes `sf.wallHeightM` when it calls it.

---

## 6. Flow C — Changing a floor's height later (Wall Height panel)

The **📏 Wall Height** flyout (`#rwHeightPanel`, editor.html ~1090) — reachable from both the
2D Edit toolbar and the 3D rail — is the "change it later" surface. Two zones:

### 6.1 Quick set — targets the *active* floor

```html
<div class="ht-btn-row">
  <button class="ht-btn" data-m="2.1336">7 ft</button>
  <button class="ht-btn" data-m="2.4384">8 ft</button>
  <button class="ht-btn active" data-m="2.7432">9 ft</button>
  <button class="ht-btn" data-m="3.048">10 ft</button>
  <button class="ht-btn" data-m="3.6576">12 ft</button>
</div>
<div class="rw-custom-row">
  <input class="dim-input" type="number" id="customHtInput" placeholder="Custom…" min="1" step="0.01"/>
  <select class="dim-select" id="customHtUnit"><option value="m">m</option><option value="ft">ft</option><option value="in">in</option></select>
  <button class="ht-btn" id="btnApplyCustomHt">Set</button>
</div>
<div class="rw-current" id="wallHtDisplay">9 ft (2.74 m)</div>
```

```js
// editor.html ~6940 — preset buttons
document.querySelectorAll('.ht-btn[data-m]').forEach(b => {
  b.addEventListener('click', () => {
    _setActiveFloorWallHeightM(parseFloat(b.dataset.m));   // active floor override (or global if single)
    updateWallHtDisplay();
    markStale();
  });
});

// editor.html ~6948 — custom value
$('btnApplyCustomHt').addEventListener('click', () => {
  const raw = parseFloat($('customHtInput').value);
  if(isNaN(raw) || raw <= 0) return;
  _setActiveFloorWallHeightM(_wallHtFromUnit(raw, $('customHtUnit').value));
  $('customHtInput').value = '';
  updateWallHtDisplay();
  markStale();
});
```

`updateWallHtDisplay()` (editor.html ~6923) re-titles the panel with the active floor's
label, updates the readout, highlights the matching preset button, and re-renders the
"All floors" list.

### 6.2 "All floors" list — edit **any** floor without switching to it

`_renderWallHtFloorList()` (editor.html ~6875) fills `#wallHtFloorList` (hidden for
single-floor buildings) with one editable row per floor:

```js
keys.forEach(fk => {
  // label = _floorKeyLabel(fk);  input pre-filled from _floorWallHeightM(fk)
  input.onchange = () => {
    const raw = parseFloat(input.value);
    if(isNaN(raw) || raw <= 0){ sync(); return; }
    _setFloorWallHeightM(fk, _wallHtFromUnit(raw, unit.value));   // ← per-floor override
    markStale();                                                  // ← rebuild 3D
    if(fk === _currentFloorKey()){ /* patch the Quick-set readout + active preset button in place */ }
    if(typeof renderFloorMgrGrid === 'function' && $('fmgCards')) renderFloorMgrGrid();  // keep the grid in sync
  };
});
```

**Every write path (setup, grid card, quick-set, all-floors list) ends the same way:**
write the number to storage → `markStale()`. `markStale()` sets `_3dStale = true` and, if the
3D view is currently visible, rebuilds it immediately; otherwise the rebuild happens on the
next switch to the 3D tab.

---

## 7. How the height drives the 3D model

Inside `build3D()` the effective height of every floor is resolved **once**, in order, into
`_floorStack` — the single source of truth for both the framing math and the render loop
(editor.html ~8137):

```js
const _floorKeysInOrder = isMultiFloorBldg ? _fpKeys
  : isFloorMode ? _sections.filter(s => s.apiData).map(s => _sections.indexOf(s))
  : [null];

let _cumY = 0;
const _floorStack = _floorKeysInOrder.map(fk => {
  const hM      = _floorWallHeightM(fk);                                   // ← per-floor height
  const fWALL_H = mpp ? hM : Math.min(FW, FD) * .32 * (hM / 2.5);          // metres, or fallback scale
  const fSLAB_T = Math.max(0.12, fWALL_H * .05);                          // this floor's own slab thickness
  const entry   = { floorKey: fk, wallHeightM: hM, WALL_H: fWALL_H, SLAB_T: fSLAB_T, yBase: _cumY };
  _cumY += fWALL_H + fSLAB_T;                                             // ← cumulative stacking
  return entry;
});
const totalBuildingHeight = _cumY;
const topFloorY = _floorStack.length
  ? _floorStack.at(-1).yBase + _floorStack.at(-1).WALL_H                   // roof sits on the top floor's own walls
  : WALL_H;
```

The render loop then passes each floor its own base + height (editor.html ~9400):

```js
_fpKeys.forEach((floorNum, fi) => {
  const fs = _floorStack[fi];
  const grp = renderFloorLevel(floorElems, fs.yBase, fi, fs.WALL_H);      // ← 4th arg = this floor's WALL_H
  floorGroups.push({ group: grp, idx: fi, yBase: fs.yBase, floorNum,
                     wallHeightM: fs.wallHeightM, WALL_H: fs.WALL_H });
});
```

Inside `renderFloorLevel(elements, yBase, floorIdx, wallHOverride)` the very first line
shadows `WALL_H` with the passed value, so **every** downstream wall / opening / mezzanine /
stair calculation on that floor automatically uses that floor's height:

```js
const WALL_H = wallHOverride != null ? wallHOverride : _floorDefaultWALL_H;
const SLAB_T = Math.max(0.12, WALL_H * .05);
```

`camera` framing, fog density, shadow-frustum size and roof position all derive from
`totalBuildingHeight` / `topFloorY`, so mixed-height buildings frame and light correctly
instead of assuming uniform spacing.

---

## 8. Persistence (save / open project)

Per-floor overrides ride along in the saved project because each floor's `wallHeightM` is
serialized with its floor-plan entry (`_plSerializeState` / `_plApplyProject`, editor.html
~13912 / ~14067):

```js
floorPlans[k] = {
  apiData:    fp.apiData || null,
  floorNum:   fp.floorNum ?? Number(k),
  imgDataUrl: fp.imgDataUrl || _plImgToDataUrl(fp.img),
  ...(fp.wallHeightM != null ? { wallHeightM: fp.wallHeightM } : {}),     // ← only written when overridden
};
```

The **global** `_wallHeightM` is *not* currently persisted separately — on reload it resets
to its default (2.7432 m), and any floor that had only inherited it will read that default.
Floors with an explicit override are restored exactly. (If a project needs the global
default preserved, add `wallHeightM: _wallHeightM` to `_plSerializeState()` and restore it in
`_plApplyProject()`.)

---

## 9. Downstream consumers

* **Isaac Sim export** — the generated inject script's `FLOORS_META` includes each floor's
  `wallHeightM` (read from the built `floorGroups`, i.e. `_floorStack`), so the Kit side
  places floor-plan image planes and floor-view cameras at the real stacked heights.
* **Room-agent data** (`_buildRoomsDataForAgent()`, editor.html ~4684) reports each labelled
  room's `wz` (its floor's `yBase`) and `wallHeightM` straight off `_three.floorGroups`, never
  a parallel formula — so it can't drift from the geometry.
* **Floor Manager bar** shows `Floor N height: X ft` using `_floorWallHeightM(_selectedMgrFloor)`.

---

## 10. Edge cases & guarantees

| Situation | Behaviour |
|---|---|
| Single-floor building | `floorKey == null` → always `_wallHeightM`; the "All floors" list and per-card height row are hidden. |
| Floor never given an override | `store.wallHeightM` is `undefined` → falls back to `_wallHeightM`. |
| Height set on a **staged** floor, then Analyze | Preserved via `handleFloorFile(..., wallHeightM)` and the `?? _floorPlans[floorNum]?.wallHeightM` fallback. |
| Re-analyze one floor (incremental) | `_floorPlans[fn]` object is rebuilt but the new object copies the prior `wallHeightM`. |
| Uncalibrated plan (no `mpp`) | `_floorStack` converts each `hM` through the `Math.min(FW,FD)*.32*(hM/2.5)` proportional fallback instead of treating it as metres. |
| Two panels open at once (grid + Height panel) | Each write calls the other's re-render (`updateWallHtDisplay` ↔ `renderFloorMgrGrid`) so they stay in sync; the active-floor row patches in place to avoid tearing out the input being typed into. |

---

## 11. Quick reference

### State
| Symbol | Meaning |
|---|---|
| `_wallHeightM` | global default height, metres |
| `_floorPlans[n].wallHeightM` / `_stagedFloors[n].wallHeightM` / `_sections[i].wallHeightM` | per-floor override, metres (absent = inherit) |
| `_floorStack[i]` | build-time `{ floorKey, wallHeightM, WALL_H, SLAB_T, yBase }` per floor |

### Functions
| Function | Role |
|---|---|
| `_floorWallHeightM(floorKey)` | resolve effective height (override → global) |
| `_setFloorWallHeightM(floorKey, m)` | write a floor's override |
| `_setActiveFloorWallHeightM(m)` | write "current floor" (or global if single) |
| `_wallHtToUnit(m, unit)` / `_wallHtFromUnit(v, unit)` | metres ↔ ft/m/in |
| `configureBuildingSetup()` | setup modal → sets `_totalFloors` + global `_wallHeightM` |
| `renderFloorMgrGrid()` | Floor Manager cards incl. per-card height row |
| `_renderWallHtFloorList()` | "All floors" editable list in the Height panel |
| `updateWallHtDisplay()` | refresh the Height panel readout / presets / list |
| `markStale()` | mark 3D dirty → rebuild now if visible, else on next 3D tab |
| `build3D()` → `_floorStack` → `renderFloorLevel(_, yBase, i, WALL_H)` | consume heights, stack floors |

### DOM
| id / class | Where |
|---|---|
| `#modalFloorHeightVal`, `#modalFloorHeightUnit` | Building Setup modal |
| `.fmg-ht-row`, `.fmg-ht-input`, `.fmg-ht-unit` | Floor Manager card |
| `#rwHeightPanel` | Wall Height flyout panel |
| `.ht-btn[data-m]`, `#customHtInput`, `#customHtUnit`, `#btnApplyCustomHt` | Height panel "Quick set" |
| `#wallHtFloorList`, `#wallHtListLabel` | Height panel "All floors" list |
| `#wallHtDisplay`, `#wallHtPanelTitle`, `#wallHtQuickLabel` | Height panel readouts |
