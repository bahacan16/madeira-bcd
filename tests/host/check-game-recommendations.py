#!/usr/bin/env python3
"""Madeira's built-in recommended settings per game (owner's decision 2026-10-09).

Source checks (every host): the lists in app/Madeira/GameRecommendations.swift
match the working lists they were taken from (Red Dead Redemption 2's line for
line), every key that is not env.NAME is one Madeira reads (the generated
settings catalog), no list carries DLL overrides for Rockstar's Social Club,
and the app applies them where the owner asked: a game's first sight (library
start-up, saving an entry, Game details, a start, the madeira-bcd home screen)
and Reset to Recommended at the bottom of both settings pages.

The Swift fixture (macOS CI) compiles the real GameRecommendations.swift and
GameRecommendationsApply.swift against small stand-ins for the app types and
checks how games are recognised (Steam games by App ID or by their folder
under steamapps\\common, every other game by its program's name, GTA V
Enhanced inside or outside a Steam library) and the first-sight, update and
reset rules. Linux hosts without Swift run the source checks; --require-swift
turns a missing compiler into a failure.
"""
from pathlib import Path
import argparse
import os
import re
import shutil
import subprocess
import tempfile

parser = argparse.ArgumentParser()
parser.add_argument('--require-swift', action='store_true')
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
app = root / 'app/Madeira'
lists_src = (app / 'GameRecommendations.swift').read_text()
apply_src = (app / 'GameRecommendationsApply.swift').read_text()
library = (app / 'Library.swift').read_text()
home = (app / 'HomeView.swift').read_text()
content = (app / 'ContentView.swift').read_text()
profiles = (app / 'GameProfiles.swift').read_text()
catalog = (app / 'ConfigCatalog.generated.swift').read_text()
project = (root / 'app/Madeira.xcodeproj/project.pbxproj').read_text()


def block(source, start, end):
    i = source.index(start)
    return source[i:source.index(end, i) + len(end)]


# The lists, as Swift sees them: the multi-line literal loses its closing
# delimiter's indentation.
presets = {}
for m in re.finditer(r'static let (\w+) = GameRecommendation\(\n\s*id: "([^"]+)", title: "([^"]+)", version: (\d+),\n'
                     r'\s*config: """\n(.*?)\n( *)""",\n(.*?)\)\n', lists_src, re.S):
    name, ident, title, version, body, indent, switches = m.groups()
    lines = [line[len(indent):] if line.startswith(indent) else line for line in body.split('\n')]
    presets[ident] = dict(name=name, title=title, version=int(version), lines=lines, switches=' '.join(switches.split()))
assert set(presets) == {'rdr2-steam', 'gta5e-steam', 'gta5e-other', 'ghost-of-tsushima', 'god-of-war',
                        'horizon-zero-dawn'}, sorted(presets)
assert 'static let all = [rdr2Steam, gta5EnhancedSteam, gta5EnhancedOther, ghostOfTsushima, godOfWar, horizonZeroDawn]' in lists_src

catalog_keys = set(re.findall(r'ConfigOption\(key: "([^"]+)"', catalog))
for ident, p in presets.items():
    assert p['version'] >= 1, ident
    assert p['lines'][0].startswith('# Madeira') and p['lines'][1].startswith('# Reset to Recommended'), ident
    seen = set()
    for line in p['lines'][2:]:
        assert line == line.strip() and not line.startswith('#'), (ident, line)
        key, sep, value = line.partition(' = ')
        assert sep and key and value, (ident, line)
        assert key not in seen, (ident, 'twice', key)
        seen.add(key)
        if key.startswith('env.'):
            assert re.fullmatch(r'env\.[A-Za-z_][A-Za-z0-9_]*', key), (ident, key)
        else:
            assert key in catalog_keys, (ident, 'not a key Madeira reads', key)
        # Madeira's own settings only: nothing that loads a replaced Social Club. SocialClubHelper.exe
        # may be named as a program in a list (env.DXMT_SMALL_RINGS), never in a DLL override.
        named = line.lower()
        if key != 'env.WINEDLLOVERRIDES':
            named = named.replace('socialclubhelper.exe', '')
        assert 'socialclub' not in named and 'MADEIRA_DLL_LOCAL' not in line, (ident, line)
print('PASS: six lists, two comment lines each, no key twice, every non-env key in the settings catalog, no Social Club DLL lines')

