/* GPL-3.0-or-later WITH the Madeira Converter Exception, version 1. */
#include "WiniosGamepad.h"
#include <pthread.h>
#include <string.h>

/* ml1920: protect the payload as well as the version. A sequence counter
 * around a non-atomic struct copy still constitutes a C data race. The lock
 * covers only a 20-byte snapshot, never framework work or a Wine server call. */
static pthread_mutex_t pad_lock = PTHREAD_MUTEX_INITIALIZER;
static struct winios_gamepad pads[WINIOS_GAMEPAD_MAX];

void winios_gamepad_set_state(int index, const struct winios_gamepad *state)
{
    struct winios_gamepad next = {0};
    if (index < 0 || index >= WINIOS_GAMEPAD_MAX) return;
    if (state && state->connected) {
        next = *state;
        next.connected = 1;
        memset(next.reserved, 0, sizeof(next.reserved));
    }
    pthread_mutex_lock(&pad_lock);
    next.packet = pads[index].packet;
    if (memcmp(&next, &pads[index], sizeof(next))) {
        next.packet++;
        pads[index] = next;
    }
    pthread_mutex_unlock(&pad_lock);
}

int winios_gamepad_get_state(int index, struct winios_gamepad *out)
{
    struct winios_gamepad value = {0};
    if (index >= 0 && index < WINIOS_GAMEPAD_MAX) {
        pthread_mutex_lock(&pad_lock);
        value = pads[index];
        pthread_mutex_unlock(&pad_lock);
    }
    if (out) {
        if (value.connected) *out = value;
        else memset(out, 0, sizeof(*out));
    }
    return value.connected != 0;
}
