//
//  GameLibrary.swift
//  Finding and launching guest executables without a rebuild.
//
//  Every title so far needed its own hard-coded button, and the escape hatch
//  was madeira-exe.txt -- a file the user has to create in the Files app, spell
//  a Windows path into by hand, and get exactly right. The first time it was
//  used in anger the answer was "ERR No documents/madeire-exe.txt".
//
//  The prefix already knows what is installed. C:\ is <Documents>/wine/drive_c,
//  so scanning it answers the question directly: these are the executables you
//  could run. Wine's own tree is excluded, because a list where cmd.exe and
//  winecfg.exe outnumber the games is not a list anyone reads.
//
//  Arguments stay per-title and persist, so "Stray needs -dx11 -windowed" is
//  remembered rather than retyped, and the hard-coded buttons keep working --
//  this is an addition, not a replacement.
//

import Foundation
import SwiftUI

// MARK: - Model

struct GuestExecutable: Identifiable, Hashable {
    /// Windows path as the guest sees it, e.g. C:\Program Files\Stray\...\Stray-Win64-Shipping.exe
    let windowsPath: String
    /// Location on the device, for size and mtime.
    let url: URL
    /// Folder immediately under Program Files, or the drive root -- what a person calls the game.
    let title: String
    let sizeBytes: Int64

    var id: String { windowsPath }

    var fileName: String { url.lastPathComponent }

    var sizeDescription: String {
        let mb = Double(sizeBytes) / (1024 * 1024)
        if mb < 1 { return String(format: "%.0f KB", Double(sizeBytes) / 1024) }
        return mb < 1024 ? String(format: "%.1f MB", mb) : String(format: "%.2f GB", mb / 1024)
    }
}

// MARK: - Scanner

enum GameLibrary {

    /// Directories under drive_c that are Wine's, not the user's.
    private static let skippedRoots: Set<String> = [
        "windows", "users", "proc", "programdata",
    ]

    /// Launchers and tooling that ship beside a game and are never the thing you want.
    private static let noiseNames: Set<String> = [
        "unins000.exe", "uninstall.exe", "uninstaller.exe",
        "vcredist_x64.exe", "vcredist_x86.exe", "dxwebsetup.exe",
        "dotnetfx.exe", "oalinst.exe", "crashreporter.exe",
        "crashhandler.exe", "ueprereqsetup_x64.exe",
    ]

    static var driveC: URL? {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first?
            .appendingPathComponent("wine")
            .appendingPathComponent("drive_c")
    }

    /// Everything under drive_c that could plausibly be launched, deepest-first by size.
    ///
    /// Depth is bounded because a game tree can be arbitrarily deep and a scan that
    /// walks all of it on the main thread would stall the UI on first open.
    static func scan(maxDepth: Int = 6) -> [GuestExecutable] {
        guard let root = driveC else { return [] }
        var found: [GuestExecutable] = []
        let fm = FileManager.default

        func walk(_ dir: URL, depth: Int, title: String?) {
            guard depth <= maxDepth else { return }
            guard let entries = try? fm.contentsOfDirectory(
                at: dir,
                includingPropertiesForKeys: [.isDirectoryKey, .fileSizeKey],
                options: [.skipsHiddenFiles]
            ) else { return }

            for entry in entries {
                let values = try? entry.resourceValues(forKeys: [.isDirectoryKey, .fileSizeKey])
                if values?.isDirectory == true {
                    if depth == 0 && skippedRoots.contains(entry.lastPathComponent.lowercased()) { continue }
                    // The first directory below drive_c (or below Program Files) names the title.
                    walk(entry, depth: depth + 1, title: title ?? titleFor(entry))
                } else if entry.pathExtension.lowercased() == "exe" {
                    if noiseNames.contains(entry.lastPathComponent.lowercased()) { continue }
                    guard let windows = windowsPath(for: entry, root: root) else { continue }
                    found.append(GuestExecutable(
                        windowsPath: windows,
                        url: entry,
                        title: title ?? entry.deletingPathExtension().lastPathComponent,
                        sizeBytes: Int64(values?.fileSize ?? 0)
                    ))
                }
            }
        }

        walk(root, depth: 0, title: nil)

        // Biggest first: a game's shipping binary dwarfs the launchers next to it,
        // so this puts the thing you actually want at the top without guessing.
        return found.sorted {
            $0.title == $1.title ? $0.sizeBytes > $1.sizeBytes
                                 : $0.title.localizedCaseInsensitiveCompare($1.title) == .orderedAscending
        }
    }

    /// The folder a person would name: the one under Program Files, else the one
    /// under drive_c. Returns nil for the Program Files containers themselves so
    /// the level below them supplies the title instead.
    private static func titleFor(_ dir: URL) -> String? {
        let name = dir.lastPathComponent
        return name.lowercased().hasPrefix("program files") ? nil : name
    }

