//
//  GameRecommendationsApply.swift
//  Madeira
//
//  madeira-bcd: writes GameRecommendations.swift's lists into a game's
//  settings (owner's decision, 2026-10-09).
//
//  The first time Madeira sees a game it has a recommendation for (a Steam
//  download finishing, the library opening or saving the game, Game details
//  opening, a start, the madeira-bcd home screen's settings and starts):
//    - with nothing of its own yet (no config file, no lines in upstream's
//      `config` field), the game gets its whole recommendation;
//    - with a config of its own, it keeps it, and only Reset to Recommended
//      replaces it.
//  A config Madeira wrote that nobody has changed since follows a newer
//  version of its list (the config file only); one the player changed, or
//  emptied, is never written again by itself. "Reset to Recommended" at the
//  bottom of the game's settings writes the whole recommendation again,
//  whatever the game holds; for a game without one it empties the game's
//  config.
//

import Foundation

extension GameRecommendations {
    /// What Madeira did for which game: [lowercased Windows path: "<id>#<version>#<file hash>"
    /// for a recommendation it wrote, "" for a game that kept its own config].
    private static let appliedKey = "madeira.recommended.applied"

    private enum Write: String { case firstSight = "first sight", updated, reset }

    /// The recommendation for a game in drive_c: a program, or a Steam game's install folder.
    static func recommendation(windowsPath: String, steamAppID: Int?) -> GameRecommendation? {
        match(windowsPath: windowsPath, steamAppID: steamAppID, fileExists: driveFileExists)
    }

    static func recommendation(for entry: LibraryEntry) -> GameRecommendation? {
        entry.desktop == true ? nil : recommendation(windowsPath: entry.windowsPath, steamAppID: entry.steamAppID)
    }

    /// A library entry's recommendation, when one is due (see above). True when the
    /// entry's own fields took it too, so the caller saves the entry.
    @discardableResult
    static func prepare(_ entry: inout LibraryEntry) -> Bool {
        guard entry.desktop != true,
              let found = due(entry.windowsPath, steamAppID: entry.steamAppID, ownLines: entry.config, title: entry.title),
              put(found.rec, to: entry.windowsPath, found.kind, title: entry.title), found.kind == .firstSight else { return false }
        takeEntryFields(found.rec, &entry)
        return true
    }

    /// A madeira-bcd home screen game (its program's path), before its settings or a start.
    static func prepare(windowsPath: String, title: String) {
        guard let found = due(windowsPath, steamAppID: nil, ownLines: nil, title: title),
              put(found.rec, to: windowsPath, found.kind, title: title), found.kind == .firstSight else { return }
        takeHomeSwitches(found.rec, windowsPath)
    }

    /// Game details › Reset to Recommended. Returns what was written; nil when the
    /// game has no recommendation and its config was emptied.
    @discardableResult
    static func reset(_ entry: inout LibraryEntry) -> GameRecommendation? {
        entry.config = nil
        guard let rec = recommendation(for: entry) else { clear(entry.windowsPath, title: entry.title); return nil }
        put(rec, to: entry.windowsPath, .reset, title: entry.title)
        takeEntryFields(rec, &entry)
        return rec
    }

    /// The madeira-bcd home screen's Reset to Recommended.
    @discardableResult
    static func reset(windowsPath: String, title: String) -> GameRecommendation? {
        guard let rec = recommendation(windowsPath: windowsPath, steamAppID: nil) else { clear(windowsPath, title: title); return nil }
        put(rec, to: windowsPath, .reset, title: title)
        takeHomeSwitches(rec, windowsPath)
        return rec
    }

    /// The line under Reset to Recommended.
    static func note(_ rec: GameRecommendation?) -> String {
        guard let rec else { return "Madeira has no recommended settings for this game yet; resetting empties this game's config." }
        let lines = GameProfile.parse(rec.config).count
        let switches = rec.switchSummary.isEmpty ? "" : "; " + rec.switchSummary
        return "Madeira's recommended settings for \(rec.title): \(lines) config lines\(switches). Resetting replaces this game's config and these switches with them."
    }

    /// The question before a reset.
    static func question(_ rec: GameRecommendation?) -> String {
        guard let rec else { return "Madeira has no recommended settings for this game yet. Empty this game's config?" }
        return "Replace this game's config and switches with Madeira's recommended settings for \(rec.title)? Changes you made to them are lost."
    }

    // MARK: -

