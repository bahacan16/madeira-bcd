//
//  LibraryBCD.swift
//  Madeira
//
//  madeira-bcd: this fork's per-game options inside upstream's library.
//
//  Upstream's library (Library.swift) is the default interface; this fork's own
//  home screen (HomeView.swift) stays as a choice in Settings › Interface and in
//  the developer interface. A game's madeira-bcd options are keyed by its
//  Windows path in both, so a setting made in one interface applies in the
//  other: the switches in LibraryPrefs (AVX, Wine's C++ runtime, NVIDIA) and
//  the game's own config file (GameProfiles.swift: MetalFX, frame generation,
//  D3D12 switches, any madeira.cfg key).
//

import SwiftUI
import UIKit

extension Notification.Name {
    /// Posted by the developer interface: show madeira-bcd's home screen (RootView).
    static let madeiraShowBCDHome = Notification.Name("madeiraShowBCDHome")
}

/// What a launch exports on top of the exe, its arguments and its screen.
enum BCDLaunch {
    /// The switches both interfaces share. Everything is set or unset, so a
    /// previous launch's choice never leaks into this one.
    static func applyExtras(avx: Bool, wineVCRT: Bool, nvidia: Bool, profile: GameProfile?) {
        if avx { setenv("MADEIRA_FEX_AVX", "1", 1) } else { unsetenv("MADEIRA_FEX_AVX") }
        if wineVCRT { setenv("MADEIRA_WINE_VCRT", "1", 1) } else { unsetenv("MADEIRA_WINE_VCRT") }
        var dxmtExtra: [String] = []
        if nvidia {
            setenv("DXMT_ENABLE_NVEXT", "1", 1)
            // The same GeForce RTX 3060 (10DE:2544) win32u registers as the
            // display adapter (sysparams_ios.c, ios_virtual_gpu_ids); merged
            // into DXMT_CONFIG by ContentView.
            dxmtExtra.append("dxgi.customDeviceId=2544")
            // DXGI's output carries user32's monitor handle and user32's mode
            // list, so a game can find its monitor on the adapter. Default-on
            // in this fork until the build 222 switch to upstream, where both
            // became 32-bit-only switches; Ghost of Tsushima has said "No
            // installed graphics card ... monitor connected to it" on every
            // launch since (logs 2026-10-01 16:15 / 16:16 / 17:04; 2026-09-28
            // ran with both). A game file's env.NAME = 0 still wins.
            setenv("DXMT_WSI_MONITOR_IDENTITY", "1", 1)
            setenv("DXMT_WSI_MODE_TABLE", "1", 1)
        } else {
            unsetenv("DXMT_ENABLE_NVEXT")
            unsetenv("DXMT_WSI_MONITOR_IDENTITY")
            unsetenv("DXMT_WSI_MODE_TABLE")
        }
        // The game's own settings (GameProfiles.swift).
        if let p = profile, p.hasSettings, let u = p.url {
            setenv("MADEIRA_CFG_GAME", u.path, 1)
        } else {
            unsetenv("MADEIRA_CFG_GAME")
        }
        // MetalFX: the D3D12 runtime reads metalfx-upscale from that file; a
        // D3D11 game gets DXMT's own MetalFX swapchain at the same factor.
        if let f = profile?.metalFXFactor {
            setenv("DXMT_METALFX_SPATIAL_SWAPCHAIN", "1", 1)
            dxmtExtra.append("d3d11.metalSpatialUpscaleFactor=\(f)")
        } else {
            unsetenv("DXMT_METALFX_SPATIAL_SWAPCHAIN")
        }
        if dxmtExtra.isEmpty { unsetenv("MADEIRA_DXMT_EXTRA") }
        else { setenv("MADEIRA_DXMT_EXTRA", dxmtExtra.joined(separator: ";"), 1)   /* DXMT splits on ";" */ }
    }

    /// A start from upstream's library: this game's madeira-bcd options and
    /// its own session log. The FPS limit is the entry's (upstream's FPS
    /// picker, which offers 40 FPS too).
    /// `sessionLog: false` for a Steam game started through Madeira Dock: the
    /// native side names that log after the program Valve's client starts
    /// (process_ios.c, madeira_steam_session_log).
    static func applyLibrary(_ entry: LibraryEntry, sessionLog: Bool = true) {
        if entry.desktop == true {
            applyExtras(avx: false, wineVCRT: false, nvidia: false, profile: nil)
            LogStore.shared.startSessionLog(program: "explorer.exe")
            return
        }
        let path = entry.windowsPath
        let profile = GameProfile(windowsPath: path)
        applyExtras(avx: LibraryPrefs.avx(path), wineVCRT: LibraryPrefs.wineVCRT(path),
                    nvidia: LibraryPrefs.nvidia(path), profile: profile)
        LibraryPrefs.markPlayed(entry.title)
        let program = path.split(separator: "\\").last.map(String.init) ?? path
        LogStore.shared.log("[bcd] library launch \(program): avx=\(LibraryPrefs.avx(path) ? 1 : 0) "
                            + "wine-vcrt=\(LibraryPrefs.wineVCRT(path) ? 1 : 0) nvidia=\(LibraryPrefs.nvidia(path) ? 1 : 0) "
                            + "game-config=\(profile.hasSettings ? 1 : 0) metalfx=\(profile.metalFXFactor.map { String($0) } ?? "off")")
        if sessionLog { LogStore.shared.startSessionLog(program: program) }
    }

