//
//  HomeView.swift
//  Madeira
//
//  The launcher: a game library in the style of GameHub and Winlator, in front
//  of the session view that ContentView has always been.
//
//  Two facts about this app shape everything here:
//
//  * One Windows session per app launch. The JIT pool can be placed only once
//    (see ContentView.wineSequenceStarted), so starting a game is a one-way
//    step: the root swaps to the session view and stays there. There is no
//    "back to library" because there is nothing to go back to that could start
//    a second game.
//  * The game surface is a process-lifetime Metal view owned by ContentView.
//    Rather than re-home it, this screen sits in FRONT of ContentView and hands
//    it a LaunchRequest; ContentView applies it and starts Wine exactly as its
//    own buttons do. Every launch path in the old panel still exists, reachable
//    as "Developer tools".
//
//  Targets iOS 26+, so it uses Liquid Glass for the floating controls (chips,
//  badges, play buttons) and leaves the box art and cards opaque -- glass is
//  for things that sit on top of content, and the covers are the content.
//

import SwiftUI
import PhotosUI
import UIKit

// MARK: - Launch request

/// Everything ContentView's launch buttons put in the environment before
/// runWineFullSequence(), as a value the library can build and hand over.
struct LaunchRequest {
    let title: String
    let exe: String
    let args: String?
    /// Wine virtual desktop size, or nil to run the program directly.
    let desktop: (w: Int, h: Int)?
    /// Tell FEX to expose AVX/AVX2 (tools/patch-fex-ios-avx.py). Off unless
    /// the game's settings turn it on.
    var avx = false
    /// Keep Wine's VC++ runtime instead of overlaying Microsoft's x86_64 one
    /// (WineProcessBridge.m, MADEIRA_WINE_VCRT).
    var wineVCRT = false

    func apply() {
        ExperimentalSettings.exportToEnvironment()
        setenv("MADEIRA_EXE", exe, 1)
        if let a = args, !a.isEmpty { setenv("MADEIRA_ARGS", a, 1) } else { unsetenv("MADEIRA_ARGS") }
        if let d = desktop {
            setenv("MADEIRA_DESKTOP", "1", 1)
            setenv("MADEIRA_SCREEN_W", String(d.w), 1)
            setenv("MADEIRA_SCREEN_H", String(d.h), 1)
        } else {
            unsetenv("MADEIRA_DESKTOP")
        }
        if avx { setenv("MADEIRA_FEX_AVX", "1", 1) } else { unsetenv("MADEIRA_FEX_AVX") }
        if wineVCRT { setenv("MADEIRA_WINE_VCRT", "1", 1) } else { unsetenv("MADEIRA_WINE_VCRT") }
        LogStore.shared.startSessionLog(program: programName)
    }

    /// The exe the session is about: the program explorer is asked to start
    /// in a desktop launch ("C:\\Crysis\\Bin64\\Crysis64.exe" in its
    /// arguments), else the exe itself.
    private var programName: String {
        if exe.lowercased() == "explorer.exe", let a = args,
           let open = a.firstIndex(of: "\""),
           let close = a[a.index(after: open)...].firstIndex(of: "\"") {
            let path = a[a.index(after: open)..<close]
            if let name = path.split(separator: "\\").last, !name.isEmpty { return String(name) }
        }
        return exe.split(separator: "\\").last.map(String.init) ?? exe
    }

    /// Identical to the "Wine Virtual Desktop" button: explorer as the shell,
    /// services.exe as its child so the SCM and RpcSs come up behind it.
    static func windowsDesktop(_ size: (Int, Int)) -> LaunchRequest {
        LaunchRequest(title: "Windows Desktop", exe: "explorer.exe",
                      args: "/desktop=shell,\(size.0)x\(size.1) C:\\windows\\system32\\services.exe",
                      desktop: (w: size.0, h: size.1))
    }

    /// Identical to the "x64 DX11 cube" button. The exe ships in the bundle.
    static let x64Cube = LaunchRequest(title: "x64 DX11 Cube", exe: "cube-x64.exe", args: nil, desktop: nil)

    /// Upstream's D3D12 smoke test (the developer panel's "D3D12 cube"): an
    /// x86-64 program drawing through the native D3D12 runtime, its shaders
    /// converted at pipeline creation by the Metal Shader Converter.
    static let x64D3D12Cube = LaunchRequest(title: "x64 DX12 Cube", exe: "d3d12-cube-x64.exe", args: nil, desktop: nil)
}

// MARK: - Experimental settings

/// Opt-in switches that change how the runtime behaves. They live in
/// Documents/madeira.cfg, the one file the native side reads (ml1095), so a
/// hand edit and the switch in Settings are the same setting.
enum ExperimentalSettings {
    /// Upstream's file-backed guest data tier (virtual_ios.c ml1077): large
    /// guest commits are mapped from a sparse file iOS may page out, which
    /// jetsam does not count. The cap is the size RDR2 was run with.
    static let storageBackedMemoryMB = 3072

