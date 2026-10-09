//
//  GameRecommendations.swift
//  Madeira
//
//  madeira-bcd: the settings Madeira recommends for the games it has been
//  tested with, shipped in the app (owner's decision, 2026-10-09).
//
//  A recommendation is the game's own config file (GameProfiles.swift,
//  madeira.cfg syntax) plus the switches the game's settings page keeps
//  elsewhere: AVX, Report an NVIDIA GPU, Wine's C++ runtime, the fastsync
//  switches, the FPS limit, the resolution and the scaling. A game that
//  arrives with no config of its own gets its recommendation once (a Steam
//  install, a game added by hand, a game already in the library);
//  "Reset to Recommended" at the bottom of its settings brings it back at
//  any time (GameRecommendationsApply.swift).
//
//  How a game is recognised:
//    - A Steam game by its App ID or by its folder under steamapps\common,
//      which Steam keeps the same from build to build.
//    - Any other game by its program's file name, wherever it was installed.
//    - GTA V Enhanced by PlayGTAV.exe (or GTA5_Enhanced.exe) beside
//      GTA5_Enhanced.exe: inside a Steam library it is the Steam copy,
//      anywhere else another copy, and each has its own list.
//
//  This file is Foundation only: tests/host/check-game-recommendations.py
//  compiles it with a test harness. The lists are the working ones recorded
//  in the private notes (OYUN-AYARLARI); a game that gets a newer working
//  list gets it here too, with a higher `version`.
//

import Foundation

/// The recommended setup of one game. A switch left nil is not touched.
struct GameRecommendation: Equatable {
    /// Stable identity ("rdr2-steam"); the record of what was applied keys on it.
    let id: String
    /// Shown under "Reset to Recommended".
    let title: String
    /// Raised whenever `config` or a switch changes.
    let version: Int
    /// The game's own config file, madeira.cfg syntax.
    let config: String
    var avx: Bool? = nil
    var nvidia: Bool? = nil
    var wineVCRT: Bool? = nil
    /// Game details › Fast synchronization (LibraryEntry.fastSync).
    var fastSync: Bool? = nil
    /// Game details › Fast semaphore waits (LibraryEntry.semaphoreFastPath).
    var semaphoreFastPath: Bool? = nil
    /// LibraryEntry.fpsMode: 1 = 60, 3 = 30, 4 = 40, 0 = display maximum, 2 = uncapped.
    var fpsMode: Int? = nil
    /// "WxH". nil keeps the device's own default, which is what the lists
    /// recorded as 1408x648 were on a 19.5:9 iPhone.
    var resolution: String? = nil
    /// Aspect & scaling (DisplayMode raw value); "fit" is the default.
    var display: String? = nil

    /// The config file's text as GameProfile stores it (one trailing newline).
    var fileText: String { config.hasSuffix("\n") ? config : config + "\n" }

    /// A short list of the switches it sets, for the settings page.
    var switchSummary: String {
        var parts: [String] = []
        if let avx { parts.append("AVX \(avx ? "on" : "off")") }
        if let nvidia { parts.append("NVIDIA \(nvidia ? "on" : "off")") }
        if let wineVCRT { parts.append("Wine's C++ runtime \(wineVCRT ? "on" : "off")") }
        if fastSync == false { parts.append("fast synchronization off") }
        if let semaphoreFastPath { parts.append("fast semaphore waits \(semaphoreFastPath ? "on" : "off")") }
        if let fpsMode {
            let label = [1: "60", 3: "30", 4: "40", 0: "display maximum", 2: "uncapped"][fpsMode] ?? "\(fpsMode)"
            parts.append("FPS limit \(label)")
        }
        if let resolution { parts.append(resolution) }
        return parts.joined(separator: ", ")
    }
}

enum GameRecommendations {
    // MARK: The lists