    /// The size that MetalFX 1.5x brings back to this screen's shape at 720
    /// lines ("WxH"), as madeira-bcd's home screen offers it (fill-mfx15).
    static var metalFX15Resolution: String {
        let s = LibraryPrefs.screenPixels("fill-mfx15")
        return "\(s.0)x\(s.1)"
    }
}

/// The madeira-bcd sections of upstream's game details page (LibraryDetail).
/// Every change is written at once, so Done, Play and a swipe away all keep it.
struct BCDGameSections: View {
    let windowsPath: String
    /// Bumped by LibraryDetail when it changes this game's config file itself.
    var refresh: Int = 0

    @State private var avx = false
    @State private var wineVCRT = false
    @State private var nvidia = false
    @State private var metalFX = ""
    @State private var frameGen = ""
    @State private var tess = ""
    @State private var submit = ""
    @State private var gpuSync = ""
    @State private var padMode = ""
    @State private var loaded = true
    @State private var copiedLink = false

    init(windowsPath: String, refresh: Int = 0) {
        self.windowsPath = windowsPath
        self.refresh = refresh
        let p = GameProfile(windowsPath: windowsPath)
        _avx = State(initialValue: LibraryPrefs.avx(windowsPath))
        _wineVCRT = State(initialValue: LibraryPrefs.wineVCRT(windowsPath))
        _nvidia = State(initialValue: LibraryPrefs.nvidia(windowsPath))
        _metalFX = State(initialValue: p.get("metalfx-upscale") ?? "")
        _frameGen = State(initialValue: p.get("env.MADEIRA_FRAMEGEN") ?? "")
        _tess = State(initialValue: p.get("dxil-tess-max-factor") ?? "")
        _submit = State(initialValue: p.get("async-submit") ?? "")
        _gpuSync = State(initialValue: p.get("fence-chain") ?? "")
        _padMode = State(initialValue: p.get(GamepadInput.padModeKey) ?? "")
    }

    private var profile: GameProfile { GameProfile(windowsPath: windowsPath) }

    private func load() {
        loaded = false
        avx = LibraryPrefs.avx(windowsPath)
        wineVCRT = LibraryPrefs.wineVCRT(windowsPath)
        nvidia = LibraryPrefs.nvidia(windowsPath)
        let p = profile
        metalFX = p.get("metalfx-upscale") ?? ""
        frameGen = p.get("env.MADEIRA_FRAMEGEN") ?? ""
        tess = p.get("dxil-tess-max-factor") ?? ""
        submit = p.get("async-submit") ?? ""
        gpuSync = p.get("fence-chain") ?? ""
        padMode = p.get(GamepadInput.padModeKey) ?? ""
        // The onChange handlers of this pass must not write the values back.
        DispatchQueue.main.async { loaded = true }
    }

    /// Writes one key of the game's file when the value really changed.
    private func store(_ key: String, _ value: String) {
        guard loaded else { return }
        let p = profile
        if (p.get(key) ?? "") != value { p.set(key, value) }
    }

    /// A picker over (value, label) pairs that also shows a value typed into the raw file.
    private func choicePicker(_ title: String, _ selection: Binding<String>, _ choices: [(String, String)]) -> some View {
        let known = choices.contains { $0.0 == selection.wrappedValue }
        let all: [(String, String)] = choices + (known ? [] : [(selection.wrappedValue, selection.wrappedValue)])
        return Picker(title, selection: selection) {
            ForEach(Array(all.enumerated()), id: \.offset) { item in
                Text(item.element.1).tag(item.element.0)
            }
        }
    }

    var body: some View {
        Group { sections }
            .onChange(of: refresh) { _, _ in load() }
    }

