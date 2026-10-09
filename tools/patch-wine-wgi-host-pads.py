#!/usr/bin/env python3
"""madeira-bcd: Madeira's host pads as Windows.Gaming.Input gamepads.

Horizon Zero Dawn (build 471, log 2026-10-09 15:01) reads its controller only
through Windows.Gaming.Input.Gamepad (RoGetActivationFactory for
Windows.Gaming.Input.Gamepad and .RawGameController; no xinput DLL is ever
loaded). Wine's windows.gaming.input adopts HID devices only, and a HID device
counts as a Gamepad only when its path carries "&XI_" (winexinput). On iOS a pad
reaches Windows games through the snapshot Madeira's XInput reads; in XInput mode
there is no HID device at all, in HID mode player 1 is a DualSense HID device
(a RawGameController, not a Gamepad). So the game saw no gamepad in either mode.

This patch adds a second kind of provider to dlls/windows.gaming.input/provider.c:
each connected XInput slot (XInputGetState from xinput1_4.dll, i.e. Madeira's
host path) becomes a provider of type Gamepad, an Xbox 360 pad to the game, as
Windows shows an XInput pad. The monitor thread (main.c) polls the four slots
twice a second for arrivals and removals, since iOS delivers no device-arrival
broadcast; the first poll runs before DllGetActivationFactory returns. Vibration
goes to the pad through XInputSetState (rumble = left motor, buzz = right motor),
as Madeira's xinput already passes it to the controller. The HID providers are
untouched.

Opt-in at run time: MADEIRA_WGI_HOST_PADS=1 in the game's environment
(env.MADEIRA_WGI_HOST_PADS = 1). Without it the DLL behaves exactly as before:
no xinput1_4.dll load, no provider, the monitor thread waits as it did.
Logs: "[wgi-host] ..." (err channel) once at start and per slot change.

Usage: patch-wine-wgi-host-pads.py wine/dlls/windows.gaming.input
(idempotent; tools/build-wine-extra-dlls.sh applies it around its
windows.gaming.input build and restores the files afterwards.)
"""
import os
import sys

MARK = "madeira_host_pads"