    /// Red Dead Redemption 2 from Steam, through Madeira Dock (builds 462-463).
    static let rdr2Steam = GameRecommendation(
        id: "rdr2-steam", title: "Red Dead Redemption 2 (Steam)", version: 1,
        config: """
        # Madeira's recommended settings for Red Dead Redemption 2 (Steam).
        # Game details › Reset to Recommended brings them back.
        d3d12-caps-log = 2
        d3d12-shader-pack = 1
        replay-split = 1
        pool-low = 1
        pool-page-fit = 1
        pool-split = 1
        vram-mb = 2304
        swap-mb = 8192
        swap-mode = 2
        pso-warm = 2
        fence-chain = 6
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
        env.MADEIRA_EXECREQ_LEAVE = 1
        env.MADEIRA_PIN_GRAPHICS_DLLS = 1
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
        env.MADEIRA_METAL_HUD_MAIN = 1
        """,
        avx: false, nvidia: false, wineVCRT: false, fastSync: true, semaphoreFastPath: false,
        display: "fit")

    /// GTA V Enhanced from Steam, through Madeira Dock (build 456: 18 minutes
    /// idle and 19 minutes of fast driving without a crash, 33-40 FPS at 40).
    static let gta5EnhancedSteam = GameRecommendation(
        id: "gta5e-steam", title: "GTA V Enhanced (Steam)", version: 1,
        config: """
        # Madeira's recommended settings for GTA V Enhanced (Steam).
        # Game details › Reset to Recommended brings them back.
        d3d12-caps-log = 2
        d3d12-core-dll = 1
        d3d12-msaa8 = 1
        d3d12-tile-based = 0
        d3d12-tiled-resources = 1
        d3d12-typed-uav-load = 1
        pool-low = 1
        pool-page-fit = 1
        pool-split = 1
        vram-mb = 4096
        swap-mb = 3072
        pso-warm = 2
        env.FEX_VECTORTSOENABLED = 0
        env.MADEIRA_CHILD_ARGS = PlayGTAV.exe -nobattleye
        env.MADEIRA_D3D11_SRC = 1
        env.MADEIRA_DOCK_GAME_SCM = 1
        env.MADEIRA_DOCK_KEEP_ALIVE = Launcher.exe;GTA5_Enhanced.exe
        env.MADEIRA_DXGI_SRC = 1
        env.MADEIRA_EXECREQ_LEAVE = 1
        env.MADEIRA_KMT_ADAPTER = 1
        env.MADEIRA_PIN_GRAPHICS_DLLS = 1
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
        env.MADEIRA_PAD_MODE = hid
        env.MADEIRA_METAL_HUD_MAIN = 1
        """,
        avx: false, nvidia: true, wineVCRT: false, fastSync: true, semaphoreFastPath: true, fpsMode: 4,
        display: "fit")

    /// GTA V Enhanced started from its own PlayGTAV.exe outside a Steam
    /// library (build 451); Madeira's own settings only.
    static let gta5EnhancedOther = GameRecommendation(
        id: "gta5e-other", title: "GTA V Enhanced (not Steam)", version: 1,
        config: """
        # Madeira's recommended settings for GTA V Enhanced outside Steam.
        # Game details › Reset to Recommended brings them back.
        vram-mb = 4096
        d3d12-core-dll = 1
        d3d12-typed-uav-load = 1
        d3d12-tiled-resources = 1
        d3d12-tile-based = 0
        d3d12-caps-log = 2
        d3d12-msaa8 = 1
        pool-split = 1
        env.MADEIRA_DXGI_SRC = 1
        env.MADEIRA_KMT_ADAPTER = 1
        env.MADEIRA_PAD_MODE = hid
        env.SteamDeck = 1
        env.MADEIRA_SC_PA_POOLS = 2
        env.MADEIRA_FASTSYNC_SEM = 1
        env.FEX_VECTORTSOENABLED = 0
        env.MADEIRA_DEVICE_STATS = 1
        env.MADEIRA_EC_HOOK_TRACE = 0
        """,
        avx: false, nvidia: true, wineVCRT: false, fastSync: true, resolution: "1280x720", display: "fit")

    /// Ghost of Tsushima (build 336: DualSense input, rumble, triggers and light work).
    static let ghostOfTsushima = GameRecommendation(
        id: "ghost-of-tsushima", title: "Ghost of Tsushima", version: 1,
        config: """
        # Madeira's recommended settings for Ghost of Tsushima.
        # Game details › Reset to Recommended brings them back.
        dxil-tess = 0
        sampler-reduction = 3
        ind-count = 6000
        env.MADEIRA_PAD_MODE = hid
        swap-mb = 3072
        """,
        avx: true, nvidia: true, wineVCRT: false, fastSync: true, semaphoreFastPath: false,
        resolution: "1280x720", display: "fit")