# Red Dead Redemption 2 from Steam: the owner's list of 2026-10-09 (build 463), line for line,
# plus the lines of build 466's test (version 2), build 467's (version 3), build 470's (version 4) and
# build 472's (version 5: pool-mid), build 475's (version 6: ring-share), build 476's (version 7:
# swap-images, small DXMT rings for the launcher and Social Club, the ExecuteIndirect probe off)
# and build 480's (version 9: xinput1_4.dll loaded with the D3D12 device, the pool back to 128 MB of
# image headroom after 479's 192 starved the game's code buffer, the orphan-lock reaper stamped-only).
rdr2 = '''d3d12-caps-log = 2
d3d12-shader-pack = 1
replay-split = 1
ring-share = 1
pool-low = 1
pool-mid = 1
pool-page-fit = 1
pool-split = 1
vram-mb = 2304
swap-mb = 8192
swap-mode = 2
swap-min-mb = 1
swap-images = 1
pso-warm = 2
fence-chain = 6
avail-phys = 1
indirect-fast = 1
ind-probe = 0
fullscreen-window = 1
env.WINE_D3D_CONFIG = renderer=no3d
env.FEX_VECTORTSOENABLED = 0
env.MADEIRA_BAND_CENSUS = 1
env.MADEIRA_RDR2_VA_HOLD = 1
env.MADEIRA_SWAP_RESERVE_MAX_MB = 9216
env.MADEIRA_THREAD_STACK_SPILL = 1
env.MADEIRA_SMALL_STACK_EXES = RDR2.exe
env.MADEIRA_D3D11_SRC = 1
env.MADEIRA_DOCK_GAME_SCM = 1
env.MADEIRA_DOCK_KEEP_ALIVE = Launcher.exe;RDR2.exe
env.MADEIRA_DXGI_SRC = 1
env.MADEIRA_DXGI_BUDGET_EVENTS = 1
env.DXMT_WSI_MONITOR_IDENTITY = 1
env.DXMT_WSI_MODE_TABLE = 1
env.DXMT_SMALL_RINGS = Launcher.exe;SocialClubHelper.exe
env.MADEIRA_EXECREQ_LEAVE = 1
env.MADEIRA_LOCK_ORPHAN = stamped
env.MADEIRA_PIN_GRAPHICS_DLLS = 1
env.MADEIRA_PRELOAD_DLLS = xinput1_4.dll
env.MADEIRA_POOL_HEAD_RESERVE_MB = 128
env.MADEIRA_POOL_LOW_IMAGES = 1
env.MADEIRA_POOL_RECYCLE_IMAGES = 1
env.MADEIRA_SC_PA_POOLS = 2
env.MADEIRA_SPAWN_BLOCK = RockstarErrorHandler
env.MADEIRA_TOUCH_MOUSE = 1
env.MADEIRA_WOW_MIN_FREE_GB = 2
env.MADEIRA_X64_GRAPHICS_ENTRY = 1
env.MADEIRA_X64_IMAGE_NOCOPY = 1
env.WINEDLLOVERRIDES = video64=
env.MADEIRA_DEVICE_STATS = 1
env.MADEIRA_METAL_HUD_MAIN = 1'''.split('\n')
assert presets['rdr2-steam']['lines'][2:] == rdr2, 'the RDR2 list differs from the owner\'s'
assert presets['rdr2-steam']['version'] == 9, presets['rdr2-steam']['version']
assert presets['rdr2-steam']['switches'] == ('avx: false, nvidia: false, wineVCRT: false, fastSync: true, '
                                             'semaphoreFastPath: false, resolution: GameRecommendation.screen720, display: "fit"'), presets['rdr2-steam']['switches']
assert 'static let screen720 = "screen@720"' in lists_src and 'resolution == Self.screen720 ? "this screen\'s shape at 720 lines" : resolution' in lists_src
assert 'if size != GameRecommendation.screen720 { entry.resolution = size }\n            else if let lines720 = ResolutionChoices.screen720Value { entry.resolution = lines720 }' in apply_src
gta = presets['gta5e-steam']
assert 'env.MADEIRA_CHILD_ARGS = PlayGTAV.exe -nobattleye' in gta['lines']
assert 'env.MADEIRA_DOCK_KEEP_ALIVE = Launcher.exe;GTA5_Enhanced.exe' in gta['lines']
assert 'semaphoreFastPath: true, fpsMode: 4' in gta['switches'] and 'nvidia: true' in gta['switches']
assert 'env.MADEIRA_FASTSYNC_SEM = 1' in presets['gta5e-other']['lines']
assert 'resolution: "1280x720"' in presets['ghost-of-tsushima']['switches'] and 'avx: true' in presets['ghost-of-tsushima']['switches']
assert 'resolution: "1920x1080"' in presets['god-of-war']['switches']
# Horizon Zero Dawn, still being brought up: build 477's test list (register spaces other than 0,
# DXGI lists the virtual monitor's own modes so the game can pick this screen's resolution, its
# window is made foreground while none is, XInput pads reach Windows.Gaming.Input, and the
# shaders the converter refuses are retried with sized ranges, remembered and replaced by
# placeholder pipelines; pipelines are warmed in the background). Version 5 (build 480): the
# orphan-lock reaper releases only locks a live thread has stamped. Version 6 (build 481): shader
# libraries only for pipelines in use, rectangle clears, the game's memory in the swap file
# (broad, from 1 MB), and no pso-warm (its queue held 24,570 pipelines when iOS closed the game).
# Version 7 (build 483): the skin-check and view-census diagnostics for its compute-skinned
# characters (missing faces, missing hair, spikes every few frames on 481, gpu-sync or not).
# Version 8 (build 484): every candidate fix at once, one line each, to be halved if it helps:
# no wave operations, raw views readable as typed, ShaderAtomic on R32 typed UAVs, and FEX's
# x86 ordering for vector, memcpy and split-lock accesses and for images with volatile metadata.
# Version 9 (build 485): none of those helped (owner, 484 test), so they are gone again;
# typed buffer views start at Apple's 16-byte boundary, and skin-check traces whole frames and
# dumps the compute skinning's data, comparing what the GPU read with what the CPU wrote.
# Version 10 (build 486): the characters came out whole on 485; typed views start exactly at
# their first element where the probe allows (plants and lighting still flickered), and the
# skinning diagnostics are gone again (view-census stays, it counts views left with an offset).
# Version 11 (build 487): nothing changed on 486 (the GPU rounds a texture buffer's start down
# to 16 bytes; walls, trees and plants vanished for single frames); typed views of upload
# memory off a 16-byte boundary read an aligned copy, a queue's wait holds until the awaited
# work is committed, and vis-trace logs every frame's draws and the culling pyramid.
# Version 12 (build 489): desc-guard logs descriptors rewritten while a batch on the GPU uses them.
hzd = presets['horizon-zero-dawn']
assert hzd['lines'][2:] == ['dxbc-register-spaces = 1', 'msc-unbounded-retry = 1', 'msc-fail-memo = 1',
                            'pso-placeholder = 1', 'pso-lazy-libs = 1', 'clear-rects = 1',
                            'typed-view-align = 16', 'typed-view-shadow = 1', 'fence-strict = 1',
                            'view-census = 1', 'vis-trace = 1', 'desc-guard = 1',
                            'swap-mb = 6144', 'swap-mode = 2', 'swap-min-mb = 1',
                            'env.DXMT_WSI_MONITOR_IDENTITY = 1', 'env.DXMT_WSI_MODE_TABLE = 1',
                            'env.MADEIRA_INPUT_FOREGROUND = 1', 'env.MADEIRA_WGI_HOST_PADS = 1',
                            'env.MADEIRA_LOCK_ORPHAN = stamped', 'env.WINEDEBUG = fixme-input'], hzd['lines']
