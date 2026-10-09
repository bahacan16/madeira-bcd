import Foundation
import SwiftUI

@main
struct MadeiraApp: App {
    init() {
        // madeira-bcd: the Metal Performance HUD's performance insights (the
        // notes that stack up left of its panel: shader compiles, similar render
        // passes, blit encoders) cover the game. Off unless madeira.cfg says
        // env.MTL_HUD_INSIGHTS_ENABLED = 1, which is exported later and wins.
        setenv("MTL_HUD_INSIGHTS_ENABLED", "0", 0)
        // ml1172: read the screen on the main thread; library entries, whose
        // default Resolution comes from it, are also made on other threads.
        _ = ResolutionChoices.screen
        // madeira-bcd: update packs are gone (owner's decision 2026-10-09, as
        // upstream); a pack an earlier build installed is deleted.
        if let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first {
            let packs = base.appendingPathComponent("MadeiraPacks", isDirectory: true)
            if FileManager.default.fileExists(atPath: packs.path) { try? FileManager.default.removeItem(at: packs) }
        }
        UserDefaults.standard.removeObject(forKey: "madeira.packs.enabled")
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .modifier(ClaimGamepadEvents())
                .onAppear {
                    GamepadInput.shared.start()
                    HardwareInput.shared.start()
                    JITNetworkShortcut.shared.restoreLeftover()   // also starts its network path monitor
                }
                // madeira://jit-network/... (the Madeira JIT shortcut returning, JITNetwork.swift),
                // else madeira://play?exe=... (Home Screen shortcuts, SavesAndShortcuts.swift).
                .onOpenURL { url in if !JITNetworkShortcut.shared.handle(url) { ShortcutRouter.shared.handle(url) } }
        }
    }
}