    static var storageBackedMemory: Bool {
        get { (Int(MadeiraConfig.get("swap-mb") ?? "") ?? 0) >= 64 }
        set { MadeiraConfig.set("swap-mb", newValue ? String(storageBackedMemoryMB) : nil) }
    }

    /// Must run before runWineFullSequence. Everything here is read from
    /// madeira.cfg by the native side, so there is nothing to export today;
    /// kept as the one place a future environment-only switch would go.
    static func exportToEnvironment() {}
}

// MARK: - Root

struct RootView: View {
    private enum Screen {
        case home
        case session(LaunchRequest?)
    }

    @State private var screen: Screen = .home

    var body: some View {
        switch screen {
        case .home:
            HomeView(onLaunch: { screen = .session($0) },
                     onDeveloper: {
                         ExperimentalSettings.exportToEnvironment()
                         screen = .session(nil)
                     })
        case .session(let request):
            ContentView(pendingLaunch: request)
        }
    }
}

// MARK: - Library model

/// One title as a person thinks of it: a folder under Program Files (or C:\)
/// with every launchable exe inside it, biggest first.
struct LibraryGame: Identifiable {
    let title: String
    let executables: [GuestExecutable]
    /// Set when one title is shown as several cards (one per exe).
    var key: String? = nil

    var id: String { key ?? title }

    /// The exe the card launches: the one chosen in settings, else the biggest.
    /// The biggest is a good default because a game's shipping binary dwarfs
    /// the launchers and crash reporters next to it. 32-bit exes sort after
    /// every 64-bit one (see group), so a folder holding both a 32-bit setup
    /// or launcher and the 64-bit game defaults to the one that can run.
    var primary: GuestExecutable {
        if let chosen = LibraryPrefs.primaryPath(for: title),
           let exe = executables.first(where: { $0.windowsPath == chosen }) {
            return exe
        }
        return executables[0]
    }

    static func group(_ found: [GuestExecutable]) -> [LibraryGame] {
        var order: [String] = []
        var byTitle: [String: [GuestExecutable]] = [:]
        for exe in found {
            if byTitle[exe.title] == nil { order.append(exe.title) }
            byTitle[exe.title, default: []].append(exe)
        }
        return order.map { title in
            // Called off the main thread (rescan), so the PE header reads are fine here.
            let is32 = Dictionary((byTitle[title] ?? []).map { ($0.windowsPath, PEInfo.archLabel($0.url) == "x86") },
                                  uniquingKeysWith: { a, _ in a })
            let exes = (byTitle[title] ?? []).sorted { a, b in
                let a32 = is32[a.windowsPath] ?? false, b32 = is32[b.windowsPath] ?? false
                return a32 != b32 ? !a32 : a.sizeBytes > b.sizeBytes
            }
            return LibraryGame(title: title, executables: exes)
        }
    }
}

/// Per-title choices that are not command-line arguments (those stay in
/// GameArguments, keyed by Windows path, exactly as the old library kept them).
enum LibraryPrefs {
    private static let primaryKey = "madeira.library.primaryExe"
    private static let playedKey = "madeira.library.lastPlayed"
    private static let desktopKey = "madeira.library.inDesktop"
    private static let avxKey = "madeira.library.avx"
    private static let vcrtKey = "madeira.library.wineVCRT"

    private static func dict<T>(_ key: String) -> [String: T] {
        (UserDefaults.standard.dictionary(forKey: key) as? [String: T]) ?? [:]
    }

    private static func store<T>(_ value: T?, _ key: String, _ field: String) {
        var all: [String: T] = dict(key)
        all[field] = value
        UserDefaults.standard.set(all, forKey: key)
    }

    static func primaryPath(for title: String) -> String? { (dict(primaryKey) as [String: String])[title] }
    static func setPrimaryPath(_ path: String, for title: String) { store(path, primaryKey, title) }

    static func lastPlayed(_ title: String) -> Date? {
        guard let t = (dict(playedKey) as [String: Double])[title] else { return nil }
        return Date(timeIntervalSince1970: t)
    }
    static func markPlayed(_ title: String) { store(Date().timeIntervalSince1970, playedKey, title) }

    static func inDesktop(_ windowsPath: String) -> Bool { (dict(desktopKey) as [String: Bool])[windowsPath] ?? false }
    static func setInDesktop(_ on: Bool, for windowsPath: String) { store(on ? true : nil, desktopKey, windowsPath) }

    static func avx(_ windowsPath: String) -> Bool { (dict(avxKey) as [String: Bool])[windowsPath] ?? false }
    static func setAVX(_ on: Bool, for windowsPath: String) { store(on ? true : nil, avxKey, windowsPath) }

    static func wineVCRT(_ windowsPath: String) -> Bool { (dict(vcrtKey) as [String: Bool])[windowsPath] ?? false }
    static func setWineVCRT(_ on: Bool, for windowsPath: String) { store(on ? true : nil, vcrtKey, windowsPath) }
}