PROVIDER_CODE = r'''
/* madeira-bcd: Madeira's host pads as Windows.Gaming.Input gamepads, opt-in
 * with MADEIRA_WGI_HOST_PADS=1 (tools/patch-wine-wgi-host-pads.py). A pad
 * reaches Windows games on iOS through the snapshot Madeira's XInput reads;
 * there is no HID device for it in XInput mode, and this DLL adopts only HID
 * devices, so a game reading only Windows.Gaming.Input.Gamepad (Horizon Zero
 * Dawn) saw no controller. Each connected XInput slot becomes a Gamepad
 * provider; the monitor thread polls the slots twice a second. */
struct madeira_xstate          /* XINPUT_STATE */
{
    DWORD packet;
    WORD buttons;
    BYTE left_trigger;
    BYTE right_trigger;
    SHORT lx;
    SHORT ly;
    SHORT rx;
    SHORT ry;
};

struct madeira_xvibration      /* XINPUT_VIBRATION */
{
    WORD left_motor;
    WORD right_motor;
};

typedef DWORD (WINAPI *madeira_xget_fn)( DWORD index, struct madeira_xstate *state );
typedef DWORD (WINAPI *madeira_xset_fn)( DWORD index, struct madeira_xvibration *vibration );

static madeira_xget_fn madeira_xget;
static madeira_xset_fn madeira_xset;
static BOOL madeira_host_pads_on;

static BOOL WINAPI madeira_host_pads_init( INIT_ONCE *once, void *param, void **context )
{
    WCHAR value[8];
    DWORD len = GetEnvironmentVariableW( L"MADEIRA_WGI_HOST_PADS", value, ARRAY_SIZE(value) );
    HMODULE xinput;

    if (!len || len >= ARRAY_SIZE(value) || wcscmp( value, L"1" )) return TRUE;
    if (!(xinput = LoadLibraryW( L"xinput1_4.dll" )))
    {
        ERR( "[wgi-host] MADEIRA_WGI_HOST_PADS=1, but xinput1_4.dll did not load (error %lu)\n", GetLastError() );
        return TRUE;
    }
    madeira_xget = (madeira_xget_fn)GetProcAddress( xinput, "XInputGetState" );
    madeira_xset = (madeira_xset_fn)GetProcAddress( xinput, "XInputSetState" );
    madeira_host_pads_on = madeira_xget != NULL;
    ERR( "[wgi-host] XInput pads %s as Windows.Gaming.Input gamepads (MADEIRA_WGI_HOST_PADS=1)\n",
         madeira_host_pads_on ? "are published" : "are NOT published (no XInputGetState)" );
    return TRUE;
}

static BOOL madeira_host_pads_enabled( void )
{
    static INIT_ONCE once = INIT_ONCE_STATIC_INIT;
    InitOnceExecuteOnce( &once, madeira_host_pads_init, NULL, NULL );
    return madeira_host_pads_on;
}

/* A thumbstick value as the [0, 1] axis gamepad.c expects. */
static DOUBLE madeira_host_axis( SHORT value )
{
    DOUBLE v = value < 0 ? value / 32768. : value / 32767.;
    return (v + 1.) / 2.;
}

/* XINPUT_STATE to what gamepad.c reads: LeftThumbstickX = 2 * axes[1] - 1,
 * LeftThumbstickY = 2 * axes[0] - 1 (up positive, as in XInput),
 * RightThumbstickX = 2 * axes[3] - 1, RightThumbstickY = 2 * axes[2] - 1,
 * the triggers axes[4] and axes[5], buttons 0-9 A B X Y LB RB View Menu LS RS,
 * the d-pad in switches[0]. */
static void madeira_host_state( const struct madeira_xstate *in, struct WineGameControllerState *out )
{
    static const WORD bits[10] = { 0x1000, 0x2000, 0x4000, 0x8000, 0x0100, 0x0200, 0x0020, 0x0010, 0x0040, 0x0080 };
    static const GameControllerSwitchPosition hat[16] =   /* index: up 1, down 2, left 4, right 8 */
    {
        GameControllerSwitchPosition_Center, GameControllerSwitchPosition_Up,
        GameControllerSwitchPosition_Down, GameControllerSwitchPosition_Center,
        GameControllerSwitchPosition_Left, GameControllerSwitchPosition_UpLeft,
        GameControllerSwitchPosition_DownLeft, GameControllerSwitchPosition_Left,
        GameControllerSwitchPosition_Right, GameControllerSwitchPosition_UpRight,
        GameControllerSwitchPosition_DownRight, GameControllerSwitchPosition_Right,
        GameControllerSwitchPosition_Center, GameControllerSwitchPosition_Up,
        GameControllerSwitchPosition_Down, GameControllerSwitchPosition_Center,
    };
    UINT i;

    memset( out, 0, sizeof(*out) );
    out->axes[0] = madeira_host_axis( in->ly );
    out->axes[1] = madeira_host_axis( in->lx );
    out->axes[2] = madeira_host_axis( in->ry );
    out->axes[3] = madeira_host_axis( in->rx );
    out->axes[4] = in->left_trigger / 255.;
    out->axes[5] = in->right_trigger / 255.;
    for (i = 0; i < ARRAY_SIZE(bits); i++) out->buttons[i] = (in->buttons & bits[i]) != 0;
    out->switches[0] = hat[in->buttons & 0xf];
}

struct host_provider
{
    IWineGameControllerProvider IWineGameControllerProvider_iface;
    IGameControllerProvider IGameControllerProvider_iface;
    LONG ref;
    DWORD index;
    struct WineGameControllerVibration vibration;
};

static inline struct host_provider *host_impl_from_IWineGameControllerProvider( IWineGameControllerProvider *iface )
{
    return CONTAINING_RECORD( iface, struct host_provider, IWineGameControllerProvider_iface );
}

static HRESULT WINAPI host_provider_QueryInterface( IWineGameControllerProvider *iface, REFIID iid, void **out )
{
    struct host_provider *impl = host_impl_from_IWineGameControllerProvider( iface );

    TRACE( "iface %p, iid %s, out %p.\n", iface, debugstr_guid( iid ), out );

    if (IsEqualGUID( iid, &IID_IUnknown ) ||
        IsEqualGUID( iid, &IID_IInspectable ) ||
        IsEqualGUID( iid, &IID_IAgileObject ) ||
        IsEqualGUID( iid, &IID_IWineGameControllerProvider ))
    {
        IInspectable_AddRef( (*out = &impl->IWineGameControllerProvider_iface) );
        return S_OK;
    }

    if (IsEqualGUID( iid, &IID_IGameControllerProvider ))
    {
        IInspectable_AddRef( (*out = &impl->IGameControllerProvider_iface) );
        return S_OK;
    }

    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI host_provider_AddRef( IWineGameControllerProvider *iface )
{
    struct host_provider *impl = host_impl_from_IWineGameControllerProvider( iface );
    return InterlockedIncrement( &impl->ref );
}

static ULONG WINAPI host_provider_Release( IWineGameControllerProvider *iface )
{
    struct host_provider *impl = host_impl_from_IWineGameControllerProvider( iface );
    ULONG ref = InterlockedDecrement( &impl->ref );
    if (!ref) free( impl );
    return ref;
}

static HRESULT WINAPI host_provider_GetIids( IWineGameControllerProvider *iface, ULONG *iid_count, IID **iids )
{
    return E_NOTIMPL;
}

static HRESULT WINAPI host_provider_GetRuntimeClassName( IWineGameControllerProvider *iface, HSTRING *class_name )
{
    return E_NOTIMPL;
}

static HRESULT WINAPI host_provider_GetTrustLevel( IWineGameControllerProvider *iface, TrustLevel *trust_level )
{
    return E_NOTIMPL;
}

static HRESULT WINAPI host_provider_get_NonRoamableId( IWineGameControllerProvider *iface, HSTRING *value )
{
    return E_NOTIMPL;
}

static HRESULT WINAPI host_provider_get_DisplayName( IWineGameControllerProvider *iface, HSTRING *value )
{
    static const WCHAR name[] = L"Controller (Xbox 360 For Windows)";
    return WindowsCreateString( name, wcslen( name ), value );
}

static HRESULT WINAPI host_provider_get_Type( IWineGameControllerProvider *iface, WineGameControllerType *value )
{
    *value = WineGameControllerType_Gamepad;
    return S_OK;
}

static HRESULT WINAPI host_provider_get_AxisCount( IWineGameControllerProvider *iface, INT32 *value )
{
    *value = 6;
    return S_OK;
}

static HRESULT WINAPI host_provider_get_ButtonCount( IWineGameControllerProvider *iface, INT32 *value )
{
    *value = 10;
    return S_OK;
}

static HRESULT WINAPI host_provider_get_SwitchCount( IWineGameControllerProvider *iface, INT32 *value )
{
    *value = 1;
    return S_OK;
}

static HRESULT WINAPI host_provider_get_State( IWineGameControllerProvider *iface, struct WineGameControllerState *out )
{
    struct host_provider *impl = host_impl_from_IWineGameControllerProvider( iface );
    struct madeira_xstate state = {0};

    if (!madeira_xget || madeira_xget( impl->index, &state )) memset( &state, 0, sizeof(state) );
    madeira_host_state( &state, out );
    out->timestamp = GetTickCount64();
    return S_OK;
}

static HRESULT WINAPI host_provider_get_Vibration( IWineGameControllerProvider *iface, struct WineGameControllerVibration *out )
{
    struct host_provider *impl = host_impl_from_IWineGameControllerProvider( iface );
    *out = impl->vibration;
    return S_OK;
}

static HRESULT WINAPI host_provider_put_Vibration( IWineGameControllerProvider *iface, struct WineGameControllerVibration value )
{
    struct host_provider *impl = host_impl_from_IWineGameControllerProvider( iface );
    struct madeira_xvibration motors = { value.rumble, value.buzz };

    if (!memcmp( &impl->vibration, &value, sizeof(value) )) return S_OK;
    impl->vibration = value;
    if (madeira_xset) madeira_xset( impl->index, &motors );
    return S_OK;
}

static HRESULT WINAPI host_provider_get_ForceFeedbackMotor( IWineGameControllerProvider *iface, IForceFeedbackMotor **value )
{
    *value = NULL;
    return S_OK;
}

static const struct IWineGameControllerProviderVtbl host_wine_provider_vtbl =
{
    host_provider_QueryInterface,
    host_provider_AddRef,
    host_provider_Release,
    /* IInspectable methods */
    host_provider_GetIids,
    host_provider_GetRuntimeClassName,
    host_provider_GetTrustLevel,
    /* IWineGameControllerProvider methods */
    host_provider_get_NonRoamableId,
    host_provider_get_DisplayName,
    host_provider_get_Type,
    host_provider_get_AxisCount,
    host_provider_get_ButtonCount,
    host_provider_get_SwitchCount,
    host_provider_get_State,
    host_provider_get_Vibration,
    host_provider_put_Vibration,
    host_provider_get_ForceFeedbackMotor,
};

DEFINE_IINSPECTABLE_( host_game_provider, IGameControllerProvider, struct host_provider,
                      host_impl_from_IGameControllerProvider, IGameControllerProvider_iface,
                      &impl->IWineGameControllerProvider_iface )

static HRESULT WINAPI host_game_provider_get_FirmwareVersionInfo( IGameControllerProvider *iface, GameControllerVersionInfo *value )
{
    return E_NOTIMPL;
}

static HRESULT WINAPI host_game_provider_get_HardwareProductId( IGameControllerProvider *iface, UINT16 *value )
{
    *value = 0x028e;   /* Xbox 360 Controller */
    return S_OK;
}

static HRESULT WINAPI host_game_provider_get_HardwareVendorId( IGameControllerProvider *iface, UINT16 *value )
{
    *value = 0x045e;   /* Microsoft */
    return S_OK;
}

static HRESULT WINAPI host_game_provider_get_HardwareVersionInfo( IGameControllerProvider *iface, GameControllerVersionInfo *value )
{
    return E_NOTIMPL;
}

static HRESULT WINAPI host_game_provider_get_IsConnected( IGameControllerProvider *iface, boolean *value )
{
    *value = TRUE;
    return S_OK;
}

static const struct IGameControllerProviderVtbl host_game_provider_vtbl =
{
    host_game_provider_QueryInterface,
    host_game_provider_AddRef,
    host_game_provider_Release,
    /* IInspectable methods */
    host_game_provider_GetIids,
    host_game_provider_GetRuntimeClassName,
    host_game_provider_GetTrustLevel,
    /* IGameControllerProvider methods */
    host_game_provider_get_FirmwareVersionInfo,
    host_game_provider_get_HardwareProductId,
    host_game_provider_get_HardwareVendorId,
    host_game_provider_get_HardwareVersionInfo,
    host_game_provider_get_IsConnected,
};

static struct host_provider *madeira_host_providers[4];

/* On the monitor thread: one provider per connected XInput slot. */
void madeira_host_pads_poll( void )
{
    DWORD i;

    if (!madeira_host_pads_enabled()) return;
    for (i = 0; i < ARRAY_SIZE(madeira_host_providers); i++)
    {
        struct madeira_xstate state;
        BOOL connected = !madeira_xget( i, &state );
        struct host_provider *impl;

        EnterCriticalSection( &provider_cs );
        impl = madeira_host_providers[i];
        if (connected == (impl != NULL))
        {
            LeaveCriticalSection( &provider_cs );
            continue;
        }
        if (connected)
        {
            if (!(impl = calloc( 1, sizeof(*impl) )))
            {
                LeaveCriticalSection( &provider_cs );
                continue;
            }
            impl->IWineGameControllerProvider_iface.lpVtbl = &host_wine_provider_vtbl;
            impl->IGameControllerProvider_iface.lpVtbl = &host_game_provider_vtbl;
            impl->ref = 1;
            impl->index = i;
        }
        madeira_host_providers[i] = connected ? impl : NULL;
        LeaveCriticalSection( &provider_cs );

        if (connected)
        {
            ERR( "[wgi-host] XInput slot %lu connected: a Windows.Gaming.Input gamepad\n", i );
            manager_on_provider_created( &impl->IGameControllerProvider_iface );
        }
        else
        {
            ERR( "[wgi-host] XInput slot %lu disconnected: its gamepad is removed\n", i );
            manager_on_provider_removed( &impl->IGameControllerProvider_iface );
            IGameControllerProvider_Release( &impl->IGameControllerProvider_iface );
        }
    }
}

/* The monitor thread's wait: as before without host pads; with them, a poll at
 * least every 500 ms. FALSE ends the thread, as a failed wait did before. */
BOOL madeira_host_pads_wait( void )
{
    static DWORD last_poll;
    BOOL on = madeira_host_pads_enabled();
    DWORD ret = MsgWaitForMultipleObjectsEx( 0, NULL, on ? 500 : INFINITE, QS_ALLINPUT, MWMO_ALERTABLE );

    if (on && (ret == WAIT_TIMEOUT || GetTickCount() - last_poll >= 500))
    {
        last_poll = GetTickCount();
        madeira_host_pads_poll();
    }
    return ret == WAIT_OBJECT_0 || (on && ret == WAIT_TIMEOUT);
}
'''

