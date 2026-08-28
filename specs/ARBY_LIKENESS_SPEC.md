# Arby Likeness Update — Cline Implementation Spec

## Goal
Make Arby (`agent/mascot.py`) visually recognizable as Jason: bald head, his
skin tone, gray goatee, navy blazer — using his actual photo as the head
texture instead of the current flat-color procedural cartoon face.

## What this trades off
Today `drawFace()` procedurally draws brows/eyes/mouth every frame so Arby's
expression reacts to `state.moodScore` (raised brows when cold, arched eyes
when hot, blink cycle, etc.). A real photo can't be redrawn like that without
looking broken. This spec keeps the photo static and swaps mood-reactivity to
a subtle color-tint wash + a positioned blink, rather than redrawn features.
The mood pill in the HUD (`#arby-mood`) already carries the explicit mood
label, so this isn't a total loss of signal — just flag it to Jason as an
intentional trade before shipping, since it changes a functional behavior
he built deliberately.

## Asset
Photo was cropped to the head (hairline to below the goatee), resized to
128x128 to match the existing `faceCv` canvas size, and re-encoded as JPEG
q80 to keep it small (~15KB base64) so the widget stays self-contained per
the file's own docstring ("no shared imports", inlined by `delivery.py`).
A standalone copy is also provided at `arby_face.jpg` in case Jason prefers
serving it from a static path instead of inlining — inlining is recommended
here since `mascot.py` explicitly commits to zero external dependencies
beyond the Three.js CDN.

Sampled from the photo for the other material colors:
- Skin (avg of forehead/both cheeks): `#c68a60`
- Blazer: `#2c2f40` (navy)
- Goatee: baked into the photo itself — no separate 3D goatee geometry needed

## Step 1 — Update SKIN constant
File: `agent/mascot.py`, in the `// ---- character (Arby)` block.

OLD:
```js
var SKIN = 0x6b4a33;
var shirtMat = new THREE.MeshStandardMaterial({color:0x58a6ff});
```

NEW:
```js
var SKIN = 0xc68a60;
var shirtMat = new THREE.MeshStandardMaterial({color:0x2c2f40}); // navy blazer
```

This alone fixes the hands (`hand` mesh already uses `color:SKIN`) and the
5 non-face sides of the head box (`skinMat`), plus the torso/arm color.
Grep the file for any other literal `SKIN` or `0x6b4a33` reference (e.g. ears,
neck trim, if any exist further down) and update those too — the search
above only surfaced the primary declarations.

## Step 2 — Load the photo as an Image, before the head-building block
Insert immediately before the `// head with an expressive canvas face` comment:

```js
// Real-photo face texture (Jason). Kept as a plain Image + canvas draw
// (not a THREE.TextureLoader) so drawFace() can composite mood tint / blink
// on top of it each call, same as the old procedural version did.
var ARBY_FACE_B64 = "PLACEHOLDER_DATA_URI";
var arbyFaceImg = new Image();
var arbyFaceReady = false;
arbyFaceImg.onload = function(){ arbyFaceReady = true; drawFace(); };
arbyFaceImg.src = ARBY_FACE_B64;
```

(The real `ARBY_FACE_B64` value — the full data URI — is in
`arby_face_data_uri.txt` alongside this spec. It's ~15KB of text; paste it in
verbatim in place of `"PLACEHOLDER_DATA_URI"`.)

## Step 3 — Replace drawFace()
This is the core change. Same function signature and call sites (`drawFace()`
is already called from `registerWin`, `registerLoss`, `reset`,
`setBlendedMood`, and the blink timer — none of those call sites need to
change).

