# Physical and touch controllers through XInput

Paired GameController devices and the existing landscape touch editor feed
Windows games through a shared XInput snapshot. Up to four physical extended
profiles have stable slots; disconnecting one does not renumber the others.
Touch input merges into player 1 (slot 0).

## Physical input and transport

The app captures live profiles and samples them on a serial queue at 250 Hz
(4 ms with 1 ms scheduling leeway), with change callbacks for prompt updates.
The timer stops while inactive or with no connected physical pads. Inactive
controllers remain connected but report neutral input. Disconnects clear slots
unless the touch layout still supplies slot 0. Buttons, triggers, and signed
stick axes reach XInput without mouse synthesis or an app-imposed dead zone.
iOS 18 event claims prevent focus navigation from consuming controller events.

`WiniosGamepad.c` publishes snapshots under a short mutex. Packet numbers change
only when the sample or connection changes. The win32u query copies state or
capabilities into the Windows caller's buffer. Wine's paired XInput change tries
this query before its existing HID path.

## Touch controls

In the existing landscape touch editor, assign a control using its Controller
tab. Saved `pad` names remain compatible: A/B/X/Y, D-pad directions, LB/RB,
L3/R3, Menu/View/Guide, LT/RT and LS/RS. LT/RT are full-press triggers; LS/RS are
analogue sticks with radial clamping and the full signed XInput range. Stick
motion is relative to the initial touch position. Guide follows Wine's existing
XInput filtering rules; not every API/game exposes it.

A visible, non-editing landscape layout with at least one supported controller
mapping connects a virtual player 1, even before a button is pressed. Hiding
controls, entering editing, switching to portrait, removing the overlay or
remapping releases input. Backgrounding releases holds while retaining the
connected identity. UIKit handles independent fingers and cancellation, so
releasing one of two controls mapped to the same button leaves the other held.
The pinch recognizer is enabled only during editing.

Touch and physical buttons combine; triggers use the larger value. A physical
stick outside its standard XInput dead zone takes priority over touch on that
stick. Otherwise a deflected touch stick takes priority; resting touch preserves
the physical value. Touch updates are event-driven and require no polling timer.
Keyboard/mouse mappings and the existing layout format are retained.

## Rollback and scope

Set `env.MADEIRA_XINPUT = 0` in Documents/madeira.cfg and restart to disable the
whole producer and controller event claims. Set `env.MADEIRA_TOUCH_XINPUT = 0`
to disable only touch gamepad input. `[xinput] ml1920` logs physical enablement
and connections; `[touch-xinput] ml1930` logs touch enablement once.

Vibration, battery telemetry, DirectInput, controller-driven library navigation
and the fork's larger editor/remapping redesign are outside this contribution.

## Integration prerequisite

Pair this with [the Wine change](https://github.com/willfaust/wine/pull/1), then
update Madeira's Wine pin. This source-review PR retains upstream's submodule
pins and prebuilt DLLs until the dependency and combined build are validated.

Rebuild the native win32u library and affected PE win32u/XInput modules using
the paired Wine source. Rebuild wow64win for a WOW64 configuration. XInput
1.1/1.2/1.3/1.4/UAP share the implementation; 9.1.0 forwards to 1.4. Copying just
the app changes over the existing prebuilt DLLs will not enable the feature.
No binaries from the larger fork are included here.

## Validation

Run on a POSIX host with a C compiler and Swift installed:

```sh
python3 build/host-tests/check-gamepad.py
python3 build/host-tests/check-touch-gamepad.py
```

The first compiles production snapshot/query code and checks packets, ranges,
slots, invalid queries, disconnect/reconnect and concurrent readers/writers.
The second compiles production touch state and checks independent button holds,
layout/lifecycle clearing, analogue ranges, duplicate sticks and physical/touch
arbitration. Set `SWIFTC` if Swift is not on PATH. These are logic tests, not
UIKit gesture or device integration tests.

The Swift bridge, touch view and changed touch-editor section type-check against
the arm64 iOS 17 SDK with the production config reader and stubs for unrelated
app UI/logging/input sinks. The C transport compiles for arm64 iOS 17. Companion
Wine XInput source compiles for x86-64/i386; its standalone native Windows API
test passes using a synthetic host query.

The full combined upstream app has not been linked or device-tested. Before
merge, rebuild the paired components and test:

- Physical buttons, sticks, triggers, disconnect/reconnect and multiple pads.
- Touch-only player 1, both sticks plus buttons together, and duplicate mappings.
- Mixed physical/touch holds; releasing either source must preserve the other.
- Hold then hide, edit, remap, remove, rotate, background or interrupt the app;
  no input should remain stuck, and fresh touches should work afterward.
- Both rollback flags, saved layouts, and existing keyboard/mouse controls.

The fork's existing device history does not prove this isolated extraction.