assert hzd['version'] == 12 and hzd['switches'] == 'avx: false, nvidia: false, wineVCRT: false, fastSync: true, display: "fit"', hzd
assert not any(line.startswith(('d3d12-wave-ops', 'd3d12-raw-typed-views', 'typed-uav-atomic', 'ignore-volatile-metadata',
                                 'env.FEX_MEMCPYSETTSOENABLED', 'env.FEX_STRICTINPROCESSSPLITLOCKS'))
               for p in presets.values() for line in p['lines']), 'the build 484 experiment is in no list'
assert not any(line.startswith(('pso-lazy-libs', 'clear-rects', 'skin-check', 'skin-dump', 'view-census', 'typed-view-align',
                                 'typed-view-shadow', 'fence-strict', 'vis-trace', 'readback-far', 'desc-guard', 'env.FEX_VECTORTSOENABLED = 1'))
               for p in presets.values() if p is not hzd for line in p['lines']), 'the build 481-489 switches are Horizon Zero Dawn\'s only'
print('PASS: RDR2 (Steam) is the owner\'s 463 list plus the 466, 467, 470, 472, 475, 476 and 480 test lines, line for line with its switches (720 lines of this screen); GTA V Enhanced, GoT and GoW keep their tested lines; Horizon Zero Dawn has build 489\'s test list')

# Recognition: Steam by App ID or steamapps\common folder; everything else by the program's name.
match = block(lists_src, 'static func match(', '\n    }\n')
assert 'rdr2AppID = 1174180, gta5EnhancedAppID = 3240220, ghostAppID = 2215430, godOfWarAppID = 1593500,\n               horizonZeroDawnAppID = 1151640' in lists_src
assert 'let installFolder = last == steamFolder ? steamFolder : nil' in match
assert 'installFolder == "red dead redemption 2"' in match and 'installFolder == "grand theft auto v enhanced"' in match
assert 'case "ghostoftsushima.exe":' in match and 'case "gow.exe":' in match and 'case "rdr2.exe", "playrdr2.exe":' in match
assert 'case "horizonzerodawn.exe":\n            return horizonZeroDawn' in match and 'if steamAppID == horizonZeroDawnAppID { return horizonZeroDawn }' in match
assert 'case "playgtav.exe", "gta5_enhanced.exe":' in match and 'return inSteam ? gta5EnhancedSteam : gta5EnhancedOther' in match
assert 'guard fileExists(folder + "\\\\GTA5_Enhanced.exe") else { return nil }' in match and 'default:\n            return nil' in match

# Where the app applies them.
save = block(library, '    func save(_ entry: LibraryEntry) {', '\n    }\n')
assert save.index('let isNew = !next.contains { $0.id == entry.id || (entry.steamAppID != nil && $0.steamAppID == entry.steamAppID) }') \
    < save.index('GameRecommendations.prepare(&entry, new: isNew)') < save.index('persist(next)')
