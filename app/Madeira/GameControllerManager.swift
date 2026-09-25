//
//  GameControllerManager.swift
//  Madeira
//
//  Bridges physical game controllers (MFi, Xbox, PlayStation DualSense/DualShock, Nintendo Switch)
//  connected to iOS via GameController.framework into Wine/Windows input.
//
//  Ported unchanged from SaimSuhailQu/Madeira (commit 72ca339), a sibling fork
//  of the same upstream. Nothing here needed adapting: it calls
//  winios_post_key and winios_pointer, which Winios.h declares with the same
//  signatures in this tree, and LogStore.shared.log, which exists here too.
//  Kept verbatim so the next change from that fork can be diffed against it.
//

import Foundation
import GameController

final class GameControllerManager: ObservableObject {
    static let shared = GameControllerManager()

    @Published var isEnabled: Bool = true {
        didSet {
            if isEnabled {
                refreshControllers()
            } else {
                activeControllerName = nil
                connectedControllersCount = 0
                lastPressedButton = "Disabled"
            }
        }
    }

    /// madeira-bcd: upstream (willfaust/Madeira #22, ml1920/ml1930) now hands
    /// physical pads to games as real XInput controllers. Mapping the same pad
    /// to keyboard and mouse as well would give a game every press twice, so
    /// the keyboard/mouse mapping below is opt-in while XInput is on; detection
    /// and the live input tester keep working either way.
    static let keyboardMappingKey = "madeira.controllers.keyboardMapping"
    @Published var keyboardMapping: Bool = UserDefaults.standard.object(forKey: GameControllerManager.keyboardMappingKey) as? Bool
        ?? !GameControllerManager.xinputOn {
        didSet { UserDefaults.standard.set(keyboardMapping, forKey: Self.keyboardMappingKey) }
    }

    /// Mirrors GamepadInput.enabled (madeira.cfg env.MADEIRA_XINPUT, default on)
    /// without needing the main actor.
    static var xinputOn: Bool {
        let value = MadeiraConfig.get("env.MADEIRA_XINPUT") ?? ProcessInfo.processInfo.environment["MADEIRA_XINPUT"]
        return value != "0"
    }

    private func postKey(_ vk: Int32, _ down: Int32) {
        if keyboardMapping { winios_post_key(vk, down) }
    }

    private func postPointer(_ x: Int32, _ y: Int32, _ flags: UInt32, _ data: UInt32) {
        if keyboardMapping { winios_pointer(x, y, flags, data) }
    }

    @Published var connectedControllersCount: Int = 0
    @Published var activeControllerName: String? = nil

    // Live input tester state (confirmed working with DualSense / Xbox / MFi)
    @Published var lastPressedButton: String = "None"
    @Published var leftStickValues: CGPoint = .zero
    @Published var rightStickValues: CGPoint = .zero
    @Published var triggerValues: (Float, Float) = (0.0, 0.0)

    // Virtual gamepad persistence flag
    @Published var virtualGamepadEnabled: Bool = true

    private var retryTimer: Timer?

    // State tracking for sticks & buttons to prevent duplicate / stutter events
    private var leftStickDir: Int = -1
    private var rightStickDir: Int = -1
    private var dpadDir: Int = -1

    // Virtual key definitions (Windows VK)
    private let vkW: Int32 = 0x57
    private let vkA: Int32 = 0x41
    private let vkS: Int32 = 0x53
    private let vkD: Int32 = 0x44

    private let vkUp: Int32 = 0x26
    private let vkRight: Int32 = 0x27
    private let vkDown: Int32 = 0x28
    private let vkLeft: Int32 = 0x25

    // Face buttons: A -> Space (0x20), B -> Esc (0x1B), X -> E (0x45), Y -> R (0x52)
    private let vkA_button: Int32 = 0x20   // Space
    private let vkB_button: Int32 = 0x1B   // Esc
    private let vkX_button: Int32 = 0x45   // E (Interact)
    private let vkY_button: Int32 = 0x52   // R (Reload)

    // Bumpers: LB -> Shift (0x10), RB -> Tab (0x09)
    private let vkLB: Int32 = 0x10         // Shift (Sprint)
    private let vkRB: Int32 = 0x09         // Tab (Inventory/Scoreboard)

    // Stick clicks: L3 -> Ctrl (0x11, Crouch), R3 -> F (0x46, Flashlight/Melee)
    private let vkL3: Int32 = 0x11
    private let vkR3: Int32 = 0x46

