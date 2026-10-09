#!/usr/bin/env python3
"""XInput pads as Windows.Gaming.Input gamepads (tools/patch-wine-wgi-host-pads.py); no Wine runs.

Horizon Zero Dawn (build 471, 2026-10-09 15:01) reads its controller only
through Windows.Gaming.Input.Gamepad, which Wine feeds from HID devices alone.
The patch adds a provider per connected XInput slot. Checks:
  - the patch applies to the submodule's windows.gaming.input and a second run
    says "already patched";
  - main.c polls once with the HID providers and waits through
    madeira_host_pads_wait; without MADEIRA_WGI_HOST_PADS=1 that wait is the
    old INFINITE one and nothing else runs;
  - compiled: the patch's XINPUT_STATE conversion followed by Wine's own
    gamepad.c reading code gives the GamepadReading Windows gives for the same
    XInput state (buttons, d-pad, both sticks with up positive, triggers);
  - tools/build-wine-extra-dlls.sh builds windows.gaming.input with the patch,
    restores the sources and replaces the shipped DLL only when it built.
Needs python3 and a C compiler.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
wgi = root / 'wine/dlls/windows.gaming.input'
script = root / 'tools/patch-wine-wgi-host-pads.py'


def function(source, signature, closing='\n}\n'):
    start = source.index(signature)
    return source[start:source.index(closing, start) + len(closing)]


with tempfile.TemporaryDirectory(prefix='madeira-wgi-') as directory:
    temporary = Path(directory)
    copy = temporary / 'windows.gaming.input'
    copy.mkdir()
    for name in ('provider.c', 'main.c'):
        shutil.copy(wgi / name, copy / name)
    first = subprocess.run(['python3', str(script), str(copy)], capture_output=True, text=True, check=True).stdout
    assert 'patched windows.gaming.input' in first, first
    second = subprocess.run(['python3', str(script), str(copy)], capture_output=True, text=True, check=True).stdout
    assert 'already patched' in second, second
    provider = (copy / 'provider.c').read_text()
    main = (copy / 'main.c').read_text()
    assert provider.startswith((wgi / 'provider.c').read_text().rstrip('\n')), 'the HID providers are untouched'
    print('PASS: the patch applies to the submodule and is idempotent')

    init = function(main, 'static void initialize_providers( void )')
    assert init.rstrip().endswith('SetupDiDestroyDeviceInfoList( set );\n    madeira_host_pads_poll();\n}'), \
        'the first poll runs with the HID providers, before DllGetActivationFactory returns'
    assert '} while (madeira_host_pads_wait());' in main and 'INFINITE, QS_ALLINPUT' not in main
    wait = function(provider, 'BOOL madeira_host_pads_wait( void )')
    assert 'on ? 500 : INFINITE' in wait, 'without the switch the monitor thread waits as before'
    assert 'return ret == WAIT_OBJECT_0 || (on && ret == WAIT_TIMEOUT);' in wait, \
        'the loop ends on what ended it before'
    enable = function(provider, 'static BOOL WINAPI madeira_host_pads_init(')
    assert 'GetEnvironmentVariableW( L"MADEIRA_WGI_HOST_PADS"' in enable and 'wcscmp( value, L"1" )' in enable, \
        'opt-in: exactly MADEIRA_WGI_HOST_PADS=1'
    assert enable.index('wcscmp( value, L"1" )') < enable.index('LoadLibraryW( L"xinput1_4.dll" )'), \
        'nothing is loaded without the switch'
    poll = function(provider, 'void madeira_host_pads_poll( void )')
    assert poll.index('if (!madeira_host_pads_enabled()) return;') < poll.index('madeira_xget('), \
        'no XInput call without the switch'
    assert 'manager_on_provider_created( &impl->IGameControllerProvider_iface );' in poll
    assert 'manager_on_provider_removed( &impl->IGameControllerProvider_iface );' in poll
    assert '*value = WineGameControllerType_Gamepad;' in provider
    vibration = function(provider, 'static HRESULT WINAPI host_provider_put_Vibration(')
    assert 'struct madeira_xvibration motors = { value.rumble, value.buzz };' in vibration and 'madeira_xset(' in vibration
    print('PASS: opt-in (MADEIRA_WGI_HOST_PADS=1), first poll with the HID providers, a poll every 500 ms, '
          'Gamepad type, rumble to XInputSetState')

    # The conversion, followed by Wine's own reading code from gamepad.c.
    xstate = function(provider, 'struct madeira_xstate          /* XINPUT_STATE */', '\n};\n')
    axis = function(provider, 'static DOUBLE madeira_host_axis( SHORT value )')
    convert = function(provider, 'static void madeira_host_state(')
    gamepad = (wgi / 'gamepad.c').read_text()
    reading = gamepad[gamepad.index('        impl->state_changed = TRUE;\n'):]
    reading = reading[:reading.index('        value->Timestamp = state.timestamp;')]
    reading = reading.replace('        impl->state_changed = TRUE;\n', '')
    harness = r'''
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t DWORD; typedef uint16_t WORD; typedef uint8_t BYTE; typedef int16_t SHORT;
typedef double DOUBLE; typedef unsigned char BOOLEAN; typedef unsigned int UINT; typedef uint64_t UINT64;
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
typedef enum
{
    GameControllerSwitchPosition_Center, GameControllerSwitchPosition_Up, GameControllerSwitchPosition_UpRight,
    GameControllerSwitchPosition_Right, GameControllerSwitchPosition_DownRight, GameControllerSwitchPosition_Down,
    GameControllerSwitchPosition_DownLeft, GameControllerSwitchPosition_Left, GameControllerSwitchPosition_UpLeft,
} GameControllerSwitchPosition;
struct WineGameControllerState { UINT64 timestamp; DOUBLE axes[32]; BOOLEAN buttons[128]; GameControllerSwitchPosition switches[4]; };
/* Windows.Gaming.Input.GamepadButtons */
enum { GamepadButtons_Menu = 0x1, GamepadButtons_View = 0x2, GamepadButtons_A = 0x4, GamepadButtons_B = 0x8,
       GamepadButtons_X = 0x10, GamepadButtons_Y = 0x20, GamepadButtons_DPadUp = 0x40, GamepadButtons_DPadDown = 0x80,
       GamepadButtons_DPadLeft = 0x100, GamepadButtons_DPadRight = 0x200, GamepadButtons_LeftShoulder = 0x400,
       GamepadButtons_RightShoulder = 0x800, GamepadButtons_LeftThumbstick = 0x1000,
       GamepadButtons_RightThumbstick = 0x2000 };
struct GamepadReading { UINT64 Timestamp; unsigned Buttons; DOUBLE LeftTrigger, RightTrigger, LeftThumbstickX,
                        LeftThumbstickY, RightThumbstickX, RightThumbstickY; };
#define FAIL(...) do { fprintf(stderr, __VA_ARGS__); exit(1); } while (0)
''' + xstate + axis + convert + r'''
static struct GamepadReading read_pad( WORD buttons, BYTE lt, BYTE rt, SHORT lx, SHORT ly, SHORT rx, SHORT ry )
{
    struct madeira_xstate in = { 1, buttons, lt, rt, lx, ly, rx, ry };
    struct WineGameControllerState state;
    struct GamepadReading reading = {0}, *value = &reading;

    madeira_host_state( &in, &state );
    {
''' + reading + r'''
    }
    return reading;
}

static int near( double a, double b ) { return fabs( a - b ) < 1e-4; }

int main( void )
{
    /* XInput button bits and the GamepadButtons Windows reports for them */
    static const struct { WORD bit; unsigned want; } buttons[] =
    {
        { 0x1000, GamepadButtons_A }, { 0x2000, GamepadButtons_B }, { 0x4000, GamepadButtons_X },
        { 0x8000, GamepadButtons_Y }, { 0x0100, GamepadButtons_LeftShoulder },
        { 0x0200, GamepadButtons_RightShoulder }, { 0x0020, GamepadButtons_View }, { 0x0010, GamepadButtons_Menu },
        { 0x0040, GamepadButtons_LeftThumbstick }, { 0x0080, GamepadButtons_RightThumbstick },
        { 0x0001, GamepadButtons_DPadUp }, { 0x0002, GamepadButtons_DPadDown },
        { 0x0004, GamepadButtons_DPadLeft }, { 0x0008, GamepadButtons_DPadRight },
        { 0x0009, GamepadButtons_DPadUp | GamepadButtons_DPadRight },
        { 0x0006, GamepadButtons_DPadDown | GamepadButtons_DPadLeft },
        { 0x0005, GamepadButtons_DPadUp | GamepadButtons_DPadLeft },
        { 0x000a, GamepadButtons_DPadDown | GamepadButtons_DPadRight },
        { 0x1000 | 0x0200 | 0x0008, GamepadButtons_A | GamepadButtons_RightShoulder | GamepadButtons_DPadRight },
    };
    struct GamepadReading r;
    unsigned i;

    r = read_pad( 0, 0, 0, 0, 0, 0, 0 );
    if (r.Buttons || !near( r.LeftThumbstickX, 0 ) || !near( r.LeftThumbstickY, 0 ) || !near( r.RightThumbstickX, 0 ) ||
        !near( r.RightThumbstickY, 0 ) || r.LeftTrigger != 0 || r.RightTrigger != 0)
        FAIL( "a pad at rest reads %#x %f %f %f %f %f %f\n", r.Buttons, r.LeftThumbstickX, r.LeftThumbstickY,
              r.RightThumbstickX, r.RightThumbstickY, r.LeftTrigger, r.RightTrigger );
    for (i = 0; i < ARRAY_SIZE(buttons); i++)
    {
        r = read_pad( buttons[i].bit, 0, 0, 0, 0, 0, 0 );
        if (r.Buttons != buttons[i].want) FAIL( "XInput %#x reads as %#x, not %#x\n", buttons[i].bit, r.Buttons, buttons[i].want );
    }
    printf( "PASS: every XInput button and d-pad direction reads as the GamepadButtons Windows gives\n" );

    r = read_pad( 0, 0, 0, 32767, 0, 0, 0 );
    if (!near( r.LeftThumbstickX, 1 ) || !near( r.LeftThumbstickY, 0 )) FAIL( "left stick right: %f %f\n", r.LeftThumbstickX, r.LeftThumbstickY );
    r = read_pad( 0, 0, 0, -32768, 0, 0, 0 );
    if (!near( r.LeftThumbstickX, -1 )) FAIL( "left stick left: %f\n", r.LeftThumbstickX );
    r = read_pad( 0, 0, 0, 0, 32767, 0, 0 );
    if (!near( r.LeftThumbstickY, 1 ) || !near( r.LeftThumbstickX, 0 )) FAIL( "left stick up: %f %f\n", r.LeftThumbstickX, r.LeftThumbstickY );
    r = read_pad( 0, 0, 0, 0, -32768, 0, 0 );
    if (!near( r.LeftThumbstickY, -1 )) FAIL( "left stick down: %f\n", r.LeftThumbstickY );
    r = read_pad( 0, 0, 0, 0, 0, 16384, -16384 );
    if (!near( r.RightThumbstickX, 16384 / 32767. ) || !near( r.RightThumbstickY, -0.5 ))
        FAIL( "right stick half right, half down: %f %f\n", r.RightThumbstickX, r.RightThumbstickY );
    r = read_pad( 0, 255, 128, 0, 0, 0, 0 );
    if (!near( r.LeftTrigger, 1 ) || !near( r.RightTrigger, 128 / 255. )) FAIL( "triggers: %f %f\n", r.LeftTrigger, r.RightTrigger );
    printf( "PASS: sticks (up positive, as in XInput and Windows.Gaming.Input) and triggers keep their values\n" );
    return 0;
}
'''
    source = temporary / 'wgi.c'
    source.write_text(harness)
    binary = temporary / 'wgi'
    flags = ['-std=c11', '-Wall', '-Werror']
    if shutil.which(os.environ.get('CC', 'cc')):
        subprocess.run([os.environ.get('CC', 'cc'), *flags, str(source), '-lm', '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
    else:
        print('SKIP: no C compiler')

build = (root / 'tools/build-wine-extra-dlls.sh').read_text()
assert 'targets="$targets dlls/windows.gaming.input/arm64ec-windows/windows.gaming.input.dll"' in build
assert 'python3 "$R/tools/patch-wine-wgi-host-pads.py" "$R/wine/dlls/windows.gaming.input" && wgi_patched=1' in build
assert 'dlls/windows.gaming.input/provider.c dlls/windows.gaming.input/main.c' in build, 'the sources are restored'
copy_step = build[build.index('if [ "$wgi_patched" = 1 ]; then'):]
copy_step = copy_step[:copy_step.index('built=0; failed=""')]
assert 'if [ -f "$f" ]; then' in copy_step and 'mv "$SHIP/windows.gaming.input.dll.tmp" "$SHIP/windows.gaming.input.dll"' in copy_step
assert 'shipped DLL kept' in copy_step, 'a failed build keeps the shipped DLL'
assert build.index('python3 "$R/tools/patch-wine-wgi-host-pads.py"') < build.index('make -C "$B" -k -j"$JOBS" $targets')
print('PASS: build-wine-extra-dlls.sh builds windows.gaming.input with the patch, restores the sources and '
      'replaces the shipped DLL only when it built')