    /// God of War (2018) (build 335: 1080p, DualSense input).
    static let godOfWar = GameRecommendation(
        id: "god-of-war", title: "God of War", version: 1,
        config: """
        # Madeira's recommended settings for God of War.
        # Game details › Reset to Recommended brings them back.
        dxmt = d3d11.mipClampBC=2
        swap-mb = 6144
        swap-min-kb = 1024
        env.MADEIRA_LD_BOUNDS = 0
        env.WINEDEBUG = err+all,err-virtual,fixme-all
        env.MADEIRA_PAD_MODE = hid
        """,
        avx: false, nvidia: false, wineVCRT: false, fastSync: true, semaphoreFastPath: false,
        resolution: "1920x1080", display: "fit")

    static let all = [rdr2Steam, gta5EnhancedSteam, gta5EnhancedOther, ghostOfTsushima, godOfWar]

    // MARK: Recognising a game

    /// Steam App IDs.
    static let rdr2AppID = 1174180, gta5EnhancedAppID = 3240220, ghostAppID = 2215430, godOfWarAppID = 1593500

    /// The recommendation for a library entry, or nil.
    /// - windowsPath: the entry's path; a program ("C:\Games\GoW\GoW.exe") or,
    ///   for a Steam game, its install folder ("C:\...\steamapps\common\Red Dead Redemption 2").
    /// - steamAppID: set for a Steam game.
    /// - fileExists: whether a C:\ path exists (a file beside the program, or
    ///   in a Steam game's folder).
    static func match(windowsPath: String, steamAppID: Int?, fileExists: (String) -> Bool) -> GameRecommendation? {
        let path = windowsPath.replacingOccurrences(of: "/", with: "\\").trimmingSuffix("\\")
        let parts = path.lowercased().split(separator: "\\").map(String.init)
        guard let last = parts.last else { return nil }
        let isProgram = last.hasSuffix(".exe")
        // The folder the program lives in, or the Steam game's install folder.
        let folder = isProgram ? (path.lastIndex(of: "\\").map { String(path[..<$0]) } ?? "") : path
        let steamFolder = steamCommonFolder(parts)
        let inSteam = steamAppID != nil || steamFolder != nil
        let exe = isProgram ? last : nil

        // Steam games: the App ID, or the folder under steamapps\common.
        if steamAppID == rdr2AppID || steamFolder == "red dead redemption 2" { return rdr2Steam }
        if steamAppID == gta5EnhancedAppID || steamFolder == "grand theft auto v enhanced" { return gta5EnhancedSteam }
        // GTA V Enhanced elsewhere: its launcher or game program beside GTA5_Enhanced.exe
        // (the legacy edition has PlayGTAV.exe too, beside GTA5.exe).
        if !inSteam, let exe, exe == "playgtav.exe" || exe == "gta5_enhanced.exe", fileExists(folder + "\\GTA5_Enhanced.exe") {
            return gta5EnhancedOther
        }
        // Other games by their program, wherever they were installed.
        if steamAppID == ghostAppID || exe == "ghostoftsushima.exe" || (inSteam && !isProgram && fileExists(folder + "\\GhostOfTsushima.exe")) {
            return ghostOfTsushima
        }
        if steamAppID == godOfWarAppID || exe == "gow.exe" || (inSteam && !isProgram && fileExists(folder + "\\GoW.exe")) {
            return godOfWar
        }
        return nil
    }

    /// The folder name right under steamapps\common, lowercased, or nil.
    static func steamCommonFolder(_ parts: [String]) -> String? {
        guard parts.count >= 3 else { return nil }
        for i in 0..<(parts.count - 2) where parts[i] == "steamapps" && parts[i + 1] == "common" {
            return parts[i + 2]
        }
        return nil
    }
}

private extension String {
    func trimmingSuffix(_ suffix: String) -> String {
        var s = self
        while s.hasSuffix(suffix) { s.removeLast(suffix.count) }
        return s
    }
}