OLD:
```js
  function drawFace(){
    var m = state.moodScore;
    var x = faceCtx;
    x.fillStyle='#'+SKIN.toString(16); x.fillRect(0,0,128,128);
    // beard + sideburns
    x.fillStyle='#17110b';
    x.fillRect(0,92,128,36);
    x.fillRect(0,58,12,40); x.fillRect(116,58,12,40);
    x.fillRect(30,84,68,10); // mustache
    // brows
    var tilt = m<-0.5 ? 0.35 : m<-0.15 ? -0.2 : m>0.5 ? -0.12 : 0;
    [[40,1],[88,-1]].forEach(function(bp){
      x.save(); x.translate(bp[0],34); x.rotate(tilt*bp[1]);
      x.fillStyle='#17110b'; x.fillRect(-13,-4,26,7);
      x.restore();
    });
    // eyes
    if (blinkClosed){
      x.strokeStyle='#17110b'; x.lineWidth=3;
      [[40],[88]].forEach(function(ep){ x.beginPath(); x.moveTo(ep[0]-9,52); x.lineTo(ep[0]+9,52); x.stroke(); });
    } else if (m>0.55){
      x.strokeStyle='#17110b'; x.lineWidth=4;
      [[40],[88]].forEach(function(ep){ x.beginPath(); x.arc(ep[0],54,9,Math.PI*1.05,Math.PI*1.95); x.stroke(); });
    } else {
      [[40],[88]].forEach(function(ep){
        x.fillStyle='#f6f8fa'; x.fillRect(ep[0]-9,45,18,14);
        x.fillStyle='#0d1117'; x.fillRect(ep[0]-3,48,7,9);
      });
    }
    // mouth (drawn on skin just above the beard band)
    x.strokeStyle='#17110b'; x.lineWidth=3; x.beginPath();
    var curve = clamp(m,-1,1)*8;
    x.moveTo(48,80-curve*0.2);
    x.quadraticCurveTo(64,80+curve,80,80-curve*0.2);
    x.stroke();
    faceTex.needsUpdate = true;
  }
```

NEW:
```js
  function drawFace(){
    var m = state.moodScore;
    var x = faceCtx;
    x.clearRect(0,0,128,128);
    if (arbyFaceReady){
      x.drawImage(arbyFaceImg,0,0,128,128);
    } else {
      // fallback before the image loads on first paint
      x.fillStyle='#'+SKIN.toString(16); x.fillRect(0,0,128,128);
    }
    // mood tint wash — replaces the old redrawn brows/eyes/mouth, which
    // can't be composited onto a real photo without looking broken. The
    // HUD mood pill (#arby-mood) still carries the explicit label.
    var tint = m>0.4 ? 'rgba(63,185,80,0.16)' : m<-0.4 ? 'rgba(248,81,73,0.18)' : null;
    if (tint){ x.fillStyle=tint; x.fillRect(0,0,128,128); }
    // blink, positioned to this photo's actual eye locations at 128x128
    if (blinkClosed){
      x.strokeStyle='rgba(23,17,11,0.85)'; x.lineWidth=3;
      [[44,70],[86,68]].forEach(function(ep){
        x.beginPath(); x.moveTo(ep[0]-9,ep[1]); x.lineTo(ep[0]+9,ep[1]); x.stroke();
      });
    }
    faceTex.needsUpdate = true;
  }
```

Note: `drawFace()` is called once synchronously right after it's defined
(`drawFace();` a few lines down, before `skinMat`/`faceMat` are built) — on
that first call `arbyFaceReady` will still be false (image loads async), so
it'll paint the flat-skin fallback for one frame, then repaint once
`arbyFaceImg.onload` fires. That's expected and fine.

## Step 4 — Verify
1. Load both dashboards (static + hosted) and confirm the head shows the
   photo, not the old cartoon face, in both the idle and fullscreen views.
2. Trigger `Arby.registerWin(true)` and `Arby.registerLoss(true)` from the
   console — confirm the green/red tint wash appears and fades back near
   neutral, and the mood pill text still updates independently.
3. Watch through a couple of blink cycles — confirm the blink lines land
   roughly on the photo's eyes, not floating elsewhere on the face. If
   they're off, the crop-to-canvas eye coordinates (44,70) and (86,68) may
   need nudging by a few px — that's a fast iterate-and-eyeball fix, not a
   logic issue.
4. Confirm the no-WebGL fallback path (`if (!renderer)`) is untouched — it
   never reaches `drawFace()`, so nothing here should affect it.
5. Sanity-check total payload size didn't meaningfully change page weight —
   the base64 asset is ~15KB, negligible next to the rest of the inlined
   widget.
6. Grep the file once more for any other literal `0x6b4a33` or `0x58a6ff`
   Jason wants carried over consistently (e.g. if either color is reused for
   a UI accent elsewhere in `_SNIPPET`'s CSS) — those are cosmetic calls for
   Jason, not required for correctness.

## Files provided alongside this spec
- `arby_face.jpg` — the processed 128x128 texture (also embeddable as-is if
  you'd rather serve it from a static path than inline it)
- `arby_face_data_uri.txt` — the full `data:image/jpeg;base64,...` string to
  paste into Step 2