/// Reads the PE header's Machine field. Two small reads per file, off the main
/// thread, and it tells a person whether a card is x86-64 (emulated) or native.
enum PEInfo {
    static func archLabel(_ url: URL) -> String? {
        guard let machine = machine(url) else { return nil }
        switch machine {
        case 0x8664: return "x64"
        case 0xAA64: return "ARM64"
        case 0xA641: return "ARM64EC"
        case 0x014C: return "x86"
        default: return nil
        }
    }

    private static func machine(_ url: URL) -> UInt16? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        guard let dos = try? handle.read(upToCount: 64), dos.count == 64 else { return nil }
        let bytes = [UInt8](dos)
        guard bytes[0] == 0x4D, bytes[1] == 0x5A else { return nil }
        var lfanew: UInt64 = 0
        for i in 0..<4 { lfanew |= UInt64(bytes[0x3C + i]) << (8 * UInt64(i)) }
        guard (try? handle.seek(toOffset: lfanew)) != nil,
              let header = try? handle.read(upToCount: 6), header.count == 6 else { return nil }
        let h = [UInt8](header)
        guard h[0] == 0x50, h[1] == 0x45, h[2] == 0, h[3] == 0 else { return nil }
        return UInt16(h[4]) | (UInt16(h[5]) << 8)
    }
}

/// User-chosen cover art. Kept in Application Support, not Documents, so it
/// does not clutter the folder the Files app shows as the Windows drive.
enum CoverStore {
    private static var directory: URL? {
        guard let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            return nil
        }
        let dir = base.appendingPathComponent("Covers", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    /// FNV-1a over the title: stable across launches, unlike String.hashValue.
    static func stableHash(_ s: String) -> UInt64 {
        var h: UInt64 = 0xcbf29ce484222325
        for b in s.utf8 { h = (h ^ UInt64(b)) &* 0x100000001b3 }
        return h
    }

    private static func url(for title: String) -> URL? {
        directory?.appendingPathComponent(String(format: "%016llx.jpg", stableHash(title)))
    }

    static func image(for title: String) -> UIImage? {
        guard let u = url(for: title) else { return nil }
        return UIImage(contentsOfFile: u.path)
    }

    static func hasCover(_ title: String) -> Bool {
        guard let u = url(for: title) else { return false }
        return FileManager.default.fileExists(atPath: u.path)
    }

    /// Downscaled on save: a 12-megapixel photo is 30 MB decoded, and a grid
    /// of them would cost more memory than the game it is a picture of.
    static func save(_ data: Data, for title: String) {
        guard let u = url(for: title), let img = UIImage(data: data) else { return }
        let maxSide: CGFloat = 900
        let scale = min(1, maxSide / max(img.size.width, img.size.height))
        let size = CGSize(width: img.size.width * scale, height: img.size.height * scale)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let scaled = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            img.draw(in: CGRect(origin: .zero, size: size))
        }
        try? scaled.jpegData(compressionQuality: 0.85)?.write(to: u, options: .atomic)
    }

    static func remove(_ title: String) {
        guard let u = url(for: title) else { return }
        try? FileManager.default.removeItem(at: u)
    }
}

/// Opens the Files app on the prefix's C:\, which is where games have to go.
enum FilesApp {
    static func openDriveC() {
        guard let c = GameLibrary.driveC,
              let path = c.path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "shareddocuments://" + path) else { return }
        UIApplication.shared.open(url)
    }
}

// MARK: - Home

struct HomeView: View {
    let onLaunch: (LaunchRequest) -> Void
    let onDeveloper: () -> Void

    @Environment(\.scenePhase) private var scenePhase
    @ObservedObject private var controllers = GameControllerManager.shared
    @AppStorage("wine_desktop_res") private var desktopRes = "960x540"

    @State private var games: [LibraryGame] = []
    @State private var archs: [String: String] = [:]
    @State private var scanning = false
    @State private var scannedOnce = false
    @State private var search = ""
    @State private var jitOn = jit_check_debugged()
    @State private var enablingJIT = false
    @State private var pendingAfterJIT: LaunchRequest?
    @State private var showJITAlert = false
    @State private var blocked32: LibraryGame?
    @State private var editing: LibraryGame?
    @State private var showSettings = false
    @State private var coverTick = 0
    @AppStorage("madeira.library.everyExe") private var everyExe = false

    private let columns = [GridItem(.adaptive(minimum: 148), spacing: 14)]

    /// One card per title, or one per exe when every exe is asked for.
    private var displayed: [LibraryGame] {
        guard everyExe else { return games }
        return games.flatMap { g in
            g.executables.map { LibraryGame(title: g.title, executables: [$0], key: $0.windowsPath) }
        }
    }

    private var filtered: [LibraryGame] {
        let q = search.trimmingCharacters(in: .whitespaces)
        return q.isEmpty ? displayed : displayed.filter { game in
            game.title.localizedCaseInsensitiveContains(q)
                || game.executables.contains { exe in exe.windowsPath.localizedCaseInsensitiveContains(q) }
        }
    }