init = block(library, '    private init() {', '\n    }\n')
assert init.index('resetPhoneResolution()') < init.index('applyRecommendations()')
apply_all = block(library, '    private func applyRecommendations() {', '\n    }\n')
assert 'guard !readOnly' in apply_all and 'GameRecommendations.prepare(&next[i])' in apply_all and 'persist(next)' in apply_all
detail = block(library, 'struct LibraryDetail: View {', '\n/// madeira-bcd: a game\'s own options as the fork stores them')
assert '.onAppear { migrateEntryOptions(); prepareRecommendation(); reloadBCD() }' in detail
assert 'let isNew = !model.entries.contains { $0.id == entry.id }' in detail
assert 'if GameRecommendations.prepare(&entry, new: isNew) { model.save(entry) }' in detail
# Adding a game by hand opens its page with the entry the library stored.
assert 'model.save(entry); browser = false; selected = model.entries.first { $0.id == entry.id } ?? entry' in library
assert 'recommended = GameRecommendations.reset(&entry)' in detail
reset_section = detail[detail.index('Button("Reset to Recommended", systemImage: "arrow.counterclockwise")'):]
assert reset_section.index('Button("Reset", role: .destructive) { resetToRecommended() }') < reset_section.index('Text(GameRecommendations.note(recommended))')
# Last in the form, after the Executable section and Remove from library.
assert detail.index('Button("Remove from library"') < detail.index('Button("Reset to Recommended"') < detail.index('if let error { Section')
assert '!remove, !confirmReset else { return }' in detail
launch = block(content, '    private func launchLibraryEntry(_ entry: LibraryEntry) {', '\n    }\n')
assert launch.index('if GameRecommendations.prepare(&entry, new: isNew) { library.save(entry) }') < launch.index('BCDLaunch.applyLibrary(entry, sessionLog: false)')
home_launch = block(home, '    private func launch(_ game: LibraryGame) {', '\n    }\n')
assert home_launch.index('GameRecommendations.prepare(windowsPath: exe.windowsPath, title: game.title)') < home_launch.index('request.avx = LibraryPrefs.avx')
sheet = block(home, 'struct GameSettingsSheet: View {', '\n// MARK: - App settings')
assert 'recommended = GameRecommendations.reset(windowsPath: exePath, title: game.title)' in sheet
assert sheet.index('Label("Save and play"') < sheet.index('Label("Reset to Recommended", systemImage: "arrow.counterclockwise")')
assert 'Text(GameRecommendations.note(recommended, home: true))' in sheet
assert 'guard !prepared else { return }' in sheet and sheet.count('prepareRecommendation()') == 3
for name in ('GameRecommendations.swift', 'GameRecommendationsApply.swift'):
    assert project.count(f'/* {name} in Sources */') == 2, name   # PBXBuildFile and the Sources phase
    assert project.count(f'/* {name} */') == 3, name              # fileRef, PBXFileReference and the group
# The rules: first sight only for a game with nothing of its own; switches only on first sight or a reset.
due = block(apply_src, '    private static func due(', '\n    }\n')
assert due.index('guard mark != "", let rec = recommendation(') < due.index('let text = GameProfile(windowsPath: windowsPath).text')
assert 'guard mark != stamp(rec), mark.hasSuffix("#" + fileHash(text)) else { return nil }' in due
assert '!(text + (ownLines ?? "")).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty' in due and 'setMark("", windowsPath)' in due
put = block(apply_src, '    private static func put(', '\n    }\n')
assert 'guard file.text == rec.fileText' in put and 'if kind != .updated {' in put
assert 'LibraryPrefs.setSafeSync(!on, for: windowsPath)' in put and 'LibraryPrefs.setScreen(size, for: windowsPath)' in put
home_lines = block(apply_src, '    private static func libraryLines(for windowsPath: String) -> String {', '\n    }\n')
assert 'if entry.steamAppID != nil, key.hasPrefix(path + "\\\\")' in home_lines and 'GameProfile(windowsPath: entry.windowsPath).text' in home_lines
assert 'takeEntryFields(rec, &entry)\n            LibraryModel.shared.save(entry)' in apply_src
print('PASS: first sight in the library (start-up, save, Game details, start) and the home screen; Reset to Recommended last on both pages')

# The Swift fixture: the real files with small stand-ins for the app types they use.
stable_hash = block(home, '    static func stableHash(_ s: String) -> UInt64 {', '\n    }\n')
profile_text = block(profiles, '    var text: String {', '\n    }\n')
profile_parse = block(profiles, '    static func parse(_ text: String) -> [String: String] {', '\n    }\n')
stubs = r'''import Foundation

enum CoverStore {
''' + stable_hash + r'''}

enum DisplayMode: String { case fit, fill, stretch, aspect }

enum ResolutionChoices { static var screen720Value: String? { "1568x720" } }   // a 19.5:9 iPhone

final class LibraryModel {
    static let shared = LibraryModel()
    var entries: [LibraryEntry] = []
    func save(_ entry: LibraryEntry) {
        if let i = entries.firstIndex(where: { $0.windowsPath == entry.windowsPath }) { entries[i] = entry } else { entries.append(entry) }
    }
    static var drive: URL { URL(fileURLWithPath: ProcessInfo.processInfo.environment["MADEIRA_TEST_DRIVE"]!, isDirectory: true) }
}

final class LogStore {
    static let shared = LogStore()
    enum Level { case info, error }
    var lines: [String] = []
    func log(_ message: String, level: Level = .info) { lines.append(message) }
}

struct LibraryEntry {
    var title: String
    var windowsPath: String
    var steamAppID: Int? = nil
    var desktop: Bool? = nil
    var config: String? = nil
    var resolution = "1408x648"
    var display: String? = nil
    var fpsMode = 1
    var fastSync: Bool? = nil
    var semaphoreFastPath: Bool? = nil
}

final class PrefStore {
    static let shared = PrefStore()
    var flags: [String: Bool] = [:]
    var screens: [String: String] = [:]
}

enum LibraryPrefs {
    static let screenSizes = ["", "1280x720", "fill", "fill-mfx15", "1600x900", "1920x1080"]
    static func avx(_ path: String) -> Bool { PrefStore.shared.flags["avx|" + path] ?? false }
    static func setAVX(_ on: Bool, for path: String) { PrefStore.shared.flags["avx|" + path] = on ? true : nil }
    static func nvidia(_ path: String) -> Bool { PrefStore.shared.flags["nvidia|" + path] ?? false }
    static func setNvidia(_ on: Bool, for path: String) { PrefStore.shared.flags["nvidia|" + path] = on ? true : nil }
    static func wineVCRT(_ path: String) -> Bool { PrefStore.shared.flags["vcrt|" + path] ?? false }
    static func setWineVCRT(_ on: Bool, for path: String) { PrefStore.shared.flags["vcrt|" + path] = on ? true : nil }
    static func safeSync(_ path: String) -> Bool { PrefStore.shared.flags["safe|" + path] ?? false }
    static func setSafeSync(_ on: Bool, for path: String) { PrefStore.shared.flags["safe|" + path] = on ? true : nil }
    static func screen(_ path: String) -> String { PrefStore.shared.screens[path] ?? "" }
    static func setScreen(_ size: String, for path: String) { PrefStore.shared.screens[path] = size.isEmpty ? nil : size }
}

struct GameProfile {
    let windowsPath: String
    var url: URL? {
        URL(fileURLWithPath: ProcessInfo.processInfo.environment["MADEIRA_TEST_CONFIGS"]!, isDirectory: true)
            .appendingPathComponent(String(format: "%016llx.cfg", CoverStore.stableHash(windowsPath.lowercased())))
    }
''' + profile_text + profile_parse + '}\n'

