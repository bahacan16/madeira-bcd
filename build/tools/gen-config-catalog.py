#!/usr/bin/env python3
"""Generate app/Madeira/ConfigCatalog.generated.swift: every option Madeira reads.

Two kinds of option live in Documents/madeira.cfg:
  - plain keys ("vram-mb = 4352"), read through build/madeira_cfg.h
    (madeira_cfg_get/int/bool), the D3D12 runtime (mad_cfg_int_pe/str_pe) or
    the app (MadeiraConfig.get/bool/flag);
  - environment switches ("env.NAME = value"), which the app exports before
    Wine starts; any MADEIRA_*/DXMT_*/MYTHIC_* name the code reads with getenv,
    GetEnvironmentVariable, env::getEnvVar, madeiraSwitch or a flag helper.

The scan covers the tracked sources of this repository and of the wine, DXMT
and FEX submodules. Each option gets its type and default from the reading
call, a subsystem from the file it is read in, and the comment next to the
first read. OVERLAY adds titles and fixed choices for the options that have a
dedicated place in Settings.

    build/tools/gen-config-catalog.py           rewrite the Swift catalog
    build/tools/gen-config-catalog.py --check   exit 1 if it is out of date
"""
import collections, json, os, re, subprocess, sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
OUT = os.path.join(ROOT, "app", "Madeira", "ConfigCatalog.generated.swift")
REPOS = {
    ".": ["build", "app", "madeira-d3d12/src", "madeira-dock/src"],
    "dxmt": ["src"],
    "wine": ["dlls", "server"],
    "FEX": ["Source", "FEXCore/Source"],
}
EXT = (".c", ".h", ".m", ".mm", ".cpp", ".hpp", ".cc", ".swift")
SKIP = ("/host-tests/", "/ffmpeg/", "ConfigCatalog.generated.swift")

CFG = re.compile(r'\b(madeira_cfg_(?:get|int|bool)|mad_cfg_(?:int|str)_pe|'
                 r'MadeiraConfig\.(?:get|bool|flag))\(\s*"([A-Za-z0-9._-]+)"\s*(?:,\s*([^,)]+))?')
ENV = re.compile(r'\b(getenv|GetEnvironmentVariable[AW]?|env::getEnvVar|madeiraSwitch|'
                 r'madeira_switch_for_caller|mad_env_is_zero|flag|envFlag)\(\s*L?"((?:MADEIRA|DXMT|MYTHIC)_[A-Z0-9_]+)"'
                 r'\s*(?:,\s*(?:fallback:|default:)?\s*([^,)]+))?')
BOOL_READERS = {"madeira_cfg_bool", "MadeiraConfig.bool", "MadeiraConfig.flag", "flag", "envFlag",
                "madeiraSwitch", "madeira_switch_for_caller", "mad_env_is_zero"}
INT_READERS = {"madeira_cfg_int", "mad_cfg_int_pe"}