    private var recent: LibraryGame? {
        games.compactMap { g in LibraryPrefs.lastPlayed(g.title).map { (g, $0) } }
            .max { $0.1 < $1.1 }?.0
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    statusRow
                    if !jitOn { jitBanner }
                    if search.isEmpty, let g = recent { continueCard(g) }
                    desktopCard
                    librarySection
                    demosSection
                    footnote
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 32)
            }
            .background(HomeBackground().ignoresSafeArea())
            .navigationTitle("Madeira")
            .searchable(text: $search, prompt: "Search games")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button { rescan() } label: {
                        if scanning { ProgressView() } else { Image(systemName: "arrow.clockwise") }
                    }
                    .disabled(scanning)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { showSettings = true } label: { Image(systemName: "gearshape") }
                }
            }
            .onAppear { if !scannedOnce { rescan() } }
            .onChange(of: scenePhase) { _, phase in
                // StikDebug enables JIT from outside the app, so re-read the
                // flag whenever we come back to the foreground.
                if phase == .active { jitOn = jit_check_debugged() }
            }
            .sheet(item: $editing, onDismiss: { coverTick += 1 }) { game in
                GameSettingsSheet(game: game, archs: archs,
                                  onPlay: { launch(game) },
                                  onCoverChanged: { coverTick += 1 })
            }
            .sheet(isPresented: $showSettings) {
                AppSettingsSheet(onDeveloper: onDeveloper)
            }
            .alert("32-bit program", isPresented: Binding(get: { blocked32 != nil },
                                                          set: { if !$0 { blocked32 = nil } })) {
                if let game = blocked32, game.executables.count > 1 {
                    Button("Choose another exe") { editing = game; blocked32 = nil }
                }
                Button("OK", role: .cancel) { blocked32 = nil }
            } message: {
                Text((blocked32?.primary.fileName ?? "This exe")
                     + " is a 32-bit (x86) Windows program. Madeira runs 64-bit programs only: "
                     + "iOS reserves the low 4 GB of every app's address space, and 32-bit Windows "
                     + "code has to live below 2 GB. Pick the game's 64-bit exe (often in a Bin64 "
                     + "or x64 folder). Installers are usually 32-bit, so install on a PC and copy "
                     + "the installed folder instead.")
            }
            .alert("JIT is not enabled", isPresented: $showJITAlert) {
                Button("Enable JIT") { enableJIT() }
                Button("Cancel", role: .cancel) { pendingAfterJIT = nil }
            } message: {
                Text("Windows programs need JIT. Madeira will ask StikDebug to enable it, then start "
                     + (pendingAfterJIT?.title ?? "the program") + ".")
            }
        }
        .preferredColorScheme(.dark)
    }

    // MARK: Sections

    private var statusRow: some View {
        GlassEffectContainer(spacing: 8) {
        HStack(spacing: 8) {
            StatusChip(icon: "bolt.fill", text: jitOn ? "JIT on" : "JIT off", tint: jitOn ? .green : .orange)
            if controllers.connectedControllersCount > 0 {
                StatusChip(icon: "gamecontroller.fill",
                           text: controllers.activeControllerName ?? "Controller", tint: .green)
            }
            StatusChip(icon: "square.stack.3d.up.fill", text: "\(games.count) games", tint: .blue)
            Spacer()
        }
        }
        .padding(.top, 4)
    }

    private var jitBanner: some View {
        Button { enableJIT() } label: {
            HStack(spacing: 12) {
                Image(systemName: enablingJIT ? "hourglass" : "bolt.slash.fill")
                    .font(.title2)
                VStack(alignment: .leading, spacing: 2) {
                    Text(enablingJIT ? "Waiting for StikDebug…" : "Enable JIT to play")
                        .font(.headline)
                    Text("Tap to hand off to StikDebug. Come back here when it is done.")
                        .font(.caption).foregroundStyle(.white.opacity(0.8))
                }
                Spacer()
                Image(systemName: "chevron.right")
            }
            .foregroundStyle(.white)
            .padding(14)
            .background(LinearGradient(colors: [.orange, .red.opacity(0.8)],
                                       startPoint: .leading, endPoint: .trailing),
                        in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(enablingJIT)
    }

    private func continueCard(_ game: LibraryGame) -> some View {
        Button { launch(game) } label: {
            ZStack(alignment: .bottomLeading) {
                CoverArt(title: game.title, tick: coverTick)
                    .frame(height: 150)
                LinearGradient(colors: [.clear, .black.opacity(0.85)], startPoint: .top, endPoint: .bottom)
                    .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                HStack(alignment: .bottom) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("CONTINUE").font(.caption2.weight(.heavy)).foregroundStyle(.white.opacity(0.7))
                        Text(game.title).font(.title3.weight(.bold)).foregroundStyle(.white).lineLimit(1)
                    }
                    Spacer()
                    PlayGlyph()
                }
                .padding(14)
            }
        }
        .buttonStyle(.plain)
    }

    private var desktopCard: some View {
        let size = ContentView.desktopSize(desktopRes)
        return HStack(spacing: 12) {
            Button { start(.windowsDesktop(size)) } label: {
                HStack(spacing: 14) {
                    Image(systemName: "macwindow.on.rectangle")
                        .font(.system(size: 28, weight: .semibold))
                        .frame(width: 52, height: 52)
                        .background(.white.opacity(0.15), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Windows Desktop").font(.headline)
                        Text("Explorer · \(size.0)×\(size.1)").font(.caption).foregroundStyle(.white.opacity(0.75))
                    }
                    Spacer()
                    PlayGlyph()
                }
                .foregroundStyle(.white)
            }
            .buttonStyle(.plain)

            Menu {
                Picker("Desktop size", selection: $desktopRes) {
                    ForEach(ContentView.desktopResolutions, id: \.self) { Text($0).tag($0) }
                }
            } label: {
                Image(systemName: "aspectratio")
                    .font(.title3)
                    .foregroundStyle(.white)
                    .frame(width: 40, height: 40)
                    .glassEffect(.regular.interactive(), in: Circle())
            }
        }
        .padding(14)
        .background(LinearGradient(colors: [Color(red: 0.16, green: 0.34, blue: 0.78),
                                            Color(red: 0.38, green: 0.20, blue: 0.70)],
                                   startPoint: .topLeading, endPoint: .bottomTrailing),
                    in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private var librarySection: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Library", detail: scanning ? "Scanning C:\\…"
                          : scannedOnce ? "\(games.reduce(0) { $0 + $1.executables.count }) exe" : nil)
            Picker("Show", selection: $everyExe) {
                Text("Games").tag(false)
                Text("Every .exe").tag(true)
            }
            .pickerStyle(.segmented)
            if filtered.isEmpty {
                emptyLibrary
            } else {
                LazyVGrid(columns: columns, spacing: 16) {
                    ForEach(filtered) { game in
                        Button { launch(game) } label: {
                            GameCard(game: game, arch: archs[game.primary.windowsPath], tick: coverTick)
                        }
                        .buttonStyle(.plain)
                        .contextMenu {
                            Button { launch(game) } label: { Label("Play", systemImage: "play.fill") }
                            Button { editing = game } label: { Label("Settings", systemImage: "slider.horizontal.3") }
                        }
                    }
                }
            }
        }
    }

    private var emptyLibrary: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !search.isEmpty {
                Text("No game matches “\(search)”.").foregroundStyle(.secondary)
            } else if scanning || !scannedOnce {
                HStack(spacing: 8) { ProgressView(); Text("Looking through C:\\…").foregroundStyle(.secondary) }
            } else {
                Label("No games yet", systemImage: "tray").font(.headline)
                Text("Copy a game's folder into C:\\ — in the Files app that is On My iPhone › Madeira › wine › drive_c. "
                     + "A folder under Program Files works too. Then pull the list to rescan.")
                    .font(.callout).foregroundStyle(.secondary)
                Button { FilesApp.openDriveC() } label: {
                    Label("Open C:\\ in Files", systemImage: "folder")
                }
                .buttonStyle(.glassProminent)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var demosSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Test programs", detail: nil)
            demoCard(.x64Cube, icon: "cube.transparent.fill", tint: .purple,
                     detail: "x86-64 → FEX → D3D11 → Metal. The smoke test.")
            demoCard(.x64D3D12Cube, icon: "cube.fill", tint: .indigo,
                     detail: "x86-64 → FEX → native D3D12 → Metal Shader Converter → Metal.")
        }
    }

    private func demoCard(_ request: LaunchRequest, icon: String, tint: Color, detail: String) -> some View {
        Button { start(request) } label: {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.title2)
                    .foregroundStyle(tint)
                    .frame(width: 44, height: 44)
                    .background(tint.opacity(0.18), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(request.title).font(.subheadline.weight(.semibold))
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "play.circle.fill").font(.title2).foregroundStyle(tint)
            }
            .padding(12)
            .background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .buttonStyle(.plain)
    }

    private var footnote: some View {
        Text("One Windows session per launch: to play something else, quit Madeira and open it again.")
            .font(.caption2)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .center)
            .multilineTextAlignment(.center)
            .padding(.top, 4)
    }

    // MARK: Actions

    private func rescan() {
        guard !scanning else { return }
        scanning = true
        DispatchQueue.global(qos: .userInitiated).async {
            let grouped = LibraryGame.group(GameLibrary.scan())
            var labels: [String: String] = [:]
            for game in grouped {
                for exe in game.executables {
                    if let label = PEInfo.archLabel(exe.url) { labels[exe.windowsPath] = label }
                }
            }
            DispatchQueue.main.async {
                games = grouped
                archs = labels
                scanning = false
                scannedOnce = true
            }
        }
    }

    private func launch(_ game: LibraryGame) {
        let exe = game.primary
        if archs[exe.windowsPath] == "x86" {
            blocked32 = game
            return
        }
        // Relocation-stripped exes based below 4 GB cannot load on iOS as they
        // are; FixedBaseImage gives them a relocation table (once, keeping the
        // original).
        if let note = FixedBaseImage.prepare(exe.url) { LogStore.shared.log(note) }
        let saved = GameArguments.get(exe.windowsPath)
        let args = saved.isEmpty ? GameArguments.suggestion(for: exe) : saved
        var request: LaunchRequest
        if LibraryPrefs.inDesktop(exe.windowsPath) {
            let size = ContentView.desktopSize(desktopRes)
            let program = "\"\(exe.windowsPath)\"" + (args.isEmpty ? "" : " " + args)
            request = LaunchRequest(title: game.title, exe: "explorer.exe",
                                    args: "/desktop=shell,\(size.0)x\(size.1) " + program,
                                    desktop: (w: size.0, h: size.1))
        } else {
            request = LaunchRequest(title: game.title, exe: exe.windowsPath, args: args, desktop: nil)
        }
        request.avx = LibraryPrefs.avx(exe.windowsPath)
        request.wineVCRT = LibraryPrefs.wineVCRT(exe.windowsPath)
        LibraryPrefs.markPlayed(game.title)
        start(request)
    }

    /// Every launch goes through here, so none can start without JIT: without
    /// it runWineFullSequence only logs an error, and the user would be left
    /// in the session view with nothing running.
    private func start(_ request: LaunchRequest) {
        jitOn = jit_check_debugged()
        if jitOn {
            onLaunch(request)
        } else {
            pendingAfterJIT = request
            showJITAlert = true
        }
    }

    private func enableJIT() {
        enablingJIT = true
        StikJITHelper.enableJIT { success in
            DispatchQueue.main.async {
                enablingJIT = false
                jitOn = success || jit_check_debugged()
                if jitOn, let request = pendingAfterJIT {
                    pendingAfterJIT = nil
                    onLaunch(request)
                }
            }
        }
    }
}