main = r'''import Foundation

func check(_ ok: Bool, _ what: String, line: Int = #line) {
    if !ok { print("FAILED (main.swift line \(line)): \(what)"); exit(1) }
}

let R = GameRecommendations.self
let steam = #"C:\Program Files (x86)\Steam\steamapps\common"#

func id(_ path: String, _ app: Int? = nil, files: [String] = []) -> String {
    let have = Set(files.map { $0.lowercased() })
    return R.match(windowsPath: path, steamAppID: app, fileExists: { have.contains($0.lowercased()) })?.id ?? "none"
}

// How a game is recognised.
let rdr2 = steam + #"\Red Dead Redemption 2"#
check(id(rdr2, 1174180) == "rdr2-steam", "RDR2 by its App ID")
check(id(rdr2) == "rdr2-steam", "RDR2 by its Steam folder")
check(id("D:\\SteamLibrary\\steamapps\\common\\Red Dead Redemption 2\\") == "rdr2-steam", "another Steam library, trailing backslash")
check(id(#"C:\Games\Red Dead Redemption 2\RDR2.exe"#) == "none", "RDR2 outside Steam has no list")
check(id(rdr2 + #"\RDR2.exe"#) == "rdr2-steam" && id(rdr2 + #"\PlayRDR2.exe"#) == "rdr2-steam", "RDR2's programs inside Steam")
check(id(rdr2 + #"\Redistributables\vc_redist.x64.exe"#) == "none", "a redistributable in a game's Steam folder")
check(id(rdr2 + #"\start.bat"#) == "none" && id(steam + #"\Grand Theft Auto V Enhanced\launch.cmd"#) == "none",
      "a batch file inside a Steam game's folder")
let gta = steam + #"\Grand Theft Auto V Enhanced"#
check(id(gta, 3240220) == "gta5e-steam", "GTA V Enhanced by its App ID")
check(id(gta) == "gta5e-steam", "GTA V Enhanced by its Steam folder")
check(id(gta + #"\PlayGTAV.exe"#, files: [gta + #"\GTA5_Enhanced.exe"#]) == "gta5e-steam", "PlayGTAV.exe inside Steam is the Steam copy")
let renamed = steam + #"\GTAV Enhanced Copy"#
check(id(renamed + #"\PlayGTAV.exe"#, files: [renamed + #"\GTA5_Enhanced.exe"#]) == "gta5e-steam", "in any Steam library folder")
check(id(gta + #"\_CommonRedist\vc_redist.x64.exe"#, files: [gta + #"\GTA5_Enhanced.exe"#]) == "none", "not its redistributables")
let other = #"C:\Games\GTA V Enhanced"#
check(id(other + #"\PlayGTAV.exe"#, files: [other + #"\GTA5_Enhanced.exe"#]) == "gta5e-other", "PlayGTAV.exe elsewhere")
check(id(other + #"\GTA5_Enhanced.exe"#, files: [other + #"\GTA5_Enhanced.exe"#]) == "gta5e-other", "GTA5_Enhanced.exe elsewhere")
check(id(#"C:\GAMES\gta v enhanced\PLAYGTAV.EXE"#, files: [other + #"\gta5_enhanced.exe"#]) == "gta5e-other", "names in any case")
check(id("C:/Games/GTA V Enhanced/PlayGTAV.exe", files: [other + #"\GTA5_Enhanced.exe"#]) == "gta5e-other", "forward slashes")
check(id(#"C:\Games\GTA V\PlayGTAV.exe"#, files: [#"C:\Games\GTA V\GTA5.exe"#]) == "none", "the legacy edition has no list")
check(id(other + #"\PlayGTAV.exe"#) == "none", "PlayGTAV.exe without GTA5_Enhanced.exe beside it")
check(id(#"C:\GhostOfTsushima\GhostOfTsushima.exe"#) == "ghost-of-tsushima", "GoT by its program")
check(id(#"E:\Some Other Folder\ghostoftsushima.EXE"#) == "ghost-of-tsushima", "GoT anywhere, in any case")
check(id(steam + #"\Ghost of Tsushima DIRECTOR'S CUT"#, 2215430) == "ghost-of-tsushima", "GoT from Steam")
check(id(steam + #"\Ghost"#, 1, files: [steam + #"\Ghost\GhostOfTsushima.exe"#]) == "none", "a library Steam game is its App ID or folder name")
check(id(#"C:\God of War\GoW.exe"#) == "god-of-war", "GoW by its program")
check(id(steam + #"\God of War"#, 1593500) == "god-of-war", "GoW from Steam")
check(id(#"C:\Games\God of War Ragnarok\GoWR.exe"#) == "none", "Ragnarok is another game")
check(id(#"C:\Horizon - Zero Down CE\HorizonZeroDawn.exe"#) == "horizon-zero-dawn", "Horizon Zero Dawn by its program (GOG)")
check(id(steam + #"\Horizon Zero Dawn\HorizonZeroDawn.exe"#) == "horizon-zero-dawn", "its program inside Steam")
check(id(steam + #"\Horizon Zero Dawn"#, 1151640) == "horizon-zero-dawn", "Horizon Zero Dawn from Steam")
check(id(#"C:\Games\Horizon Zero Dawn Remastered\HorizonZeroDawnRemastered.exe"#) == "none", "the remaster is another game")
check(id(#"C:\Games\Foo\foo.exe"#) == "none" && id(steam + #"\Portal 2"#, 620) == "none", "other games")
check(id("") == "none" && id("C:\\") == "none", "no path")
print("PASS: Steam games by App ID or steamapps\\common folder, GTA V Enhanced inside and outside Steam, GoT, GoW and Horizon Zero Dawn by their programs, others none")

// The lists themselves.
check(Set(R.all.map(\.id)).count == R.all.count, "ids are unique")
for rec in R.all {
    check(rec.fileText.hasSuffix("\n") && !rec.fileText.hasSuffix("\n\n"), "\(rec.id): one trailing newline")
    check(GameProfile.parse(rec.config).count == rec.config.split(separator: "\n").count - 2, "\(rec.id): every line but the comments parses")
}
check(R.gta5EnhancedSteam.switchSummary == "AVX off, NVIDIA on, Wine's C++ runtime off, fast semaphore waits on, FPS limit 40",
      R.gta5EnhancedSteam.switchSummary)
check(R.gta5EnhancedSteam.summary(home: true) == "AVX off, NVIDIA on, Wine's C++ runtime off", R.gta5EnhancedSteam.summary(home: true))
check(R.note(R.ghostOfTsushima, home: true).contains("1280x720") && !R.note(R.ghostOfTsushima, home: true).contains("semaphore"),
      R.note(R.ghostOfTsushima, home: true))
check(R.note(R.rdr2Steam).contains("@RDR2_CONFIG_LINES@ config lines") && R.note(R.rdr2Steam).contains("this screen's shape at 720 lines") && R.note(nil).contains("no recommended settings"), R.note(R.rdr2Steam))

// First sight, the player's changes, newer lists and Reset to Recommended.
let key = "madeira.recommended.applied"
UserDefaults.standard.removeObject(forKey: key)
func mark(_ path: String) -> String? { (UserDefaults.standard.dictionary(forKey: key) as? [String: String])?[path.lowercased()] }
func file(_ path: String) -> String { GameProfile(windowsPath: path).text }

var red = LibraryEntry(title: "Red Dead Redemption 2", windowsPath: rdr2, steamAppID: 1174180)
red.display = "fill"
LibraryPrefs.setNvidia(true, for: rdr2)
check(R.prepare(&red), "first sight takes the entry's fields")
check(file(rdr2) == R.rdr2Steam.fileText, "first sight writes the list")
check(red.fastSync == true && red.semaphoreFastPath == false && red.display == nil && red.fpsMode == 1, "first sight's entry fields")
check(red.resolution == "1568x720", "this screen's shape at 720 lines")
check(!LibraryPrefs.avx(rdr2) && !LibraryPrefs.nvidia(rdr2) && !LibraryPrefs.wineVCRT(rdr2), "first sight's switches")
check(mark(rdr2)?.hasPrefix("rdr2-steam#\(R.rdr2Steam.version)#") == true, "what was written is recorded")
check(!R.prepare(&red) && file(rdr2) == R.rdr2Steam.fileText, "once")
GameProfile(windowsPath: rdr2).text = R.rdr2Steam.fileText + "fence-chain = 1\n"
check(!R.prepare(&red) && file(rdr2).hasSuffix("fence-chain = 1\n"), "a changed config is the player's")
GameProfile(windowsPath: rdr2).text = ""
check(!R.prepare(&red) && file(rdr2).isEmpty, "an emptied config stays empty")
red.fpsMode = 2
LibraryPrefs.setNvidia(true, for: rdr2)
check(R.reset(&red)?.id == "rdr2-steam" && file(rdr2) == R.rdr2Steam.fileText, "Reset writes the list again")
check(!LibraryPrefs.nvidia(rdr2) && red.fpsMode == 2, "Reset writes the list's switches and leaves the rest")
// A list Madeira wrote that nobody changed follows a newer version: the config file only.
let older = "# an older list\nvram-mb = 2048\n"
GameProfile(windowsPath: rdr2).text = older
var marks = (UserDefaults.standard.dictionary(forKey: key) as? [String: String]) ?? [:]
marks[rdr2.lowercased()] = "rdr2-steam#0#" + String(CoverStore.stableHash(older), radix: 16)
UserDefaults.standard.set(marks, forKey: key)
LibraryPrefs.setAVX(true, for: rdr2)
check(!R.prepare(&red) && file(rdr2) == R.rdr2Steam.fileText, "an untouched older list is updated")
check(LibraryPrefs.avx(rdr2) && !R.prepare(&red), "an update leaves the switches, once")
print("PASS: first sight writes the list and its switches once; changed or emptied configs stay; Reset writes it again; untouched lists follow newer versions")

var gtaEntry = LibraryEntry(title: "GTA V Enhanced", windowsPath: gta, steamAppID: 3240220)
GameProfile(windowsPath: gta).text = "vram-mb = 4096\n"
check(!R.prepare(&gtaEntry) && file(gta) == "vram-mb = 4096\n" && mark(gta) == "" && gtaEntry.fpsMode == 1, "a config of its own stays")
GameProfile(windowsPath: gta).text = ""
check(!R.prepare(&gtaEntry) && file(gta).isEmpty, "and is not filled in later")
check(R.reset(&gtaEntry)?.id == "gta5e-steam" && file(gta) == R.gta5EnhancedSteam.fileText, "GTA V Enhanced Reset")
check(gtaEntry.fpsMode == 4 && gtaEntry.semaphoreFastPath == true && LibraryPrefs.nvidia(gta) && !LibraryPrefs.avx(gta), "its switches")
let gowExe = #"C:\Games\God of War\GoW.exe"#
var gow = LibraryEntry(title: "God of War", windowsPath: gowExe)
gow.config = "dxmt = d3d11.mipClampBC=1"
check(!R.prepare(&gow) && file(gowExe).isEmpty && mark(gowExe) == "", "lines in upstream's config field are the game's own")
check(R.reset(&gow)?.id == "god-of-war" && gow.resolution == "1920x1080" && gow.config == nil, "God of War Reset")
check(LibraryPrefs.screen(gowExe) == "1920x1080" && !LibraryPrefs.safeSync(gowExe), "the home screen's forms of the switches too")
var desk = LibraryEntry(title: "Desktop", windowsPath: #"C:\windows\system32\explorer.exe"#)
desk.desktop = true
check(!R.prepare(&desk) && R.recommendation(for: desk) == nil, "the Desktop has none")
let fooExe = #"C:\Games\Foo\foo.exe"#
var foo = LibraryEntry(title: "Foo", windowsPath: fooExe)
GameProfile(windowsPath: fooExe).text = "fence-chain = 6\n"
check(!R.prepare(&foo) && file(fooExe) == "fence-chain = 6\n" && mark(fooExe) == nil, "a game without a list is left alone")
check(R.reset(&foo) == nil && file(fooExe).isEmpty, "its Reset empties its config")
print("PASS: own configs (the file or upstream's field) stay until Reset; Reset writes every switch of the list; no list: Reset empties the config")

// The madeira-bcd home screen: keyed by the program, its own forms of the switches, drive_c looked up.
let drive = URL(fileURLWithPath: ProcessInfo.processInfo.environment["MADEIRA_TEST_DRIVE"]!, isDirectory: true)
let folder = drive.appendingPathComponent("Games/GTA V Enhanced", isDirectory: true)
try! FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
check(FileManager.default.createFile(atPath: folder.appendingPathComponent("gta5_enhanced.EXE").path, contents: Data()), "fixture")
let play = #"C:\Games\GTA V Enhanced\PlayGTAV.exe"#
check(R.recommendation(windowsPath: play, steamAppID: nil)?.id == "gta5e-other", "drive_c is looked up in any case")
check(R.recommendation(windowsPath: #"C:\Games\GTA V\PlayGTAV.exe"#, steamAppID: nil) == nil, "a missing game file")
R.prepare(windowsPath: play, title: "GTA V Enhanced")
check(file(play) == R.gta5EnhancedOther.fileText && LibraryPrefs.nvidia(play) && !LibraryPrefs.safeSync(play)
      && LibraryPrefs.screen(play) == "1280x720", "the home screen's first sight")
LibraryPrefs.setScreen("", for: play)
check(R.reset(windowsPath: play, title: "GTA V Enhanced")?.id == "gta5e-other" && LibraryPrefs.screen(play) == "1280x720", "the home screen's Reset")
var playEntry = LibraryEntry(title: "GTA V Enhanced", windowsPath: play)
check(!R.prepare(&playEntry), "the library's entry for the same program sees it was written")
check(R.prepare(&playEntry, new: true) && playEntry.resolution == "1280x720" && playEntry.fastSync == true,
      "an entry new to the library takes the fields of a list written before")
var page = LibraryEntry(title: "Red Dead Redemption 2", windowsPath: rdr2, steamAppID: 1174180)
page.display = "fill"
check(R.prepare(&page, new: true) && page.display == nil && page.fastSync == true && file(rdr2) == R.rdr2Steam.fileText,
      "a Steam game's first page made before the library stored its entry")
let gotSteam = steam + #"\Ghost of Tsushima DIRECTOR'S CUT"#
let olderGoT = "# an older list\nswap-mb = 2048\n"
GameProfile(windowsPath: gotSteam).text = olderGoT
marks = (UserDefaults.standard.dictionary(forKey: key) as? [String: String]) ?? [:]
marks[gotSteam.lowercased()] = "ghost-of-tsushima#0#" + String(CoverStore.stableHash(olderGoT), radix: 16)
UserDefaults.standard.set(marks, forKey: key)
var gotEntry = LibraryEntry(title: "Ghost of Tsushima", windowsPath: gotSteam, steamAppID: 2215430)
check(R.prepare(&gotEntry, new: true) && file(gotSteam) == R.ghostOfTsushima.fileText && gotEntry.resolution == "1280x720",
      "a new entry whose untouched older list is updated takes the list's fields too")
let ownExe = #"C:\Games\Ghost Own\GhostOfTsushima.exe"#
GameProfile(windowsPath: ownExe).text = "dxil-tess = 1\n"
var own = LibraryEntry(title: "Ghost of Tsushima", windowsPath: ownExe)
check(!R.prepare(&own, new: true) && mark(ownExe) == "" && own.resolution == "1408x648" && !LibraryPrefs.avx(ownExe),
      "a config of its own stays its own for a new entry too")
let got = #"C:\GoT\GhostOfTsushima.exe"#
R.prepare(windowsPath: got, title: "Ghost of Tsushima")
check(LibraryPrefs.avx(got) && LibraryPrefs.nvidia(got) && LibraryPrefs.screen(got) == "1280x720", "GoT on the home screen")
// Lines in the library entry's upstream config field for the same program are the game's own.
let otherGoW = #"C:\Other\GoW.exe"#
LibraryModel.shared.entries = [LibraryEntry(title: "God of War", windowsPath: otherGoW, config: "dxmt = d3d11.mipClampBC=1")]
R.prepare(windowsPath: otherGoW, title: "God of War")
check(file(otherGoW).isEmpty && mark(otherGoW) == "" && LibraryPrefs.screen(otherGoW).isEmpty, "the library's own lines count on the home screen")
let lib2 = #"D:\Library2\steamapps\common\Red Dead Redemption 2"#
GameProfile(windowsPath: lib2).text = "vram-mb = 2304\n"
LibraryModel.shared.entries = [LibraryEntry(title: "Red Dead Redemption 2", windowsPath: lib2, steamAppID: 1174180)]
R.prepare(windowsPath: lib2 + #"\RDR2.exe"#, title: "Red Dead Redemption 2")
check(file(lib2 + #"\RDR2.exe"#).isEmpty && mark(lib2 + #"\RDR2.exe"#) == "",
      "the library Steam game's own config counts for its program on the home screen")
let gowHome = #"C:\Home\GoW.exe"#
LibraryModel.shared.entries = [LibraryEntry(title: "God of War", windowsPath: gowHome, config: "dxmt = d3d11.mipClampBC=1")]
check(R.reset(windowsPath: gowHome, title: "God of War")?.id == "god-of-war" && file(gowHome) == R.godOfWar.fileText
      && LibraryModel.shared.entries[0].resolution == "1920x1080" && LibraryModel.shared.entries[0].config == nil,
      "a home screen Reset reaches the library entry for the same program")
check(LogStore.shared.lines.contains { $0.hasPrefix("[recommended] Red Dead Redemption 2: rdr2-steam v\(R.rdr2Steam.version) written (first sight)") }, "log line")
UserDefaults.standard.removeObject(forKey: key)
print("PASS: the home screen shares the files and records; Safe thread sync and Screen size take the list's values")
'''