    /// Translate a device URL under drive_c into the guest's C:\ path.
    /// Returns nil for anything outside the prefix, which the guest could not open anyway.
    static func windowsPath(for url: URL, root: URL? = nil) -> String? {
        guard let root = root ?? driveC else { return nil }
        let rootPath = root.standardizedFileURL.path
        let filePath = url.standardizedFileURL.path
        guard filePath.hasPrefix(rootPath + "/") else { return nil }
        let relative = String(filePath.dropFirst(rootPath.count + 1))
        return "C:\\" + relative.replacingOccurrences(of: "/", with: "\\")
    }
}

// MARK: - Remembered arguments

/// Per-title command line, keyed by Windows path so two builds of the same game
/// do not share one setting. Kept in UserDefaults: it is a handful of short
/// strings, and it has to survive the app being killed by the JIT debugger.
enum GameArguments {
    private static let key = "madeira.gameArguments"

    static func get(_ windowsPath: String) -> String {
        (UserDefaults.standard.dictionary(forKey: key) as? [String: String])?[windowsPath] ?? ""
    }

    static func set(_ args: String, for windowsPath: String) {
        var all = (UserDefaults.standard.dictionary(forKey: key) as? [String: String]) ?? [:]
        let trimmed = args.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { all.removeValue(forKey: windowsPath) } else { all[windowsPath] = trimmed }
        UserDefaults.standard.set(all, forKey: key)
    }

    /// Defaults for titles whose required flags are already known from the
    /// hard-coded buttons, so a freshly scanned library launches them correctly
    /// the first time instead of reproducing the DX12 hang.
    static func suggestion(for exe: GuestExecutable) -> String {
        let name = exe.fileName.lowercased()
        if name.hasPrefix("stray-win64") { return "Hk_project -dx11 -windowed" }
        if name.hasSuffix("-win64-shipping.exe") { return "-dx11 -windowed" }
        return ""
    }
}

// MARK: - UI

struct GameLibraryView: View {
    /// Called with the Windows path and the arguments to use.
    let onLaunch: (String, String) -> Void
    let onLog: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var games: [GuestExecutable] = []
    @State private var scanned = false
    @State private var editing: GuestExecutable?
    @State private var argsDraft = ""

    var body: some View {
        NavigationStack {
            Group {
                if !scanned {
                    ProgressView("Scanning C:\\ …")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if games.isEmpty {
                    emptyState
                } else {
                    list
                }
            }
            .navigationTitle("Games")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Button { rescan() } label: { Image(systemName: "arrow.clockwise") }
                        .disabled(!scanned)
                }
            }
        }
        .task { if !scanned { rescan() } }
        .sheet(item: $editing) { game in argumentsSheet(for: game) }
    }

    private var emptyState: some View {
        VStack(spacing: 14) {
            Image(systemName: "externaldrive.badge.questionmark")
                .font(.system(size: 44))
                .foregroundStyle(.secondary)
            Text("No executables found")
                .font(.headline)
            Text("Copy a game folder into\nFiles → On My iPhone → Madeira → wine → drive_c → Program Files,\nthen tap refresh.")
                .font(.footnote)
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 28)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var list: some View {
        List {
            ForEach(games) { game in
                Button {
                    let args = resolvedArguments(for: game)
                    onLog("Library: \(game.windowsPath)")
                    onLog("Library: args = \(args.isEmpty ? "(none)" : args)")
                    onLaunch(game.windowsPath, args)
                    dismiss()
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(game.title.isEmpty ? game.fileName : game.title)
                            .font(.body.weight(.medium))
                        Text(game.fileName)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        HStack(spacing: 6) {
                            Text(game.sizeDescription)
                            let args = resolvedArguments(for: game)
                            if !args.isEmpty {
                                Text("·")
                                Text(args).lineLimit(1)
                            }
                        }
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    }
                }
                .swipeActions(edge: .trailing) {
                    Button {
                        argsDraft = resolvedArguments(for: game)
                        editing = game
                    } label: {
                        Label("Arguments", systemImage: "text.cursor")
                    }
                    .tint(.indigo)
                }
            }
        }
    }

    private func argumentsSheet(for game: GuestExecutable) -> some View {
        NavigationStack {
            Form {
                Section("Command line") {
                    TextField("(none)", text: $argsDraft, axis: .vertical)
                        .font(.system(.body, design: .monospaced))
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                }
                Section {
                    Text(game.windowsPath)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
            .navigationTitle(game.title.isEmpty ? game.fileName : game.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { editing = nil }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        GameArguments.set(argsDraft, for: game.windowsPath)
                        editing = nil
                    }
                }
            }
        }
    }

    /// A saved value always wins, including an explicitly emptied one; the
    /// suggestion only fills in for a title never configured here.
    private func resolvedArguments(for game: GuestExecutable) -> String {
        let saved = GameArguments.get(game.windowsPath)
        if !saved.isEmpty { return saved }
        return GameArguments.suggestion(for: game)
    }

    private func rescan() {
        scanned = false
        DispatchQueue.global(qos: .userInitiated).async {
            let found = GameLibrary.scan()
            DispatchQueue.main.async {
                games = found
                scanned = true
                onLog("Library: found \(found.count) executable\(found.count == 1 ? "" : "s") under C:\\")
            }
        }
    }
}
