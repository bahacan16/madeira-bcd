//
//  StorageSection.swift
//  Madeira
//
//  madeira-bcd: Settings > Storage -- what the runtime keeps on the phone and a
//  way to take back the space. Frame captures (the overlay's CAP), session logs
//  and the shader caches all grow without the user ever seeing them; a phone
//  that fills up kills the game mid-session in ways that look like our bugs.
//

import SwiftUI

struct StorageItem: Identifiable {
    let id: String
    let title: String
    let detail: String
    let deletable: Bool
    let urls: () -> [URL]
}

final class StorageModel: ObservableObject {
    @Published private(set) var sizes: [String: Int64] = [:]
    @Published private(set) var busy = false

    static var documents: URL? { FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first }

    /// C:\users\<name>\AppData\Local\Madeira\ShaderCache for every profile.
    static func d3d12ShaderCaches() -> [URL] {
        guard let users = GameLibrary.driveC?.appendingPathComponent("users"),
              let names = try? FileManager.default.contentsOfDirectory(atPath: users.path) else { return [] }
        return names.map {
            users.appendingPathComponent($0).appendingPathComponent("AppData/Local/Madeira/ShaderCache", isDirectory: true)
        }.filter { FileManager.default.fileExists(atPath: $0.path) }
    }

    static let items: [StorageItem] = [
        StorageItem(id: "capture", title: "Frame captures",
                    detail: "Written by the overlay's CAP button", deletable: true,
                    urls: { [StorageModel.documents?.appendingPathComponent("capture", isDirectory: true)].compactMap { $0 } }),
        StorageItem(id: "logs", title: "Session logs",
                    detail: "Earlier games' logs (the newest 40 are kept)", deletable: true,
                    urls: { [StorageModel.documents?.appendingPathComponent("logs", isDirectory: true),
                             StorageModel.documents?.appendingPathComponent("madeira-log.prev.txt")].compactMap { $0 } }),
        StorageItem(id: "sc12", title: "D3D12 shader cache",
                    detail: "Converted shaders; deleting it makes the next start convert them again", deletable: true,
                    urls: { StorageModel.d3d12ShaderCaches() }),
        StorageItem(id: "sc11", title: "D3D11 shader cache",
                    detail: "DXMT's compiled shaders", deletable: true,
                    urls: { [StorageModel.documents?.appendingPathComponent("shadercache", isDirectory: true)].compactMap { $0 } }),
        StorageItem(id: "drive", title: "Windows drive (C:)",
                    detail: "Games, saves and the Windows files", deletable: false,
                    urls: { [GameLibrary.driveC].compactMap { $0 } }),
    ]

    func refresh() {
        guard !busy else { return }
        busy = true
        DispatchQueue.global(qos: .utility).async {
            var out: [String: Int64] = [:]
            for item in StorageModel.items {
                out[item.id] = item.urls().reduce(Int64(0)) { $0 + StorageModel.size(of: $1) }
            }
            DispatchQueue.main.async {
                self.sizes = out
                self.busy = false
            }
        }
    }

    func clear(_ item: StorageItem) {
        guard item.deletable else { return }
        DispatchQueue.global(qos: .utility).async {
            for u in item.urls() { try? FileManager.default.removeItem(at: u) }
            DispatchQueue.main.async { self.refresh() }
        }
    }

    /// Allocated bytes under `url` (a file or a directory; symlinks are not followed).
    static func size(of url: URL) -> Int64 {
        let fm = FileManager.default
        let keys: [URLResourceKey] = [.isRegularFileKey, .totalFileAllocatedSizeKey, .fileAllocatedSizeKey]
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: url.path, isDirectory: &isDir) else { return 0 }
        if !isDir.boolValue {
            let v = try? url.resourceValues(forKeys: Set(keys))
            return Int64(v?.totalFileAllocatedSize ?? v?.fileAllocatedSize ?? 0)
        }
        var total: Int64 = 0
        if let e = fm.enumerator(at: url, includingPropertiesForKeys: keys, options: [], errorHandler: { _, _ in true }) {
            for case let f as URL in e {
                guard let v = try? f.resourceValues(forKeys: Set(keys)), v.isRegularFile == true else { continue }
                total += Int64(v.totalFileAllocatedSize ?? v.fileAllocatedSize ?? 0)
            }
        }
        return total
    }
}

struct StorageSection: View {
    @StateObject private var model = StorageModel()
    @State private var confirm: StorageItem?

    var body: some View {
        Section {
            ForEach(StorageModel.items) { item in
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.title)
                        Text(item.detail).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Text(sizeText(item))
                        .font(.footnote.monospacedDigit())
                        .foregroundStyle(.secondary)
                    if item.deletable {
                        Button(role: .destructive) { confirm = item } label: { Image(systemName: "trash") }
                            .buttonStyle(.borderless)
                            .disabled((model.sizes[item.id] ?? 0) == 0)
                    }
                }
            }
        } header: {
            HStack {
                Text("Storage")
                if model.busy { Spacer(); ProgressView() }
            }
        } footer: {
            Text("Sizes are what each item takes on this phone. Nothing here touches games or saves.")
        }
        .onAppear { model.refresh() }
        .confirmationDialog(confirm.map { "Delete \($0.title.lowercased())?" } ?? "",
                            isPresented: Binding(get: { confirm != nil }, set: { if !$0 { confirm = nil } }),
                            titleVisibility: .visible,
                            presenting: confirm) { item in
            Button("Delete", role: .destructive) { model.clear(item) }
        } message: { item in
            Text(item.detail)
        }
    }

    private func sizeText(_ item: StorageItem) -> String {
        guard let s = model.sizes[item.id] else { return "…" }
        return ByteCountFormatter.string(fromByteCount: s, countStyle: .file)
    }
}