// MARK: - Pieces

private struct HomeBackground: View {
    var body: some View {
        LinearGradient(colors: [Color(red: 0.05, green: 0.06, blue: 0.10),
                                Color(red: 0.09, green: 0.07, blue: 0.14)],
                       startPoint: .top, endPoint: .bottom)
    }
}

private struct StatusChip: View {
    let icon: String
    let text: String
    let tint: Color

    var body: some View {
        HStack(spacing: 5) {
            Image(systemName: icon).font(.caption2.weight(.bold))
            Text(text).font(.caption.weight(.semibold)).lineLimit(1)
        }
        .foregroundStyle(tint)
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .glassEffect(.regular.tint(tint.opacity(0.25)), in: Capsule())
    }
}

private struct SectionHeader: View {
    let title: String
    let detail: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).font(.title3.weight(.bold))
            Spacer()
            if let detail { Text(detail).font(.caption).foregroundStyle(.secondary) }
        }
    }
}

private struct PlayGlyph: View {
    var body: some View {
        Image(systemName: "play.fill")
            .font(.system(size: 16, weight: .bold))
            .foregroundStyle(.white)
            .frame(width: 40, height: 40)
            .glassEffect(.regular.interactive(), in: Circle())
    }
}

/// Box art: the user's picture if they chose one, otherwise a gradient and
/// initials derived from the title -- stable, so a game keeps its look.
struct CoverArt: View {
    let title: String
    let tick: Int