MAIN_DECL = '''
/* madeira-bcd: tools/patch-wine-wgi-host-pads.py (provider.c) */
extern void madeira_host_pads_poll( void );
extern BOOL madeira_host_pads_wait( void );
'''


def patch(directory):
    provider_path = os.path.join(directory, "provider.c")
    main_path = os.path.join(directory, "main.c")
    provider = open(provider_path).read()
    main = open(main_path).read()
    if MARK in provider and MARK in main:
        print("already patched")
        return
    if MARK in provider or MARK in main:
        sys.exit("patch-wine-wgi-host-pads: only one of provider.c and main.c is patched")

    # provider.c: the host providers at the end, after the HID providers.
    if "void provider_remove( const WCHAR *device_path )" not in provider:
        sys.exit("patch-wine-wgi-host-pads: provider_remove not found in provider.c")
    for needed in ("static CRITICAL_SECTION provider_cs = ", "struct WineGameControllerState",
                   "DEFINE_IINSPECTABLE( game_provider, IGameControllerProvider"):
        if needed not in provider:
            sys.exit("patch-wine-wgi-host-pads: %r not found in provider.c" % needed)
    provider = provider.rstrip("\n") + "\n" + PROVIDER_CODE

    # main.c: the declarations, the first poll with the HID providers, the wait.
    anchor = "static LRESULT CALLBACK devnotify_wndproc("
    if main.count(anchor) != 1:
        sys.exit("patch-wine-wgi-host-pads: devnotify_wndproc not found in main.c")
    main = main.replace(anchor, MAIN_DECL.lstrip("\n") + "\n" + anchor)
    first = "    SetupDiDestroyDeviceInfoList( set );\n}\n"
    if main.count(first) != 1:
        sys.exit("patch-wine-wgi-host-pads: the end of initialize_providers not found in main.c")
    main = main.replace(first, "    SetupDiDestroyDeviceInfoList( set );\n    madeira_host_pads_poll();\n}\n")
    wait = "    } while (!MsgWaitForMultipleObjectsEx( 0, NULL, INFINITE, QS_ALLINPUT, MWMO_ALERTABLE ));\n"
    if main.count(wait) != 1:
        sys.exit("patch-wine-wgi-host-pads: the monitor thread's wait not found in main.c")
    main = main.replace(wait, "    } while (madeira_host_pads_wait());\n")

    open(provider_path, "w").write(provider)
    open(main_path, "w").write(main)
    print("patched windows.gaming.input: host pads as gamepads (MADEIRA_WGI_HOST_PADS=1)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: patch-wine-wgi-host-pads.py wine/dlls/windows.gaming.input")
    patch(sys.argv[1])