with tempfile.TemporaryDirectory(prefix='madeira-recommended-') as directory:
    temporary = Path(directory)
    compiler = shutil.which('swiftc')
    if compiler:
        (temporary / 'Stubs.swift').write_text(stubs)
        # the RDR2 list's config lines (its two comment lines are not config)
        rdr2_config_lines = sum(1 for line in presets['rdr2-steam']['lines']
                                if line.strip() and not line.strip().startswith('#'))
        (temporary / 'main.swift').write_text(main.replace('@RDR2_CONFIG_LINES@', str(rdr2_config_lines)))
        (temporary / 'drive').mkdir()
        (temporary / 'configs').mkdir()
        binary = temporary / 'recommended'
        subprocess.run([compiler, str(app / 'GameRecommendations.swift'), str(app / 'GameRecommendationsApply.swift'),
                        str(temporary / 'Stubs.swift'), str(temporary / 'main.swift'), '-o', str(binary)], check=True)
        env = dict(os.environ, MADEIRA_TEST_DRIVE=str(temporary / 'drive'), MADEIRA_TEST_CONFIGS=str(temporary / 'configs'))
        subprocess.run([str(binary)], check=True, env=env)
    elif args.require_swift:
        raise SystemExit('Swift compiler required for the recommended-settings fixture')
    else:
        print('SKIP: Swift compiler unavailable locally; macOS CI must run this check with --require-swift')