    // Mouse buttons: LT -> Right Click (Aim), RT -> Left Click (Fire)
    private var ltDown = false
    private var rtDown = false

    private let deadzone: Float = 0.22

    private init() {
        setupNotifications()
        refreshControllers()
        startPeriodicRetry()
    }

    private func startPeriodicRetry() {
        // Automatic detection/retries: polls every 3 seconds for connected controllers or re-pairing
        retryTimer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            guard let self = self, self.isEnabled else { return }
            if self.connectedControllersCount == 0 {
                self.refreshControllers()
            }
        }
    }

    private func setupNotifications() {
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(controllerDidConnect(_:)),
            name: .GCControllerDidConnect,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(controllerDidDisconnect(_:)),
            name: .GCControllerDidDisconnect,
            object: nil
        )
    }

    @objc private func controllerDidConnect(_ notification: Notification) {
        if let controller = notification.object as? GCController {
            LogStore.shared.log("GameController connected: \(controller.vendorName ?? "Generic Controller")", level: .success)
            configureController(controller)
        }
        refreshControllers()
    }

    @objc private func controllerDidDisconnect(_ notification: Notification) {
        if let controller = notification.object as? GCController {
            LogStore.shared.log("GameController disconnected: \(controller.vendorName ?? "Generic Controller")", level: .info)
        }
        refreshControllers()
    }

    func refreshControllers() {
        let controllers = GCController.controllers()
        DispatchQueue.main.async {
            self.connectedControllersCount = controllers.count
            self.activeControllerName = controllers.first?.vendorName
        }
        for controller in controllers {
            configureController(controller)
        }
    }

    private func configureController(_ controller: GCController) {
        guard isEnabled, let gamepad = controller.extendedGamepad else { return }

        // --- Left Thumbstick -> WASD ---
        gamepad.leftThumbstick.valueChangedHandler = { [weak self] (_, xValue, yValue) in
            guard let self = self, self.isEnabled else { return }
            self.leftStickValues = CGPoint(x: CGFloat(xValue), y: CGFloat(yValue))
            self.handleStick(x: xValue, y: yValue, stickType: .left)
        }

        // --- Right Thumbstick -> Arrow Keys / Camera ---
        gamepad.rightThumbstick.valueChangedHandler = { [weak self] (_, xValue, yValue) in
            guard let self = self, self.isEnabled else { return }
            self.rightStickValues = CGPoint(x: CGFloat(xValue), y: CGFloat(yValue))
            self.handleStick(x: xValue, y: yValue, stickType: .right)
        }

        // --- D-Pad -> Arrow Keys ---
        gamepad.dpad.valueChangedHandler = { [weak self] (_, xValue, yValue) in
            guard let self = self, self.isEnabled else { return }
            if abs(xValue) > 0.1 || abs(yValue) > 0.1 {
                self.lastPressedButton = "D-Pad (\(String(format: "%.1f, %.1f", xValue, yValue)))"
            }
            self.handleStick(x: xValue, y: yValue, stickType: .dpad)
        }

        // --- Face Buttons ---
        gamepad.buttonA.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "Button A (Cross / Space)" }
            self.postKey(self.vkA_button, pressed ? 1 : 0)
        }

        gamepad.buttonB.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "Button B (Circle / Esc)" }
            self.postKey(self.vkB_button, pressed ? 1 : 0)
        }

        gamepad.buttonX.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "Button X (Square / E)" }
            self.postKey(self.vkX_button, pressed ? 1 : 0)
        }

        gamepad.buttonY.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "Button Y (Triangle / R)" }
            self.postKey(self.vkY_button, pressed ? 1 : 0)
        }

        // --- Bumpers ---
        gamepad.leftShoulder.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "LB / L1 (Sprint)" }
            self.postKey(self.vkLB, pressed ? 1 : 0)
        }

        gamepad.rightShoulder.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "RB / R1 (Tab)" }
            self.postKey(self.vkRB, pressed ? 1 : 0)
        }

        // --- Triggers (Mouse Left / Right Click) ---
        gamepad.leftTrigger.valueChangedHandler = { [weak self] (_, value, pressed) in
            guard let self = self, self.isEnabled else { return }
            self.triggerValues = (value, self.triggerValues.1)
            let isDown = value > 0.3 || pressed
            if isDown && !self.ltDown { self.lastPressedButton = "LT / L2 (Aim / RightClick)" }
            if isDown != self.ltDown {
                self.ltDown = isDown
                self.postPointer(0, 0, isDown ? 0x0008 : 0x0010, 0) // RIGHTDOWN / RIGHTUP
            }
        }

        gamepad.rightTrigger.valueChangedHandler = { [weak self] (_, value, pressed) in
            guard let self = self, self.isEnabled else { return }
            self.triggerValues = (self.triggerValues.0, value)
            let isDown = value > 0.3 || pressed
            if isDown && !self.rtDown { self.lastPressedButton = "RT / R2 (Fire / LeftClick)" }
            if isDown != self.rtDown {
                self.rtDown = isDown
                self.postPointer(0, 0, isDown ? 0x0002 : 0x0004, 0) // LEFTDOWN / LEFTUP
            }
        }

        // --- Thumbstick Buttons (L3 / R3) ---
        gamepad.leftThumbstickButton?.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "L3 / Left Stick Click (Ctrl)" }
            self.postKey(self.vkL3, pressed ? 1 : 0)
        }

        gamepad.rightThumbstickButton?.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "R3 / Right Stick Click (F)" }
            self.postKey(self.vkR3, pressed ? 1 : 0)
        }

        // --- Menu / Options / Pause Buttons ---
        gamepad.buttonMenu.pressedChangedHandler = { [weak self] (_, _, pressed) in
            guard let self = self, self.isEnabled else { return }
            if pressed { self.lastPressedButton = "Menu / Options (ESC)" }
            self.postKey(0x1B, pressed ? 1 : 0) // ESC
        }

        if let buttonOptions = gamepad.buttonOptions {
            buttonOptions.pressedChangedHandler = { [weak self] (_, _, pressed) in
                guard let self = self, self.isEnabled else { return }
                if pressed { self.lastPressedButton = "Share / View (ENTER)" }
                self.postKey(0x0D, pressed ? 1 : 0) // ENTER
            }
        }
    }

    private enum StickType {
        case left, right, dpad
    }

    private func handleStick(x: Float, y: Float, stickType: StickType) {
        let nextDir = snapDirection(x: x, y: y)
        let keys: [Int32]
        let currentDir: Int

        switch stickType {
        case .left:
            keys = [vkW, vkD, vkS, vkA] // WASD
            currentDir = leftStickDir
        case .right:
            keys = [vkUp, vkRight, vkDown, vkLeft] // Arrow keys
            currentDir = rightStickDir
        case .dpad:
            keys = [vkUp, vkRight, vkDown, vkLeft] // Arrow keys
            currentDir = dpadDir
        }

        guard nextDir != currentDir else { return }

        let oldKeys = Set(keysForDir(currentDir, keys))
        let newKeys = Set(keysForDir(nextDir, keys))

        for vk in oldKeys.subtracting(newKeys) {
            self.postKey(vk, 0)
        }
        for vk in newKeys.subtracting(oldKeys) {
            self.postKey(vk, 1)
        }

        switch stickType {
        case .left:  leftStickDir = nextDir
        case .right: rightStickDir = nextDir
        case .dpad:  dpadDir = nextDir
        }
    }

    /// 8-way directional snap: 0 = Up, 1 = Up-Right, 2 = Right, 3 = Down-Right, 4 = Down, 5 = Down-Left, 6 = Left, 7 = Up-Left
    private func snapDirection(x: Float, y: Float) -> Int {
        let mag = sqrt(x * x + y * y)
        if mag < deadzone { return -1 }

        var angle = atan2(Double(x), Double(y)) * 180.0 / .pi
        if angle < 0 { angle += 360.0 }
        return Int((angle + 22.5) / 45.0) % 8
    }

    private func keysForDir(_ dir: Int, _ q: [Int32]) -> [Int32] {
        switch dir {
        case 0: return [q[0]]          // Up
        case 1: return [q[0], q[1]]    // Up-Right
        case 2: return [q[1]]          // Right
        case 3: return [q[2], q[1]]    // Down-Right
        case 4: return [q[2]]          // Down
        case 5: return [q[2], q[3]]    // Down-Left
        case 6: return [q[3]]          // Left
        case 7: return [q[0], q[3]]    // Up-Left
        default: return []
        }
    }
}