    var body: some View {
        GeometryReader { geo in
            Group {
                if let image = CoverStore.image(for: title) {
                    Image(uiImage: image).resizable().scaledToFill()
                } else {
                    ZStack {
                        LinearGradient(colors: CoverArt.palette(title),
                                       startPoint: .topLeading, endPoint: .bottomTrailing)
                        Text(CoverArt.initials(title))
                            .font(.system(size: max(18, min(geo.size.width, geo.size.height) * 0.3),
                                          weight: .black, design: .rounded))
                            .foregroundStyle(.white.opacity(0.92))
                            .shadow(color: .black.opacity(0.25), radius: 6, y: 3)
                    }
                }
            }
            .frame(width: geo.size.width, height: geo.size.height)
            .clipped()
        }
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .id(tick)
    }

    static func palette(_ title: String) -> [Color] {
        let h = CoverStore.stableHash(title)
        let hue = Double(h % 360) / 360
        let hue2 = (hue + 0.12).truncatingRemainder(dividingBy: 1)
        return [Color(hue: hue, saturation: 0.70, brightness: 0.62),
                Color(hue: hue2, saturation: 0.80, brightness: 0.32)]
    }

    static func initials(_ title: String) -> String {
        let words = title.split(whereSeparator: { !$0.isLetter && !$0.isNumber })
        let letters = words.prefix(2).compactMap { $0.first.map(String.init) }
        return letters.isEmpty ? "?" : letters.joined().uppercased()
    }
}

