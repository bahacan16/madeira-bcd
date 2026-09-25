import SwiftUI

@main
struct MadeiraApp: App {
    var body: some Scene {
        WindowGroup {
            RootView()
                .modifier(ClaimGamepadEvents())
                .onAppear { GamepadInput.shared.start() }
        }
    }
}
