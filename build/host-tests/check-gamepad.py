#!/usr/bin/env python3
"""Compile the production snapshot transport and win32u query on a POSIX host.

No Wine, game, controller, SDK or credentials are needed. Run with python3.
"""
from pathlib import Path
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
driver = (root / 'build/win32u-unix/driver_ios.c').read_text()
query = driver[driver.index('/* ml1920: same-task controller snapshots'):]
types = r'''
#include <stdint.h>
#include <assert.h>
#include <pthread.h>
#include <stdatomic.h>
#include <string.h>
typedef uint8_t BYTE;
typedef uint16_t WORD;
typedef int16_t SHORT;
typedef uint32_t DWORD;
typedef unsigned int UINT;
typedef uintptr_t ULONG_PTR;
#define C_ASSERT(x) _Static_assert(x, #x)
'''
tests = r'''
static atomic_int finished;
static struct winios_gamepad patterns[2];
static void *writer(void *arg) {
    uintptr_t which = (uintptr_t)arg;
    for (int i = 0; i < 100000; ++i)
        winios_gamepad_set_state(0, &patterns[which]);
    atomic_fetch_add(&finished, 1);
    return NULL;
}
int main(void) {
    struct winios_gamepad state = {0}, result;
    struct ios_xinput_state output;
    struct ios_xinput_caps caps;
    assert(sizeof(state) == 20);
    assert(!winios_gamepad_get_state(-1, &result) && !result.connected);
    assert(!winios_gamepad_get_state(4, &result));
    assert(!ios_gamepad_query(0, 0, &output));
    state.connected = 1; state.buttons = 0x1011;
    state.lx = -32768; state.ly = 32767;
    state.rx = -1234; state.ry = 5678;
    state.left_trigger = 255; state.right_trigger = 17;
    state.packet = 99;
    winios_gamepad_set_state(0, &state);
    assert(winios_gamepad_get_state(0, &result) && result.packet == 1);
    state.packet = 999;
    winios_gamepad_set_state(0, &state);
    assert(winios_gamepad_get_state(0, &result) && result.packet == 1);
    assert(ios_gamepad_query(0, 0, &output));
    assert(output.packet_number == 1 && output.gamepad.buttons == 0x1011);
    assert(output.gamepad.thumb_lx == -32768 && output.gamepad.thumb_ly == 32767);
    assert(output.gamepad.thumb_rx == -1234 && output.gamepad.thumb_ry == 5678);
    assert(output.gamepad.left_trigger == 255 && output.gamepad.right_trigger == 17);
    assert(ios_gamepad_query(0, 1, &caps));
    assert(caps.type == 1 && caps.sub_type == 1 && caps.flags == 0);
    assert(caps.left_motor_speed == 0 && caps.right_motor_speed == 0);
    assert(!ios_gamepad_query(0, 2, &caps) && !ios_gamepad_query(4, 0, &output));
    assert(!ios_gamepad_query(0, 0, NULL));
    for (int i = 1; i < 4; ++i) {
        state.buttons = (uint16_t)(1 << i);
        winios_gamepad_set_state(i, &state);
        assert(winios_gamepad_get_state(i, &result) && result.buttons == (1 << i));
    }
    winios_gamepad_set_state(0, NULL);
    memset(&result, 0xff, sizeof(result));
    assert(!winios_gamepad_get_state(0, &result));
    struct winios_gamepad zero = {0};
    assert(!memcmp(&result, &zero, sizeof(result)));
    winios_gamepad_set_state(0, &state);
    assert(winios_gamepad_get_state(0, &result) && result.packet == 3);
    patterns[0] = state; patterns[1] = state;
    patterns[0].buttons = 0x1000; patterns[0].lx = -100; patterns[0].right_trigger = 0;
    patterns[1].buttons = 0x8000; patterns[1].lx = 100; patterns[1].right_trigger = 255;
    winios_gamepad_set_state(0, &patterns[0]);
    pthread_t threads[2];
    for (uintptr_t i = 0; i < 2; ++i) assert(!pthread_create(&threads[i], NULL, writer, (void *)i));
    do {
        assert(winios_gamepad_get_state(0, &result));
        unsigned i = result.buttons == 0x1000 ? 0 : 1;
        result.packet = patterns[i].packet;
        assert(!memcmp(&result, &patterns[i], sizeof(result)));
    } while (atomic_load(&finished) != 2);
    for (int i = 0; i < 2; ++i) assert(!pthread_join(threads[i], NULL));
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix='madeira-gamepad-') as tmp:
    source = Path(tmp) / 'check.c'
    binary = Path(tmp) / 'check'
    source.write_text(types + query + tests)
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-O2', '-pthread', '-I', str(root / 'build/win32u-unix'), str(source),
                    str(root / 'app/Madeira/Winios/WiniosGamepad.c'), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('PASS: packet changes, ranges, four slots, disconnect/reconnect, query ABI, concurrent snapshots')