private struct GameCard: View {
    let game: LibraryGame
    let arch: String?
    let tick: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ZStack(alignment: .topTrailing) {
                CoverArt(title: game.title, tick: tick)
                    .aspectRatio(3.0 / 4.0, contentMode: .fit)
                if let arch {
                    Text(arch)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .glassEffect(.regular, in: Capsule())
                        .padding(8)
                }
            }
            Text(game.title)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
            Text(game.primary.fileName + " · " + game.primary.sizeDescription)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
            HStack(spacing: 4) {
                Image(systemName: "folder")
                Text(game.primary.folderDescription)
                    .lineLimit(1)
                    .truncationMode(.head)
                if game.executables.count > 1 {
                    Spacer(minLength: 2)
                    Text("\(game.executables.count) exe")
                }
            }
            .font(.caption2)
            .foregroundStyle(.tertiary)
        }
    }
}

// MARK: - Per-game settings

struct GameSettingsSheet: View {
    let game: LibraryGame
    let archs: [String: String]
    let onPlay: () -> Void
    let onCoverChanged: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var exePath: String
    @State private var args: String
    @State private var inDesktop: Bool
    @State private var avx: Bool
    @State private var wineVCRT: Bool
    @State private var photo: PhotosPickerItem?
    @State private var tick = 0

    init(game: LibraryGame, archs: [String: String], onPlay: @escaping () -> Void, onCoverChanged: @escaping () -> Void) {
        self.game = game
        self.archs = archs
        self.onPlay = onPlay
        self.onCoverChanged = onCoverChanged
        let exe = game.primary
        _exePath = State(initialValue: exe.windowsPath)
        _args = State(initialValue: GameArguments.get(exe.windowsPath))
        _inDesktop = State(initialValue: LibraryPrefs.inDesktop(exe.windowsPath))
        _avx = State(initialValue: LibraryPrefs.avx(exe.windowsPath))
        _wineVCRT = State(initialValue: LibraryPrefs.wineVCRT(exe.windowsPath))
    }

    /// What the exe will get: the typed arguments, else the suggestion.
    private var effectiveArgs: String {
        let typed = args.trimmingCharacters(in: .whitespaces)
        return typed.isEmpty ? GameArguments.suggestion(for: selected) : typed
    }

    private func hasFlag(_ flag: String) -> Bool {
        args.split(separator: " ").contains { $0.caseInsensitiveCompare(flag) == .orderedSame }
    }

    /// Adds the flag, or takes it out when it is already there. The DX
    /// choices exclude each other.
    private func toggleFlag(_ flag: String) {
        var parts = args.split(separator: " ").map(String.init)
        if hasFlag(flag) {
            parts.removeAll { $0.caseInsensitiveCompare(flag) == .orderedSame }
        } else {
            if flag.hasPrefix("-dx") { parts.removeAll { $0.lowercased().hasPrefix("-dx") } }
            if flag == "-windowed" { parts.removeAll { $0.lowercased() == "-fullscreen" } }
            if flag == "-fullscreen" { parts.removeAll { $0.lowercased() == "-windowed" } }
            parts.append(flag)
        }
        args = parts.joined(separator: " ")
    }