# Titles, kinds and fixed choices for options with a dedicated Settings row.
# "choices" are (value, label); the empty value means "remove the key".
OVERLAY = {
    "env.MADEIRA_PIN_GRAPHICS_DLLS": {"category": "Memory & JIT pool", "title": "Keep graphics DLLs loaded",
                "kind": "bool", "default": "0",
                "note": "Default off. On a D3D12 device/probe call, pins already loaded D3D12, DXGI and Wine Metal "
                        "DLLs until normal process shutdown, preventing repeated unload/reload copies. Keeps DLL "
                        "data live; does not recycle executable ranges or change GPU capabilities. Restart the "
                        "session after changing it.",
                "sources": ["madeira-d3d12/src/pe/madeira_d3d12.c"]},
    "env.MADEIRA_POOL_RECYCLE_IMAGES": {"category": "Memory & JIT pool", "title": "Images in retired code-buffer space",
                "kind": "bool", "default": "0",
                "note": "Default off. If other image allocations fail, retired region-C code buffers up to 64 MB "
                        "may enter the image freelist after a 3-second grace, executable-page and thread-PC checks. "
                        "Requires pool-low; 64-bit processes only. Live buffers and the pool-low-margin stay unchanged."},
    "env.MADEIRA_X64_IMAGE_NOCOPY": {"category": "Memory & JIT pool", "title": "Pure-x64 images without a JIT-pool copy",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: an x64-only image (not ARM64EC hybrid, not a Wine builtin) in a 64-bit "
                        "process is not copied into the JIT pool; the emulator runs its code from the loaded "
                        "image, as 32-bit programs already do, and its executable protections are applied without "
                        "EXEC. Frees the pool for hybrid DLL copies and code buffers. Hybrid images keep their "
                        "copy. Set it in the game's own file; restart the session after changing it."},
    "env.MADEIRA_FIXED_BASE_GUARD": {"category": "Memory & JIT pool", "title": "Keep 0x140000000 for a fixed-base game",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: a resource-only image view (a version or resource query, e.g. the Rockstar "
                        "Games Launcher reading Social-Club-Setup.exe) never takes the reserved executable window at "
                        "0x140000000; it is mapped elsewhere. The window then stays free for a game exe without "
                        "relocations that can only run there (Red Dead Redemption 2). It moves other programs' "
                        "images (licensed GTA V Enhanced), so set it only in such a game's own file; read at "
                        "session start."},
    "env.MADEIRA_RDR2_VA_HOLD": {"category": "Memory & JIT pool", "title": "Keep 8960 MB free for Red Dead Redemption 2",
                "kind": "bool", "default": "0",
                "note": "Default off; only with env.MADEIRA_SC_PA_POOLS = 2. 1: Social Club's layout 2 keeps libcef.dll's "
                        "PartitionAlloc pools at 256 MB and its metadata at 1 GB, chrome_elf.dll's metadata at 6.25 GB, "
                        "gives the rest of the app's 4 GB hold to Wine's furniture (3.75 GB more room for threads and "
                        "allocations), keeps Oilpan's cage at 1 GB so the FEX arena grows to 15 GB, and holds "
                        "[0x75d0000000, 0x7800000000) from boot for one 8960 MB reserve, the one "
                        "Red Dead Redemption 2 makes as it starts. Set it only in that game's own file; GTA V keeps "
                        "layout 2 as it is. Read at session start."},
    "env.MADEIRA_SWAP_RESERVE_MAX_MB": {"category": "Memory & JIT pool", "title": "Swap tier: largest reservation backed (MB)",
                "kind": "int", "default": "", "sources": ["build/ntdll-unix/virtual_ios.c"],
                "note": "The largest new reservation the swap tier backs whole: 256 MB in wide (at most 4 GB), 8 GB in "
                        "broad (at most 16 GB). 9216 with broad coverage (swap-mode = 2) also backs Red Dead "
                        "Redemption 2's 8960 MB heap with the file. Read at session start."},
    "env.MADEIRA_THREAD_STACK_SPILL": {"category": "Memory & JIT pool", "title": "New threads may use the kernel's pick when memory space is full",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: a new thread's 1 MB kernel stack (and its emulator stack) is looked for from "
                        "the start of Wine's furniture band instead of from 4 GB (each new thread no longer tries "
                        "about 300 refused addresses first), and when that band is full the kernel places it "
                        "anywhere still free instead of the thread failing. Red Dead Redemption 2 froze four minutes "
                        "into the story because every new thread failed this way while 1.8 GB was free elsewhere. "
                        "Read at session start."},
    "env.MADEIRA_SMALL_STACK_EXES": {"category": "Memory & JIT pool", "title": "Programs whose threads keep a 2 MB stack",
                "kind": "text", "default": "",
                "note": "Empty by default: every thread stack is at least 8 MB (Chromium needs that). Exe names, "
                        "separated by commas (e.g. RDR2.exe): threads of these programs get the stack they ask for, "
                        "at least 2 MB, so their stacks take less of the shared memory space. Never list Chromium "
                        "programs (SocialClubHelper.exe, Launcher.exe, steamwebhelper.exe). Read when a thread starts."},
    "replay-split": {"category": "Direct3D 12", "title": "Log where Direct3D 12 submission time goes",
                "kind": "bool", "default": "0",
                "note": "Default off, logging only. 1: every 5 seconds a [perf] replay split line divides the time "
                        "ExecuteCommandLists takes per frame into waiting for the queue, preparation, opening a "
                        "command buffer, each kind of command (draws, dispatches, copies, clears, barriers, render "
                        "targets, queries, state) and the end of each list. For finding why a game's frame is slow "
                        "when the GPU is not busy."},
    "env.MADEIRA_MFC": {"category": "Wine libraries", "title": "Microsoft MFC (mfc140.dll, mfc140u.dll)",
                "kind": "bool", "default": "1",
                "note": "On by default. MFC comes from the same Microsoft VC++ package as the C++ runtime and is "
                        "linked into the game's system32; games that import it (Horizon Zero Dawn) stop at once "
                        "without it. Wine has no MFC of its own. 0 leaves it out. Read at session start."},
    "fullscreen-window": {"category": "Direct3D 12", "title": "Full screen moves the game's window over the whole screen",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: when a Direct3D 12 game switches to full screen, its window is moved to the "
                        "screen's top-left corner and over the whole screen first, as Windows does; the game then "
                        "sizes it to the chosen mode. Without it a game that only resizes its window (Red Dead "
                        "Redemption 2) keeps it where it was, beside the Wine desktop. Read once per session."},
    "indirect-fast": {"category": "Direct3D 12", "title": "Encode ExecuteIndirect records without per-record setup",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: after the first record of an ExecuteIndirect, the other records (which share "
                        "all state but their offset) are encoded 64 at a time with only their offset and the "
                        "indirect draw or dispatch, instead of the whole per-draw setup each. For games that issue "
                        "thousands of indirect records a frame (Red Dead Redemption 2), where that setup costs more "
                        "CPU time than the GPU spends. DXIL and DXBC pipelines (DXBC from build 470); pipelines "
                        "with tessellation or geometry stages, diagnostics and captures keep the per-record path. "
                        "Read once per session."},
    "dxbc-register-spaces": {"category": "Direct3D 12", "title": "Shader model 5.1 resources in register spaces other than 0",
                "kind": "bool", "default": "0",
                "note": "Default off: a DXBC shader (shader model 5.1) that declares a resource in a register space "
                        "other than 0 is refused, and its pipeline is not created. 1: such shaders are converted, and "
                        "each resource is bound from the root-signature entry with the same space and register. "
                        "Horizon Zero Dawn needs it (its constant buffers sit in spaces 6 and 8). Shaders converted "
                        "with it on are cached apart from the others. Read once per session.",
                "sources": ["madeira-d3d12/src/unix/madeira_ir_unix.mm", "madeira-d3d12/src/pe/madeira_d3d12.c"]},
    "env.MADEIRA_GAME_DIALOG_CURSOR": {"category": "Windows, display & input", "title": "Cursor over a game's launcher or message box",
                "kind": "bool", "default": "1",
                "note": "Default on. A game session draws no cursor: the finger is the pointer. While the game shows a "
                        "launcher or message box, the arrow is drawn where the finger last touched (where the click "
                        "lands) and goes away with the box. Tap the button itself to press it. 0: no cursor, as "
                        "before. Desktop (Dock) sessions keep their own cursor."},
    # madeira-bcd: Horizon Zero Dawn on build 471 got no input at all.
    "env.MADEIRA_INPUT_FOREGROUND": {"category": "Windows, display & input",
                "title": "Bring the game to the front when no window is",
                "kind": "bool", "default": "0",
                "note": "Default off. iOS has no window manager: a game that shows its window without activating "
                        "it and reads no XInput stays in the background, and Windows sends raw keyboard and mouse "
                        "input only to the program in front (Horizon Zero Dawn ignored every key and tap). 1: while "
                        "no window at all is in front, the game's main window is brought to the front when it polls "
                        "its messages or a key or tap arrives. Never while another window is in front."},
    "env.MADEIRA_WGI_HOST_PADS": {"category": "Controllers", "title": "Controllers in Windows.Gaming.Input",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: each connected XInput controller (a paired pad or the touch controller) "
                        "is also a Windows.Gaming.Input gamepad, an Xbox pad to the game, with rumble. For games "
                        "that read their controller only that way (Horizon Zero Dawn); a game that also reads "
                        "XInput could list it twice. Read when the game first asks for a gamepad.",
                "sources": ["tools/patch-wine-wgi-host-pads.py"]},
    # madeira-bcd: Red Dead Redemption 2 on build 470, 1.5 GB of argument-ring chunks.
    "ind-probe": {"category": "Direct3D 12", "title": "ExecuteIndirect probe (diagnostic)",
                "kind": "bool", "default": "1",
                "note": "Default on. Every 3 s per pipeline, the first argument record of an ExecuteIndirect is read "
                        "back by a small compute pass and logged as a [probe] line, 1500 lines at most. Each probe "
                        "ends the open render pass. 0: no probes (Red Dead Redemption 2's list, build 476)."},
    # madeira-bcd: Horizon Zero Dawn's refused shaders (build 475, 2026-10-09 16:20).
    "msc-unbounded-retry": {"category": "Direct3D 12", "title": "Retry refused shaders with sized unbounded ranges",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: a DXIL shader the converter refuses with code 4 (a resource not in the root "
                        "signature) is converted once more with the root signature's unbounded descriptor ranges "
                        "given a size (2048 samplers, 1000000 others). Horizon Zero Dawn's ranges start at register 4 "
                        "and ~4,900 of its shaders were refused. Once that works for a root signature, its later "
                        "shaders take the sized ranges first."},
    "pso-placeholder": {"category": "Direct3D 12", "title": "Placeholder for pipelines whose shaders did not convert",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: a graphics pipeline whose vertex or pixel shader did not convert is returned "
                        "to the game as a placeholder whose draws are skipped, instead of a failure. Horizon Zero "
                        "Dawn stopped with an \"Error\" box when its settings menu got such a failure."},
    "msc-fail-memo": {"category": "Direct3D 12", "title": "Remember refused shaders for the session",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: a shader the converter refused is not converted again for the next pipeline "
                        "that uses it (same bytecode, root signature and options) until the game exits. Horizon Zero "
                        "Dawn converted each refused shader ~6 times, ~30,000 conversions in its first minutes."},
    "ring-share": {"category": "Direct3D 12", "title": "Share argument buffers between command lists",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: a command list gives the argument-buffer chunks its replay wrote back as "
                        "soon as the GPU is done with them, instead of keeping them until it is recorded again; "
                        "all lists then share one pool. Red Dead Redemption 2 kept 1.5 GB of them (half of its "
                        "Metal memory) with the switch off."},
    # madeira-bcd: tools/patch-winemetal-null-fragment.py (winemetal reads it).
    "gs-null-fragment": {"category": "Direct3D 12", "title": "Empty fragment function for pipelines without a pixel shader",
                "kind": "bool", "default": "1", "sources": ["tools/patch-winemetal-null-fragment.py"],
                "note": "Default on. Metal aborts on a mesh pipeline (geometry shader or tessellation emulation) "
                        "with rasterization on and no fragment function, while D3D allows a pipeline with no pixel "
                        "shader. On: such a pipeline gets an empty fragment function and writes no render target, "
                        "as in D3D; depth and stencil work as before. Horizon Zero Dawn's pipeline thread died "
                        "without it and the game stopped after its first frame. 0: such a pipeline is refused "
                        "instead (the geometry shader is dropped). Pipelines with a pixel shader are not touched. "
                        "Read once per session."},
    "env.MADEIRA_BAND_CENSUS": {"category": "Debugging / logs", "title": "Log the 64 GB address band by piece",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: about once a minute and when a large reserve fails, [band] lines say what "
                        "each piece of [0x7000000000, 0x8000000000) uses (Social Club's pools and metadata, the "
                        "furniture, the V8 and Oilpan cages, the FEX arena), the largest free gap, and whether Red "
                        "Dead Redemption 2's 8960 MB reserve fits; 64 censuses at most. Logging only, changes no "
                        "placement; read at session start."},
    "env.MADEIRA_EC_HOOK_TRACE": {"category": "Debugging / logs", "title": "Log code patches the emulated copy misses",
                "kind": "bool", "default": "1",
                "note": "Default on, logging only. Compare executable PE sections and their pool copies before "
                        "sync, including x64 entry thunks. Separate budgets preserve later graphics-jump records "
                        "after native startup rewrites. A difference alone does not prove an inline hook. 0 disables it."},
    "env.MADEIRA_X64_GRAPHICS_ENTRY": {"category": "Direct3D 12 (Madeira)", "title": "Patchable x64 graphics method entries",
                "kind": "bool", "default": "0",
                "note": "Default off. 1 exposes typed x64 ARM64EC entries for swapchain Present/Present1, "
                        "ResizeBuffers/ResizeBuffers1 and queue ExecuteCommandLists/GetTimestampFrequency/GetClockCalibration/GetDesc/Signal/Wait. "
                        "With MADEIRA_DXGI_SRC=1, "
                        "also selects the factory's patchable MakeWindowAssociation and swapchain-creation entries. "
                        "The x64 entries use the loader's PE addresses so code patches and nearby allocations agree. "
                        "Existing native implementations remain behind the entries. Restart the game session."},
    "env.MADEIRA_POOL_LOW_IMAGES": {"category": "Memory & JIT pool", "title": "Small images in spare code-buffer space",
                "kind": "bool", "default": "0",
                "note": "Default off. If the normal JIT image allocation fails, copies up to 16 MB may use "
                        "unallocated region C, preserving 4 MB for code buffers. Requires pool-low; 64-bit "
                        "processes only. Does not shrink live buffers or the pool-low-margin."},
    "env.MADEIRA_IMAGE_PATCH_TRACE": {"category": "Debugging / logs", "title": "DLL code patch trace", "kind": "bool", "default": "0",
                "note": "1 logs up to 64 successful 1–16 byte executable-image protection requests and their PE/pool bytes. Read-only, owner-aware diagnostics; does not alter hooks or code."},
    "env.MADEIRA_DLL_LOCAL": {"category": "Wine core (ntdll)", "title": "App-local DLL reads", "kind": "text", "default": "",
                "note": "Optional semicolon-separated DLL filenames, including .dll. A source.dll=proxy.dll entry "
                        "redirects requests outside the executable's directory to that local proxy, preserving initial "
                        "loads from the executable's own directory. Bare filenames prefer the same local DLL. "
                        "Only read-only opens and attribute queries are redirected. Missing local files, "
                        "writes, create/delete operations, relative paths and non-file devices keep the normal path. "
                        "Off by default; set only in the game's own file."},
    "swap-mb": { "note": "Moves game data to a file on this device's storage when memory runs short, up to this size. Off by default; read at launch.", "category": "Memory & JIT pool","title": "Swap tier size", "kind": "choice",
                "choices": [("", "Off"), ("1024", "1 GB"), ("2048", "2 GB"), ("3072", "3 GB"), ("4096", "4 GB")]},
    "env.MADEIRA_SWAP_COVERAGE": {"category": "Memory & JIT pool", "note": "Which allocations the swap tier backs with its file (only when the tier is on). Large allocations (classic, the default): single 8 MB+ commits in the guest band. All allocations of 1 MB+ (blocks). 1 MB+ and overflow (wide): blocks plus allocations outside the band and fresh reservations. Whole reservations 4 MB+ (broad, ml1257): every new reservation of at least swap-min-mb (4 MB) below FEX's band backed whole when made, holes punched on decommit, swap-mb caps the disk it uses (a soft cap, checked when a block is backed). Unset: broad if swap-mode = 2, else classic.", "title": "Swap tier coverage", "kind": "choice",
                "choices": [("", "Large allocations (8 MB+)"), ("blocks", "All allocations of 1 MB+"), ("wide", "1 MB+ and overflow"), ("broad", "Whole reservations 4 MB+ (broad)")]},
    "swap-mode": {"category": "Memory & JIT pool", "title": "Swap tier mode (2 = broad)",
                "note": "2 selects broad swap coverage (ml1257) when env.MADEIRA_SWAP_COVERAGE is unset; any other value, classic. The coverage key wins when set."},
    "swap-min-mb": {"category": "Memory & JIT pool", "title": "Swap tier floor (MB)",
                "note": "The smallest allocation the swap tier backs (ml1257): 8 MB in classic, 1 MB in blocks and wide, 4 MB in broad unless set. MADEIRA_SWAP_MIN_KB overrides it for blocks, wide and broad."},
    "inproc-sync": { "category": "Synchronisation","title": "Madsync (in-process sync)", "default": "0",
                "note": "1 selects madsync (Settings > Sync engine > Madsync). Unset: fastsync, the default engine; 0 without env.MADEIRA_FASTSYNC: Wine standard sync."},
    "env.MADEIRA_FASTSYNC": {"category": "Synchronisation", "title": "Fastsync (in-process sync, default)", "kind": "choice",
                "note": "Fastsync is the default engine: with neither this nor inproc-sync set, the app exports auto. Settings > Sync engine > Fastsync removes both keys. Never runs while madsync is on. auto arms the fast wake path on heavy event traffic, 1 from the start, cells only answers polls, 0 is off.",
                "choices": [("", "Default (auto)"), ("auto", "Auto"), ("1", "On"), ("cells", "Poll answers only"), ("0", "Off")]},
    # Read by winemetal (the DXGI budget) and, for the opt-in D3DKMT adapter, by
    # build/win32u-unix/d3dkmt_ios.c; keep the DXMT category and note.
    "vram-mb": {"title": "Video memory budget (MB)", "category": "Direct3D 9/10/11 (DXMT)",
                "note": "ml1095: madeira.cfg vram-mb = N"},
    "pool": { "category": "Memory & JIT pool","title": "JIT pool size (MB)"},
    "totalphys": { "category": "Memory & JIT pool","title": "Reported physical memory (MB)"},
    "eco": { "note": 'Runs guest threads at a lower iOS QoS class so the system favours efficiency and saves power; can cost speed. Class chosen by eco-qos.',"title": "Eco scheduling"},
    "eco-qos": {"title": "Eco QoS class", "kind": "choice", "note": "The QoS class guest threads run at while eco is on.",
                "choices": [("", "Utility (default)"), ("background", "Background"), ("initiated", "User initiated")]},
    "env.MADEIRA_WG_VIDEO": {"title": "Media: MP4 video (32-bit programs)",
                "note": "0 limits the media parser to MP3/WAV; by default MP4 with H.264/HEVC video decodes through VideoToolbox."},
    "env.MADEIRA_WOW_RWX_PLAIN": {"category": "Memory & JIT pool", "note": "Unset: a 32-bit window's anonymous RWX memory is plain read/write to the host once Wine Mono's libmono-2.0-x86.dll is mapped there (ml1279/ml1282). 1: in every 32-bit window. 0: never (stores go through the JIT pool's alias, and FEX's Mono bridge stays off)."},
    "env.MADEIRA_WINEMONO_BRIDGE": {"note": "FEX's Mono backpatcher bridge (ml712). On by itself for Wine Mono, 32- and 64-bit (ml1282/ml1286); for the 32-bit runtime in a guest window it also turns SMC detection off once the backpatcher is found (ml1280). 0 keeps it off."},
    "env.MADEIRA_MONO_DEFAULTS": {"category": "Wine libraries", "note": "Wine Mono, 32- and 64-bit: mscoree sets MONO_THREADS_SUSPEND=coop and adds keep-delegates to MONO_DEBUG before Mono loads, and puts the previous values back once Mono has read them (ml1282); values already set win. 0 sets nothing."},
    "cpu-count": {"title": "Reported CPU count (0 = device)"},
    "desktop-size": {"title": "Virtual desktop size (WxH)",
                     "note": "The developer interface's Wine desktop size (ml1127); its Resolution menu writes it (ml1157). Madeira Dock sessions without a game's Resolution use it too. Unset: this screen's shape at 1280x720's pixel count (ml1172: 1408x648 on a 19.5:9 iPhone, 1152x800 on an 11-inch iPad). Applies at the next app start."},
    "d3d9": {"title": "Direct3D 9 frontend (32-bit)", "kind": "choice",
             "choices": [("", "Default (emulated)"), ("native", "Native ARM64 frontend")]},
    "fence-chain": {"title": "D3D12 fence chain mode"},
    "async-submit": {"title": "D3D12 asynchronous submission"},
    "upload-swap": {"title": "D3D12 upload buffers on file-backed memory"},
    "d3d12-typed-uav-load": {"title": "D3D12 typed UAV loads (report support)"},
    # madeira-bcd: the packed shader cache (madeira-d3d12/src/unix/madeira_sc_pack.c).
    "d3d12-shader-pack": {"category": "Direct3D 12", "title": "D3D12 shader cache in two pack files",
                "kind": "bool", "default": "0",
                "note": "Off by default: one file per converted shader in Documents/shadercache, as before. 1: the "
                        "converter keeps its DXBC and DXIL cache entries in two append-only files "
                        "(shadercache/pack/dxbc.mdpk, dxil.mdpk); loose entries of earlier runs move in as games use "
                        "them. Off again, the packs are not read: what moved into them converts again. Bounds: DXIL as "
                        "env.MADEIRA_D3D12_DXIL_CACHE_MB (512 MB), DXBC 2 GB; a full pack starts over. Read once per "
                        "app start; the game's own file wins."},
    # Read by DXMT's DXGI and by win32u's display adapter (sysparams_ios.c); a
    # library entry's "Report an NVIDIA GPU" sets it.
    "env.DXMT_ENABLE_NVEXT": {"category": "Direct3D 9/10/11 (DXMT)", "title": "Report an NVIDIA GPU (all games)",
                "note": "1: DXGI names NVIDIA as the vendor, DXMT's NVAPI answers and win32u registers the display "
                        "adapter as a GeForce RTX 3060 (driver 581.57). Per game: Game details > Report an NVIDIA GPU, "
                        "which also sets the matching DXGI device id."},
    "ags-rewrite": {"title": "D3D12 AMD AGS 64-bit atomics rewrite"},
    "env.MADEIRA_EXE": {"title": "Program to start at launch (Windows path or name)"},
    "env.MADEIRA_ONBOARDING": {"title": "First-run Steam setup"},
    "env.MADEIRA_XINPUT": {"title": "Physical controllers (XInput)"},
    "env.MADEIRA_TOUCH_XINPUT": {"title": "Touch controller as XInput player 1"},
    "env.MADEIRA_DINPUT_PAD": {"title": "DirectInput joystick from the host gamepad"},
    # ml2100: the HID controller (build/wineserver/hidpad_ios.c, docs/CONTROLLERS.md).
    "env.MADEIRA_PAD_MODE": {"category": "Controllers", "title": "Controller API (player 1)", "kind": "choice",
                "note": "XInput (default): every controller is an Xbox pad. hid: player 1 becomes a HID game controller, "
                        "a DualSense (054C:0CE6) when it is a PlayStation pad, else a generic HID gamepad, and leaves "
                        "XInput. dualsense/generic force the identity. Read at session start; the game's own file wins.",
                "choices": [("", "XInput (default)"), ("hid", "DirectInput / HID"), ("dualsense", "HID, always a DualSense"),
                            ("generic", "HID, always a generic gamepad")],
                "sources": ["app/Madeira/GamepadInput.swift"]},
    "env.MADEIRA_HIDPAD": {"category": "Controllers",
                "note": "Set by the app at session start from env.MADEIRA_PAD_MODE (dualsense or generic) for the "
                        "wineserver and ntdll; not meant to be set by hand."},
    "env.MADEIRA_HIDPAD_NAME": {"category": "Controllers",
                "note": "Set by the app: the product string a generic HID gamepad reports (the physical pad's name)."},
    "env.MADEIRA_HIDPAD_XINPUT": {"category": "Controllers", "title": "HID mode: keep player 1 on XInput too", "kind": "bool",
                "default": "0",
                "note": "1: with the HID controller on, player 1 also stays an XInput pad. Off by default, so a game "
                        "that reads both APIs does not see the same pad twice.",
                "sources": ["app/Madeira/GamepadInput.swift"]},
    # ml2106: game output to the physical pad (app/Madeira/PadOutput.m).
    "env.MADEIRA_PAD_OUTPUT": {"category": "Controllers", "title": "Rumble, adaptive triggers and lightbar to the pad",
                "kind": "choice",
                "note": "On (default): XInput rumble plays on the controller (CoreHaptics), and in DualSense HID mode the "
                        "game's output reports drive rumble, adaptive triggers (closest GameController mode), lightbar "
                        "and player LEDs. hid: only the DualSense's; xinput: only XInput rumble; 0: none. Read at "
                        "session start; the game's own file wins.",
                "choices": [("", "On (default)"), ("hid", "DualSense output only"), ("xinput", "XInput rumble only"),
                            ("0", "Off")],
                "sources": ["app/Madeira/GamepadInput.swift", "app/Madeira/PadOutput.m"]},
    "env.MADEIRA_PROMOTE": {"title": "Hold the display at its maximum rate"},
    # madeira-bcd: GTA V Enhanced's Social Club (StikJITHelper.swift, process_ios.c, virtual_ios.c).
    "pool-split": {"category": "Memory & JIT pool", "title": "Split JIT pool around the main thread's stack",
                "kind": "bool", "default": "0",
                "note": "1: when the pool would shrink below its size (pool, 896 MB by default) because the main "
                        "thread's stack splits the only executable band, the free run above the stack becomes a "
                        "second debugger region and both form one pool (about 880 MB instead of 560-630 MB). Costs "
                        "the second region's size in memory. Off by default; read at launch, the game's own file "
                        "wins."},
    "pool-page-fit": {"category": "Memory & JIT pool", "title": "Fit split pool regions to memory pages",
                "kind": "bool", "default": "0",
                "note": "With pool-split on: fit regions A/B to 16 KB pages instead of 16 MB steps, retaining small "
                        "remainders in free runs without increasing the requested pool budget. With pool-low on, "
                        "also fit region C to pages while preserving pool-low-margin. Costs the additional pages in memory. Off by default; read at "
                        "launch, the game's own file wins. Restart Madeira and enable JIT again after changing it."},
    "pool-pair": {"category": "Memory & JIT pool", "title": "Split JIT pool: prefer two runs above the window",
                "kind": "bool", "default": "1",
                "note": "With pool-split on: when the largest free run lies below the 0x140000000 executable window "
                        "(where the pool cannot split), the pool takes two runs above the window instead if together "
                        "they are larger (GTA V: 368 + 320 MB instead of 464 MB). Falls back to the single run if the "
                        "placement misses. On by default; 0 turns it off. Read at launch, the game's own file wins."},
    "pool-mid": {"category": "Memory & JIT pool", "title": "Split JIT pool: use the free run between the regions",
                "kind": "bool", "default": "0",
                "note": "With pool-split on: when another mapping sits between the pool's two regions and leaves a "
                        "free run of 64 MB or more there, that run becomes a third debugger region in the same pool "
                        "(RDR2 build 470: 604 MB pool, a 71 MB mapping and 213 MB free between the regions; the "
                        "DLL copies ran out and the game failed with ERR_GFX_INIT). Only from what the two regions "
                        "left of the pool size; costs that region's size in memory. Off by default (RDR2's list turns "
                        "it on); read at launch, the game's own file wins.",
                "sources": ["app/Madeira/StikJITHelper.swift", "build/ntdll-unix/virtual_ios.c"]},
    "pool-low": {"category": "Memory & JIT pool", "title": "JIT code buffers below the executable window",
                "kind": "bool", "default": "0",
                "note": "1: a free run below the 0x140000000 executable window (less pool-low-margin) "
                        "becomes a third debugger region for the emulator's code buffers, so the whole JIT pool is "
                        "left to DLL copies (GTA V: about 300 MB more). Needs the pool above the window; costs the "
                        "region's size in memory. Off by default; read at launch, the game's own file wins."},
    "pool-low-margin": {"category": "Memory & JIT pool", "title": "Code-buffer region: MB left free below the window",
                "kind": "int", "default": "128",
                "note": "With pool-low on: how much of the free run below the executable window stays free for "
                        "programs that load there (child processes' main executables). A run with a free run at "
                        "least this large below it keeps none. 128 by default."},
    "env.MADEIRA_SC_CEF": {"category": "Wine core (ntdll)", "title": "Social Club's Chromium in one process",
                "kind": "bool", "default": "1",
                "note": "On by default; 0 turns it off. SocialClubHelper.exe runs --single-process with "
                        "PartitionAllocBackupRefPtr disabled and V8 --jitless (MADEIRA_JITLESS = 0 keeps V8's JIT), its "
                        "--type= children are refused, and a process with socialclub.dll mapped that is not the "
                        "helper (the game) gets no JIT-pool copy of libcef.dll: its load fails, as it did when the "
                        "pool was full. No effect on programs without Social Club."},
    "env.MADEIRA_SC_CEF_FLAGS": {"category": "Wine core (ntdll)", "title": "Extra SocialClubHelper.exe switches",
                "note": "Appended verbatim to SocialClubHelper.exe's command line while env.MADEIRA_SC_CEF is on, "
                        "e.g. --disable-gpu or --enable-logging=file --v=1. A --js-flags= in it gets --jitless as its "
                        "first flag (Chromium keeps only the last --js-flags=), unless MADEIRA_JITLESS = 0."},
    "env.MADEIRA_SC_PA_POOLS": {"category": "Memory & JIT pool", "title": "Room for Social Club's PartitionAlloc pools",
                "kind": "choice", "default": "",
                "choices": [("", "Off"), ("1", "Layout 1 (chrome_elf.dll only)"), ("2", "Layout 2 (chrome_elf, libcef, Oilpan)")],
                "note": "1: Wine boots with the emulator's arena at 0x7d00000000 (12 GB instead of 16 GB) and keeps "
                        "0x7c00000000 +4 GB free, so SocialClubHelper.exe's 32 GB PartitionAlloc reservation, which "
                        "must start on a 32 GB boundary, gets 0x7800000000 (20 GB of it really reserved). 2: also "
                        "libcef.dll's (0x7000000000) and Oilpan's; the JIT pool's RW alias moves to 0x7900000000. "
                        "Off by default; set it in the game's own file; read at session start."},
    # madeira-bcd: the opt-in source build of DXMT's 64-bit dxgi.dll (tools/build-dxgi-dll.sh).
    "env.MADEIRA_DXGI_SRC": {"category": "Direct3D 9/10/11 (DXMT)", "title": "DXGI built from DXMT source (IDXGIFactory7)",
                "kind": "bool", "default": "0",
                "note": "1: the game runs the 64-bit dxgi.dll the CI builds from the dxmt submodule (dxgi-src.dll): "
                        "upstream's DXMT dxgi plus IDXGIFactory7 and EnumAdapterByLuid (GTA V Enhanced stops with "
                        "ERR_GFX_D3D_NOD3D12 without Factory7). Off (default): upstream's committed dxgi.dll. Set it in "
                        "the game's own file, not for every game; read at session start."},
    "avail-phys": {"category": "Memory & JIT pool", "title": "Report the memory left before the app's limit as available",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: Windows programs' available physical memory (GlobalMemoryStatusEx) is what "
                        "remains before Madeira's memory limit instead of the phone's free memory, so Chromium "
                        "(Social Club, Steam) frees caches below 1000 MB and 400 MB left, and a game can see memory "
                        "running out. Set it in the game's own file; read at session start."},
    # madeira-bcd: the memory before the game (2026-10-09, virtual_ios.c ios_avail_phys / ios_swap_image_flush).
    "avail-phys-cef-mb": {"category": "Memory & JIT pool", "title": "Available memory Chromium sees (MB)",
                "kind": "int", "default": "0",
                "note": "0 (default): off. N: Social Club's SocialClubHelper.exe and Steam's steamwebhelper.exe see at "
                        "most N MB of available physical memory, nobody else (the game and the launcher keep the real "
                        "figure). Below 400 (e.g. 300) Chromium reports critical memory pressure every 5 s and purges "
                        "caches and garbage; 400-999 (e.g. 900), moderate every 10 s. Independent of avail-phys. Set it "
                        "in the game's own file; read at session start."},
    "swap-images": {"category": "Memory & JIT pool", "title": "Swap tier: pure-x64 DLLs and programs in the file",
                "kind": "bool", "default": "0",
                "note": "Default off. 1: the sections of pure-x64 images (with env.MADEIRA_X64_IMAGE_NOCOPY = 1: "
                        "libcef.dll, steamclient64.dll, launchers, the game exe) move to the swap tier's file once they "
                        "are read, so they stop counting against the memory limit (iOS writes them back and may drop "
                        "them). Needs swap-mb. Page edges shared with a neighbouring section stay in memory. Set it in "
                        "the game's own file; read at session start."},
    # madeira-bcd: budget-change events in dxgi-src.dll (tools/patch-dxgi-budget-events.py).
    "env.MADEIRA_DXGI_BUDGET_EVENTS": {"category": "Direct3D 9/10/11 (DXMT)",
                "title": "Tell the game when its video memory budget changes",
                "kind": "bool", "default": "0", "sources": ["tools/patch-dxgi-budget-events.py"],
                "note": "Needs env.MADEIRA_DXGI_SRC = 1. The video memory budget DXGI reports shrinks as Madeira "
                        "nears its memory limit (vram-trim-mb below it). 1: a game's budget-change event is "
                        "registered for real, signalled at once and again whenever the budget moves by 32 MB, so a "
                        "game that waits for it (Red Dead Redemption 2) reads the smaller budget and frees textures. "
                        "Off (default): the registration is refused, as before. Set it in the game's own file; read "
                        "at session start."},
    # madeira-bcd: the opt-in source build of DXMT's 64-bit d3d11.dll (tools/build-d3d11-dll.sh).
    "env.MADEIRA_D3D11_SRC": {"category": "Direct3D 9/10/11 (DXMT)", "title": "D3D11 built from DXMT source (context state swap)",
                "kind": "bool", "default": "0",
                "note": "1: the game runs the 64-bit d3d11.dll the CI builds from the dxmt submodule (d3d11-src.dll): "
                        "upstream's DXMT d3d11 plus SwapDeviceContextState, which Wine's Direct2D (d2d1) calls and "
                        "upstream's aborts in (Rockstar Games Launcher exited with code 3). Off (default): upstream's "
                        "committed d3d11.dll. Set it in the game's own file, not for every game; read at session start."},
    # madeira-bcd: small staging rings in d3d11-src.dll (tools/patch-d3d11-src-small-rings.py).
    "env.DXMT_SMALL_RINGS": {"category": "Direct3D 9/10/11 (DXMT)", "title": "Small DXMT staging rings for these programs",
                "kind": "text", "default": "", "sources": ["tools/patch-d3d11-src-small-rings.py"],
                "note": "Needs env.MADEIRA_D3D11_SRC = 1. Program file names separated by ';' (e.g. "
                        "Launcher.exe;SocialClubHelper.exe), or 1 for every program: in those programs DXMT's staging "
                        "ring, copy-temp ring and upload heap use 4 MB Metal blocks instead of 32 MB. A ring keeps its "
                        "blocks until 300 later submissions and an idle program keeps them for good (Rockstar Games "
                        "Launcher 132 MB, Social Club 68 MB through a whole RDR2 session). Larger uploads still get a "
                        "block of their own; each named program logs one [small-rings] line. Unset or 0 (default): "
                        "32 MB as upstream. Set it in the game's own file; read when the program starts."},
    # madeira-bcd: the D3D12/DXGI GPU as a D3DKMT adapter (build/win32u-unix/d3dkmt_ios.c).
    "env.MADEIRA_KMT_ADAPTER": {"category": "Windows, display & input", "title": "D3DKMT adapter for the GPU (WDDM 3.1)",
                "kind": "bool", "default": "0",
                "note": "1: D3DKMTEnumAdapters2 lists the GPU DXGI and D3D12 report (same LUID) and "
                        "D3DKMTQueryAdapterInfo answers like a WDDM 3.1 driver (driver version, caps, device ids, "
                        "memory, performance data); with env.MADEIRA_DXGI_SRC = 1 DXGI's CheckInterfaceSupport gives "
                        "the same driver version. Off (default): no adapter is listed, as before. Set it in the game's "
                        "own file; read at session start."},
    "dxmt": {"title": "DXMT options (a=b;c=d)",
             "note": "Exported as DXMT_CONFIG with the options joined by ';', a library game's own dxmt options after these: "
                     "e.g. d3d11.mipClampBC=1;d3d11.preferredMaxFrameRate=30. DXMT reads at most 259 characters of it, "
                     "and nothing at all from a longer value (ml1255)."},
    "metalfx-upscale": {"title": "MetalFX upscaling factor", "kind": "choice",
             "note": "Scales the presented picture with Apple's MetalFX spatial scaler (Direct3D 11 and 12). Usually set per game in Game details > Display.",
             "choices": [("", "Off"), ("1.5", "1.5x"), ("2", "2x")]},
}


def category(path):
    p = path.replace("\\", "/")
    rules = [
        ("madeira-d3d12", "Direct3D 12"), ("dxmt", "Direct3D 9/10/11 (DXMT)"),
        ("madeira-dock", "Steam & Dock"), ("FEX/", "x86 emulation (FEX)"),
        ("winegstreamer", "Media"), ("audio", "Audio"), ("madsync", "Synchronisation"),
        ("/sync", "Synchronisation"), ("virtual_ios", "Memory & JIT pool"), ("JITAllocator", "Memory & JIT pool"),
        ("StikJIT", "Memory & JIT pool"), ("signal_", "Exceptions & threads"), ("thread", "Exceptions & threads"),
        ("wine/server", "Wine server"), ("wineserver", "Wine server"), ("win32u", "Windows, display & input"),
        ("Winios", "Windows, display & input"), ("Input", "Windows, display & input"), ("Gamepad", "Controllers"),
        ("Touch", "Controllers"), ("dinput", "Controllers"), ("xinput", "Controllers"),
        ("Steam", "Steam & Dock"), ("Dock", "Steam & Dock"), ("Onboarding", "Steam & Dock"),
        ("Library", "App & front end"), ("FPSOverlay", "App & front end"), ("app/Madeira", "App & front end"),
        ("ntdll", "Wine core (ntdll)"), ("wine/dlls", "Wine libraries"), ("build/", "Wine core (ntdll)"),
    ]
    for needle, name in rules:
        if needle in p:
            return name
    return "Other"


def comment_near(lines, i):
    """The comment on line i, else the nearest comment block within 8 lines above."""
    def trailing(line):
        for m in re.finditer(r'//+|/\*+', line):
            if line[:m.start()].count('"') % 2 == 0:   # not inside a string literal
                return line[m.end():]
        return None
    parts = []
    t = trailing(lines[i])
    if t and t.strip(" */"):
        parts.append(t)
    else:
        j = i - 1
        while j >= 0 and j >= i - 8 and not lines[j].strip().startswith(("//", "/*", "*")) \
                and not lines[j].rstrip().endswith("*/"):
            j -= 1
        block = []
        while j >= 0 and j >= i - 14:
            t = lines[j].strip()
            if t.startswith(("//", "/*", "*")) or t.endswith("*/"):
                if not t.startswith(("//", "/*", "*")):
                    t = trailing(t) or t
                block.insert(0, t)
                j -= 1
            else:
                break
        parts += block
    text = " ".join(parts)
    text = re.sub(r'/\*+|\*+/|//+', ' ', text)
    text = re.sub(r'(^|\s)\*(\s|$)', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip(" *-")
    return (text[:237] + "...") if len(text) > 240 else text


def scan():
    opts = {}
    for repo, dirs in REPOS.items():
        base = os.path.join(ROOT, repo)
        if not os.path.isdir(base):
            continue
        out = subprocess.run(["git", "-C", base, "ls-files", "--"] + dirs,
                             capture_output=True, text=True).stdout.split()
        for rel in out:
            path = os.path.normpath(os.path.join(repo, rel))
            if not path.endswith(EXT) or any(s in path for s in SKIP):
                continue
            try:
                txt = open(os.path.join(ROOT, path), errors="ignore").read()
            except OSError:
                continue
            lines = txt.split("\n")
            for pat, is_env in ((CFG, False), (ENV, True)):
                for m in pat.finditer(txt):
                    reader, name, arg = m.group(1), m.group(2), (m.group(3) or "").strip()
                    if is_env or reader == "MadeiraConfig.flag":
                        key = "env." + name
                    else:
                        key = name
                    ln = txt.count("\n", 0, m.start())
                    kind = "bool" if reader in BOOL_READERS else "int" if reader in INT_READERS else "text"
                    dflt = ""
                    if reader in ("madeira_cfg_int", "madeira_cfg_bool", "mad_cfg_int_pe"):
                        dflt = arg if re.fullmatch(r'-?\d+|0x[0-9a-fA-F]+', arg or "") else ""
                    elif reader in ("MadeiraConfig.flag", "flag", "envFlag"):
                        dflt = "0" if arg.endswith("false") else "1" if (not arg or arg.endswith("true")) else ""
                    elif reader in ("madeiraSwitch", "madeira_switch_for_caller"):
                        dflt = "32-bit only"
                    elif reader == "MadeiraConfig.bool":
                        dflt = "1" if arg.endswith("true") else "0"
                    o = opts.setdefault(key, {"key": key, "kind": kind, "default": dflt, "sites": []})
                    if o["kind"] == "text" and kind != "text":
                        o["kind"] = kind
                    if not o["default"] and dflt:
                        o["default"] = dflt
                    o["sites"].append((path, ln + 1, comment_near(lines, ln)))
    # The engine code that acts on an option describes it better than the
    # Settings UI that merely reads it back: prefer sites outside app/.
    for o in opts.values():
        sites = sorted(o.pop("sites"), key=lambda s: (s[0].startswith("app/"), s[0], s[1]))
        o["category"] = category(sites[0][0])
        o["note"] = next((n for _, _, n in sites if n), "")
        files = []
        for p, _, _ in sites:                 # file names only: line numbers would make
            if p not in files:                # the catalog stale on every unrelated edit
                files.append(p)
        o["sources"] = files[:3]
    for key, extra in OVERLAY.items():
        o = opts.setdefault(key, {"key": key, "kind": "text", "default": "", "category": "App & front end",
                                  "note": "", "sources": []})
        o.update({k: v for k, v in extra.items()})
    return [opts[k] for k in sorted(opts, key=lambda k: (opts[k]["category"], k.lower()))]


def swift_str(s):
    return json.dumps(s, ensure_ascii=False)


def render(opts):
    out = ["// Generated by build/tools/gen-config-catalog.py from the sources that read each option.",
           "// Do not edit by hand: run the script (tests/host/check-config-catalog.py fails when stale).",
           "", "extension ConfigCatalog {", "    static let generated: [ConfigOption] = ["]
    for o in opts:
        choices = ", ".join(f"({swift_str(v)}, {swift_str(l)})" for v, l in o.get("choices", []))
        sources = ", ".join(swift_str(s) for s in o["sources"])
        out.append(f'        ConfigOption(key: {swift_str(o["key"])}, title: {swift_str(o.get("title", ""))}, '
                   f'kind: .{o["kind"]}, defaultValue: {swift_str(o["default"])}, '
                   f'category: {swift_str(o["category"])}, note: {swift_str(o["note"])}, '
                   f'choices: [{choices}], sources: [{sources}]),')
    out += ["    ]", "}", ""]
    return "\n".join(out)


def main():
    text = render(scan())
    if "--check" in sys.argv:
        cur = open(OUT).read() if os.path.exists(OUT) else ""
        if cur != text:
            print("ConfigCatalog.generated.swift is out of date: run build/tools/gen-config-catalog.py")
            return 1
        print("ConfigCatalog.generated.swift is current")
        return 0
    open(OUT, "w").write(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
