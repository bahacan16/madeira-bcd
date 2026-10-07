#!/usr/bin/env python3
"""ml2106: XInput rumble reaches the host pad.

Wine's xinput (dlls/xinput1_3/main.c, shared by xinput1_1/1_2/1_4 through
PARENTSRC; xinput9_1_0 forwards to xinput1_4) answers XInputSetState for a
host pad (Madeira's ml668 path) with ERROR_SUCCESS and drops the motors. This
patch passes them on through the same win32u call the host path already uses:
NtUserGetGamepadState(index, 2, &vibration), op 2 being this fork's
"set vibration" (build/win32u-unix/driver_ios.c ios_gamepad_query), which
stores them for the app (WiniosGamepad.c winios_gamepad_set_vibration) to play
on the controller (app/Madeira/PadOutput.m). XINPUT_VIBRATION is two WORDs in
32- and 64-bit alike, so wow64win's existing pointer translation for this call
is the whole marshalling. A win32u that does not know op 2 answers 0, which
this code ignores.

XInputEnable(FALSE) silences the motors and XInputEnable(TRUE) sends the last
values again, as Windows does; while disabled XInputSetState only records.
When the game process ends normally (DLL_PROCESS_DETACH) running motors are
stopped, as Windows' driver does when the process goes away.

Usage: patch-wine-xinput-vibration.py wine/dlls/xinput1_3/main.c
(idempotent; tools/build-wine-extra-dlls.sh applies it around its xinput
build and restores the file afterwards.)
"""
import sys

path = sys.argv[1]
src = open(path).read()
if "madeira_host_vibration" in src:
    print("already patched"); sys.exit(0)
# willfaust/wine#20 (67b8c8b, wine 257f271): the same host rumble upstream,
# with op 2 named NtUserGamepadOp_SetVibration in include/ntuser.h.
if "NtUserGamepadOp_SetVibration" in src and "host_vibration[XUSER_MAX_COUNT]" in src:
    print("xinput host rumble is upstream (willfaust/wine#20); nothing to do"); sys.exit(0)

helper = r'''
/* madeira-bcd ml2106: see tools/patch-wine-xinput-vibration.py. */
static XINPUT_VIBRATION madeira_host_vibration[XUSER_MAX_COUNT];

static void madeira_host_vibrate(DWORD index)
{
    XINPUT_VIBRATION motors = {0};

    if (InterlockedCompareExchange(&host_enabled, 0, 0)) motors = madeira_host_vibration[index];
    NtUserGetGamepadState(index, 2 /* madeira-bcd: set vibration */, &motors);
}
'''
anchor = "/* Is the host publishing ANY pad?"
if src.count(anchor) != 1:
    sys.exit("patch-wine-xinput-vibration: host_pad_any anchor not found")
src = src.replace(anchor, helper.lstrip("\n") + "\n" + anchor)

old = '''    InterlockedExchange(&host_enabled, !!enable);
    if (host_pad_any()) return;
'''
new = '''    InterlockedExchange(&host_enabled, !!enable);
    if (host_pad_any())
    {
        XINPUT_STATE host_state;

        for (index = 0; index < XUSER_MAX_COUNT; index++)
            if (host_pad_state(index, &host_state)) madeira_host_vibrate(index);
        return;
    }
'''
if src.count(old) != 1:
    sys.exit("patch-wine-xinput-vibration: XInputEnable anchor not found")
src = src.replace(old, new)

old = '''    case DLL_PROCESS_ATTACH:
        xinput_instance = inst;
        DisableThreadLibraryCalls(inst);
        break;
'''
new = old + '''    case DLL_PROCESS_DETACH:
    {
        /* madeira-bcd: a game that exits with its motors running must not
         * leave the pad rumbling (Windows' driver stops them with the
         * process); the app session outlives the game. */
        static const XINPUT_VIBRATION off;
        DWORD i;

        for (i = 0; i < XUSER_MAX_COUNT; i++)
            if (madeira_host_vibration[i].wLeftMotorSpeed || madeira_host_vibration[i].wRightMotorSpeed)
                NtUserGetGamepadState(i, 2, (void *)&off);
        break;
    }
'''
if src.count(old) != 1:
    sys.exit("patch-wine-xinput-vibration: DllMain anchor not found")
src = src.replace(old, new)

old = '''    /* Host capabilities do not advertise force feedback. */
    if (host_pad_state(index, &host_state)) return ERROR_SUCCESS;
'''
new = '''    if (host_pad_state(index, &host_state))
    {
        madeira_host_vibration[index] = *vibration;
        madeira_host_vibrate(index);
        return ERROR_SUCCESS;
    }
'''
if src.count(old) != 1:
    sys.exit("patch-wine-xinput-vibration: XInputSetState anchor not found")
src = src.replace(old, new)
open(path, "w").write(src)
print("patched " + path)