    private var selected: GuestExecutable {
        game.executables.first(where: { $0.windowsPath == exePath }) ?? game.primary
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Arguments", text: $args,
                              prompt: Text(GameArguments.suggestion(for: selected).isEmpty
                                           ? "none"
                                           : GameArguments.suggestion(for: selected)),
                              axis: .vertical)
                        .font(.body.monospaced())
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .lineLimit(1...4)
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 8) {
                            ForEach(["-dx11", "-dx10", "-dx9", "-dx12", "-windowed", "-fullscreen", "-nosplash"], id: \.self) { flag in
                                Button(flag) { toggleFlag(flag) }
                                    .buttonStyle(.bordered)
                                    .tint(hasFlag(flag) ? Color.accentColor : Color.gray)
                                    .font(.caption.monospaced())
                            }
                        }
                    }
                    Text(selected.fileName + " " + effectiveArgs)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                } header: {
                    Text("Arguments")
                } footer: {
                    Text("Passed to the exe on every launch; the line under the chips is what will run. Empty uses "
                         + "the suggestion shown. Unreal Engine titles default to DX12 -- try -dx11. Crysis picks "
                         + "its renderer with -dx10 / -dx9.")
                }

                Section {
                    Picker("Run", selection: $exePath) {
                        ForEach(game.executables) { exe in
                            Text(exe.fileName + " · " + exe.sizeDescription).tag(exe.windowsPath)
                        }
                    }
                    LabeledContent("Architecture", value: archs[exePath] ?? "unknown")
                    if archs[exePath] == "x86" {
                        Label("32-bit programs cannot run in Madeira. Choose a 64-bit exe.",
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                    Text(exePath)
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                } header: {
                    Text("Executable")
                } footer: {
                    Text("64-bit first, then biggest first. A game's own binary is almost always the largest exe in its folder.")
                }

                Section {
                    Toggle("Run inside the Wine desktop", isOn: $inDesktop)
                    Toggle("AVX / AVX2", isOn: $avx)
                    Toggle("Wine's C++ runtime", isOn: $wineVCRT)
                } header: {
                    Text("Launch options")
                } footer: {
                    Text("The Wine desktop gives a program a window manager; most games do not need one. Turn on AVX when a game "
                         + "quits at start with \"illegal instruction\" (c000001d) in the log: it was built "
                         + "for AVX CPUs. Emulated AVX is slower, so leave it off otherwise. Wine's C++ runtime "
                         + "replaces Microsoft's concrt140/msvcp140_* for a game that crashes right after "
                         + "loading them.")
                }

                Section {
                    CoverArt(title: game.title, tick: tick)
                        .frame(height: 200)
                        .listRowInsets(EdgeInsets())
                    PhotosPicker(selection: $photo, matching: .images) {
                        Label("Choose cover image", systemImage: "photo.on.rectangle")
                    }
                    if CoverStore.hasCover(game.title) {
                        Button(role: .destructive) {
                            CoverStore.remove(game.title)
                            tick += 1
                            onCoverChanged()
                        } label: {
                            Label("Remove cover image", systemImage: "trash")
                        }
                    }
                }

                Section {
                    Button {
                        save()
                        dismiss()
                        onPlay()
                    } label: {
                        Label("Save and play", systemImage: "play.fill")
                            .frame(maxWidth: .infinity)
                            .font(.headline)
                    }
                    // The library's 32-bit alert cannot show while this sheet is
                    // still animating away, so refuse here instead.
                    .disabled(archs[exePath] == "x86")
                }
            }
            .navigationTitle(game.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { save(); dismiss() }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
            .onChange(of: exePath) { _, newPath in
                // Arguments and the desktop choice belong to an exe, not a title.
                args = GameArguments.get(newPath)
                inDesktop = LibraryPrefs.inDesktop(newPath)
                avx = LibraryPrefs.avx(newPath)
                wineVCRT = LibraryPrefs.wineVCRT(newPath)
            }
            .onChange(of: photo) { _, item in
                guard let item else { return }
                Task {
                    if let data = try? await item.loadTransferable(type: Data.self) {
                        CoverStore.save(data, for: game.title)
                        await MainActor.run {
                            tick += 1
                            onCoverChanged()
                        }
                    }
                }
            }
        }
        .preferredColorScheme(.dark)
    }

    private func save() {
        LibraryPrefs.setPrimaryPath(exePath, for: game.title)
        GameArguments.set(args, for: exePath)
        LibraryPrefs.setInDesktop(inDesktop, for: exePath)
        LibraryPrefs.setAVX(avx, for: exePath)
        LibraryPrefs.setWineVCRT(wineVCRT, for: exePath)
    }
}

// MARK: - App settings

struct AppSettingsSheet: View {
    let onDeveloper: () -> Void

    @Environment(\.dismiss) private var dismiss
    @AppStorage("wine_desktop_res") private var desktopRes = "960x540"
    @State private var storageBackedMemory = ExperimentalSettings.storageBackedMemory
    @State private var showControllers = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Display") {
                    Picker("Wine desktop size", selection: $desktopRes) {
                        ForEach(ContentView.desktopResolutions, id: \.self) { Text($0).tag($0) }
                    }
                }

                Section("Input") {
                    Button { showControllers = true } label: {
                        Label("Game controllers", systemImage: "gamecontroller")
                    }
                }

                Section {
                    Button { FilesApp.openDriveC() } label: {
                        Label("Open C:\\ in Files", systemImage: "folder")
                    }
                } header: {
                    Text("Games")
                } footer: {
                    Text("Games live in the Windows drive: On My iPhone › Madeira › wine › drive_c.")
                }

                Section {
                    Toggle(isOn: $storageBackedMemory) {
                        Label("Storage-backed memory", systemImage: "internaldrive")
                    }
                    .onChange(of: storageBackedMemory) { _, on in
                        ExperimentalSettings.storageBackedMemory = on
                    }
                } header: {
                    Text("Experimental")
                } footer: {
                    Text("Backs a game's large memory allocations with up to \(ExperimentalSettings.storageBackedMemoryMB / 1024) GB "
                         + "of the device's storage, so iOS can move them out of RAM instead of closing Madeira "
                         + "when a game needs more than fits. Costs some speed when that happens, and writes to "
                         + "storage. Saved as swap-mb in madeira.cfg; takes effect at the next game start, and "
                         + "the log shows [swap] ml1077 lines when it is working.")
                }

                Section {
                    ShareLink(items: LogStore.shared.exportableLogs) {
                        Label("Share logs", systemImage: "square.and.arrow.up")
                    }
                    Button {
                        dismiss()
                        onDeveloper()
                    } label: {
                        Label("Developer tools", systemImage: "hammer")
                    }
                } header: {
                    Text("Diagnostics")
                } footer: {
                    Text("Developer tools is the full test panel: every launch button, the log console and "
                         + "the probes. It is a one-way switch for this launch, like starting a game.")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .sheet(isPresented: $showControllers) { ControllerSetupSheet() }
        }
        .preferredColorScheme(.dark)
    }
}