    private static func due(_ windowsPath: String, steamAppID: Int?, ownLines: String?,
                            title: String) -> (rec: GameRecommendation, kind: Write)? {
        let text = GameProfile(windowsPath: windowsPath).text
        if let mark = marks[windowsPath.lowercased()] {
            // Written before: a newer list replaces it while the file is exactly what was written.
            guard !mark.isEmpty, mark.hasSuffix("#" + fileHash(text)),
                  let rec = recommendation(windowsPath: windowsPath, steamAppID: steamAppID), mark != stamp(rec) else { return nil }
            return (rec: rec, kind: .updated)
        }
        guard let rec = recommendation(windowsPath: windowsPath, steamAppID: steamAppID) else { return nil }
        if !(text + (ownLines ?? "")).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            // First seen with a config of its own: it stays, now and later.
            setMark("", windowsPath)
            LogStore.shared.log("[recommended] \(title): keeps its own config; \(rec.id) v\(rec.version) is one Reset to Recommended away")
            return nil
        }
        return (rec: rec, kind: .firstSight)
    }

    /// The config file, and on first sight or a reset the switches LibraryPrefs keeps.
    @discardableResult
    private static func put(_ rec: GameRecommendation, to windowsPath: String, _ kind: Write, title: String) -> Bool {
        let file = GameProfile(windowsPath: windowsPath)
        file.text = rec.fileText
        guard file.text == rec.fileText else {
            LogStore.shared.log("[recommended] \(title): could not write \(rec.id)", level: .error)
            return false
        }
        if kind != .updated {
            if let on = rec.avx { LibraryPrefs.setAVX(on, for: windowsPath) }
            if let on = rec.nvidia { LibraryPrefs.setNvidia(on, for: windowsPath) }
            if let on = rec.wineVCRT { LibraryPrefs.setWineVCRT(on, for: windowsPath) }
        }
        setMark(stamp(rec), windowsPath)
        LogStore.shared.log("[recommended] \(title): \(rec.id) v\(rec.version) written (\(kind.rawValue))")
        return true
    }

    private static func clear(_ windowsPath: String, title: String) {
        GameProfile(windowsPath: windowsPath).text = ""
        setMark(nil, windowsPath)
        LogStore.shared.log("[recommended] \(title): none for this game; its config was emptied (reset)")
    }

    /// The switches a library entry keeps itself.
    private static func takeEntryFields(_ rec: GameRecommendation, _ entry: inout LibraryEntry) {
        if let on = rec.fastSync { entry.fastSync = on }
        if let on = rec.semaphoreFastPath { entry.semaphoreFastPath = on }
        if let mode = rec.fpsMode { entry.fpsMode = mode }
        if let size = rec.resolution { entry.resolution = size }
        if let mode = rec.display { entry.display = mode == DisplayMode.fit.rawValue ? nil : mode }
    }

    /// The madeira-bcd home screen's forms of the same switches: Safe thread sync is
    /// fastsync off; Screen size takes the resolution when it is one of its sizes.
    private static func takeHomeSwitches(_ rec: GameRecommendation, _ windowsPath: String) {
        if let on = rec.fastSync { LibraryPrefs.setSafeSync(!on, for: windowsPath) }
        if let size = rec.resolution, LibraryPrefs.screenSizes.contains(size) { LibraryPrefs.setScreen(size, for: windowsPath) }
    }

    private static var marks: [String: String] {
        (UserDefaults.standard.dictionary(forKey: appliedKey) as? [String: String]) ?? [:]
    }

    private static func setMark(_ value: String?, _ windowsPath: String) {
        var all = marks
        all[windowsPath.lowercased()] = value
        UserDefaults.standard.set(all, forKey: appliedKey)
    }

    private static func stamp(_ rec: GameRecommendation) -> String { "\(rec.id)#\(rec.version)#\(fileHash(rec.fileText))" }

    private static func fileHash(_ text: String) -> String { String(CoverStore.stableHash(text), radix: 16) }

    /// Whether a C:\ path exists in drive_c; the name's case does not matter, as on Windows.
    static func driveFileExists(_ windowsPath: String) -> Bool {
        let path = windowsPath.replacingOccurrences(of: "\\", with: "/")
        guard path.lowercased().hasPrefix("c:/") else { return false }
        let url = LibraryModel.drive.appendingPathComponent(String(path.dropFirst(3)))
        let name = url.lastPathComponent.lowercased()
        let names = (try? FileManager.default.contentsOfDirectory(atPath: url.deletingLastPathComponent().path)) ?? []
        return names.contains { $0.lowercased() == name }
    }
}