    @ViewBuilder private var sections: some View {
        Section {
            Toggle("AVX / AVX2", isOn: $avx)
                .onChange(of: avx) { _, on in if loaded { LibraryPrefs.setAVX(on, for: windowsPath) } }
            Toggle("Wine's C++ runtime", isOn: $wineVCRT)
                .onChange(of: wineVCRT) { _, on in if loaded { LibraryPrefs.setWineVCRT(on, for: windowsPath) } }
            Toggle("Report an NVIDIA GPU", isOn: $nvidia)
                .onChange(of: nvidia) { _, on in if loaded { LibraryPrefs.setNvidia(on, for: windowsPath) } }
        } header: {
            Text("madeira-bcd: launch options")
        } footer: {
            Text("Turn on AVX when a game quits at start with \"illegal instruction\" (c000001d): it was built for AVX "
                 + "CPUs; emulated AVX is slower. Wine's C++ runtime replaces Microsoft's concrt140/msvcp140_* for a "
                 + "game that crashes right after loading them. Report an NVIDIA GPU is for games that stop with "
                 + "\"no graphics card\" or \"failed to get GPU driver info\".")
        }
        Section {
            choicePicker("MetalFX upscaling", $metalFX, GameProfile.metalFXChoices)
                .onChange(of: metalFX) { _, v in store("metalfx-upscale", v) }
            choicePicker("Frame generation", $frameGen, GameProfile.frameGenChoices)
                .onChange(of: frameGen) { _, v in store("env.MADEIRA_FRAMEGEN", v) }
            choicePicker("Tessellation detail (D3D12)", $tess, GameProfile.tessChoices)
                .onChange(of: tess) { _, v in store("dxil-tess-max-factor", v) }
            choicePicker("D3D12 command encoding", $submit, GameProfile.submitChoices)
                .onChange(of: submit) { _, v in store("async-submit", v) }
            choicePicker("GPU sync (D3D12)", $gpuSync, GameProfile.gpuSyncChoices)
                .onChange(of: gpuSync) { _, v in store("fence-chain", v) }
            NavigationLink {
                GameConfigEditor(profile: profile, onSave: { load() })
            } label: {
                Label("Advanced: this game's config", systemImage: "doc.text")
            }
        } header: {
            Text("madeira-bcd: graphics & performance")
        } footer: {
            Text("MetalFX upscaling renders at the resolution above and scales the picture up 1.5× or 2× with "
                 + "Apple's scaler; the \"MetalFX 1.5×\" resolution is this screen's shape at 480 lines, which "
                 + "1.5× brings to 720. Frame generation (experimental) shows a MetalFX-interpolated frame between "
                 + "every two game frames; FPS limits do not apply while it is on. The advanced file takes any "
                 + "madeira.cfg key or env.NAME line for this game only.")
        }
        Section {
            choicePicker("Controller API", $padMode, GameProfile.padModeChoices)
                .onChange(of: padMode) { _, v in store(GamepadInput.padModeKey, v) }
        } header: {
            Text("madeira-bcd: controller")
        } footer: {
            Text("XInput shows every controller as an Xbox pad, which almost every game understands. "
                 + "DirectInput / HID shows player 1 as what it is: a DualSense to Sony's PC ports (God of War "
                 + "and others show PlayStation buttons), a HID gamepad to DirectInput games; it is then not an "
                 + "XInput pad. Read when the game starts; the in-game menu changes it too.")
        }
        Section {
            Button {
                UIPasteboard.general.string = ShortcutRouter.link(for: windowsPath)
                copiedLink = true
            } label: {
                Label(copiedLink ? "Link copied" : "Copy Home Screen shortcut link",
                      systemImage: copiedLink ? "checkmark" : "link")
            }
        } header: {
            Text("madeira-bcd: Home Screen")
        } footer: {
            Text("In the Shortcuts app: new shortcut, Open URLs, paste the link, then Share > Add to Home Screen.")
        }
    }
}

/// Settings › Interface: which screen Madeira starts with.
struct BCDInterfacePicker: View {
    @State private var choice = FrontendChoice.preferred
    @State private var restartNotice = false

    var body: some View {
        Picker("Start with", selection: Binding(get: { choice }, set: { value in
            choice = value
            FrontendChoice.choose(value)
            restartNotice = true
        })) {
            Text("Library").tag("new")
            Text("madeira-bcd home").tag("bcd")
            Text("Developer interface").tag("old")
        }
        .alert("Restart Madeira", isPresented: $restartNotice) {
            Button("OK", role: .cancel) {}
        } message: {
            Text("Close Madeira from the app switcher and open it again to switch interfaces.")
        }
    }
}

/// Adds every game madeira-bcd's home screen finds in drive_c (its main exe)
/// to upstream's library, once.
enum BCDLibraryImport {
    static func run(done: @escaping (Int) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let games = LibraryGame.group(GameLibrary.scan())
            var found: [LibraryEntry] = []
            for game in games {
                let exe = game.primary
                guard var entry = try? LibraryModel.inspect(exe.url) else { continue }
                entry.title = game.title
                found.append(entry)
            }
            DispatchQueue.main.async {
                let model = LibraryModel.shared
                let known = Set(model.entries.map { $0.relativePath.lowercased() })
                var added = 0
                for entry in found where !known.contains(entry.relativePath.lowercased()) {
                    model.save(entry)
                    added += 1
                }
                LogStore.shared.log("[bcd] library import: \(added) added, \(found.count - added) already there")
                done(added)
            }
        }
    }
}
