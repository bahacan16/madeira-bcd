//
//  SessionUI.swift
//  Madeira
//
//  The game session as a player sees it, modelled on upstream's new UI (shown
//  in the Madeira Discord, not yet pushed): a launch screen with the game's
//  art until the first frame arrives, the game alone on screen, and one
//  Session panel for everything that used to be spread over the developer
//  view's button rows. Steam sign-in and downloads are deliberately left out.
//
//  The developer view is still there, under Settings > Developer tools; a
//  game started from the library no longer shows it.
//

import SwiftUI
import UIKit

// MARK: - Session state

/// What is running, and the player-facing switches that outlive one panel.
final class GameSession: ObservableObject {
    static let shared = GameSession()

    /// Title of the game this launch started (one Windows session per launch).
    @Published var title = ""

    /// The FPS readout beside the game. Persisted: a preference, not a state.
    @Published var showPerformance: Bool =
        UserDefaults.standard.object(forKey: GameSession.performanceKey) as? Bool ?? true {
        didSet { UserDefaults.standard.set(showPerformance, forKey: Self.performanceKey) }
    }

    private static let performanceKey = "madeira.session.performanceOverlay"
}

/// How the 1024x768 surface sits on the screen. MetalBackedView.gameRect()
/// reads this, and touch mapping uses the same rect, so input follows.
enum DisplayFit: String, CaseIterable, Identifiable {
    case fit
    case stretch

    var id: String { rawValue }

    var label: String {
        switch self {
        case .fit: return "Fit (4:3)"
        case .stretch: return "Stretch"
        }
    }

    private static let key = "madeira.display.fit"

    static var current: DisplayFit {
        get { DisplayFit(rawValue: UserDefaults.standard.string(forKey: key) ?? "") ?? .fit }
        set {
            UserDefaults.standard.set(newValue.rawValue, forKey: key)
            MetalBackedView.relayoutLive()
        }
    }
}

/// DXMT's pacing modes (g_madeira_vsync_mode). 1/0/2 are upstream's (the FPS
/// pill cycles through them); 3 and 4 are added in CI by
/// tools/patch-dxmt-frame-limits.py. Listed in the order the panel shows them.
enum FrameLimit: Int32, CaseIterable, Identifiable {
    case locked30 = 3
    case locked40 = 4
    case locked60 = 1
    case display = 0
    case unlimited = 2

    var id: Int32 { rawValue }

    var label: String {
        switch self {
        case .locked30: return "30 FPS"
        case .locked40: return "40 FPS"
        case .locked60: return "60 FPS"
        case .display: return "\(UIScreen.main.maximumFramesPerSecond) FPS"
        case .unlimited: return "Unlimited"
        }
    }

    static var current: FrameLimit { FrameLimit(rawValue: Int32(madeira_get_vsync_locked())) ?? .locked60 }

    /// 30 and 60 divide a 60 Hz panel evenly. 40 does not: 25 ms is three
    /// refreshes at 120 Hz but rounds to 33 ms at 60 Hz, so it needs the
    /// ProMotion rate held, as do the display-max and raw modes.
    static func wantsHighRefresh(_ mode: Int32) -> Bool {
        mode != FrameLimit.locked60.rawValue && mode != FrameLimit.locked30.rawValue
    }

    static func apply(_ limit: FrameLimit) {
        madeira_set_vsync_locked(Int32(limit.rawValue))
        ProMotionIntent.shared.setActive(wantsHighRefresh(limit.rawValue))
    }
}

// MARK: - Session panel

/// Everything a player adjusts mid-game. Shown as a sheet in portrait and as a
/// card on the touch-controls window in landscape (the game surface is a
/// window-level view, so nothing in the app window can draw over it).
struct SessionPanelView: View {
    var onClose: () -> Void

    @ObservedObject private var session = GameSession.shared
    @ObservedObject private var controls = TouchControlsModel.shared
    @ObservedObject private var input = InputSettings.shared
    @State private var frameLimit = FrameLimit.current
    @State private var fit = DisplayFit.current
    @State private var eco = madeira_get_eco() != 0

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Toggle("Touch controls", isOn: $controls.visible)
                    HStack {
                        Text("Opacity")
                        Slider(value: $controls.opacity, in: 0.2...1.0)
                    }
                    Button {
                        onClose()
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { controls.editing = true }
                    } label: {
                        Label("Edit controls", systemImage: "slider.horizontal.3")
                    }
                    Button {
                        onClose()
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { MetalBackedView.toggleKeyboard() }
                    } label: {
                        Label("Keyboard", systemImage: "keyboard")
                    }
                } header: {
                    Text("Controls")
                } footer: {
                    Text("Touch controls are drawn in landscape. A connected game controller reaches games as an Xbox pad.")
                }

                Section {
                    Picker("FPS limit", selection: $frameLimit) {
                        ForEach(FrameLimit.allCases) { Text($0.label).tag($0) }
                    }
                    .onChange(of: frameLimit) { _, value in FrameLimit.apply(value) }
                    Picker("Display fit", selection: $fit) {
                        ForEach(DisplayFit.allCases) { Text($0.label).tag($0) }
                    }
                    .onChange(of: fit) { _, value in DisplayFit.current = value }
                    Toggle("Performance overlay", isOn: $session.showPerformance)
                    Toggle("Battery saver (ECO)", isOn: $eco)
                        .onChange(of: eco) { _, on in madeira_set_eco(on ? 1 : 0) }
                } header: {
                    Text("Display")
                } footer: {
                    Text("ECO runs the game's threads at a low priority, on the efficiency cores. It is not a "
                         + "frame cap: use it while a game loads, so the phone keeps its heat budget for "
                         + "gameplay, and turn it off to play.")
                }

                Section {
                    Picker("Mode", selection: $input.relative) {
                        Text("Absolute").tag(false)
                        Text("Relative").tag(true)
                    }
                    .pickerStyle(.segmented)
                    sensitivityRow("Touch sensitivity", value: $input.sensAbs)
                    sensitivityRow("Mouse sensitivity", value: $input.sensRel)
                } header: {
                    Text("Mouse & pointer")
                } footer: {
                    Text(input.relative
                         ? "Relative: drag moves the pointer like a trackpad; games that capture the mouse get motion."
                         : "Absolute: the pointer goes where you touch.")
                }

                Section {
                    NavigationLink {
                        LiveLogView()
                    } label: {
                        Label("Live log", systemImage: "text.alignleft")
                    }
                    ShareLink(items: LogStore.shared.exportableLogs) {
                        Label("Share logs", systemImage: "square.and.arrow.up")
                    }
                }
            }
            .navigationTitle(session.title.isEmpty ? "Session" : session.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done", action: onClose)
                }
            }
        }
    }

    private func sensitivityRow(_ title: String, value: Binding<Double>) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(title)
                Spacer()
                Text(String(format: "%.2f", value.wrappedValue))
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Slider(value: value, in: 0.10...8.0)
        }
    }
}

// MARK: - Live log

struct LiveLogView: View {
    @ObservedObject private var log = LogStore.shared

    var body: some View {
        LiveLogList(limit: 200)
            .navigationTitle("Live log")
            .navigationBarTitleDisplayMode(.inline)
    }
}

/// Newest first, the raw last line of each bucket.
struct LiveLogList: View {
    let limit: Int
    @ObservedObject private var log = LogStore.shared

    var body: some View {
        let rows = Array(log.entries.sorted { $0.lastTimestamp > $1.lastTimestamp }.prefix(limit))
        List(rows) { entry in
            Text(entry.lastRaw)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(entry.level == .error ? Color.red : Color.primary)
                .lineLimit(3)
                .listRowInsets(EdgeInsets(top: 2, leading: 8, bottom: 2, trailing: 8))
                .listRowBackground(Color.clear)
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
    }
}

// MARK: - Launch screen

/// A window above everything (game surface and touch controls included) that
/// covers the start-up: Wine, FEX and the game's own loading, which take from
/// seconds to minutes and used to be a black strip under a scrolling log.
enum LaunchSplashHost {
    private static var window: UIWindow?
    private static var hiding = false

    static func show(title: String) {
        guard window == nil else { return }
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        guard let scene = scenes.first(where: { $0.activationState == .foregroundActive }) ?? scenes.first
        else { return }
        let w = UIWindow(windowScene: scene)
        // Above the touch-controls window (+101) and the joystick pad (+100).
        w.windowLevel = .normal + 102
        w.backgroundColor = .black
        let host = UIHostingController(rootView: LaunchSplashView(title: title, onDismiss: { LaunchSplashHost.hide() }))
        host.view.backgroundColor = .black
        w.rootViewController = host
        w.isHidden = false
        window = w
    }

    static func hide() {
        guard let w = window, !hiding else { return }
        hiding = true
        UIView.animate(withDuration: 0.35, animations: { w.alpha = 0 }, completion: { _ in
            w.isHidden = true
            LaunchSplashHost.window = nil
            LaunchSplashHost.hiding = false
        })
    }
}

struct LaunchSplashView: View {
    let title: String
    let onDismiss: () -> Void

    @ObservedObject private var log = LogStore.shared
    @State private var showLog = false
    @State private var ticks = 0
    @State private var framesSeen = 0
    @State private var windowSeenAt: Int?
    private let timer = Timer.publish(every: 0.25, on: .main, in: .common).autoconnect()

    var body: some View {
        ZStack {
            background
            VStack(spacing: 14) {
                Spacer(minLength: 20)
                cover
                Text(title)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(.white)
                HStack(spacing: 8) {
                    ProgressView().tint(.white)
                    Text(status)
                        .font(.footnote)
                        .foregroundStyle(.white.opacity(0.75))
                        .lineLimit(1)
                }
                Text(elapsed)
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.white.opacity(0.5))
                HStack(spacing: 12) {
                    pill(showLog ? "Hide live log" : "Show live log", icon: "text.alignleft") { showLog.toggle() }
                    pill("Skip", icon: "forward.end") { onDismiss() }
                }
                if showLog {
                    LiveLogList(limit: 80)
                        .frame(maxWidth: 560, maxHeight: 260)
                        .background(.black.opacity(0.45), in: RoundedRectangle(cornerRadius: 12))
                }
                Spacer(minLength: 20)
                Text("The launch screen closes when the game shows its first frame.")
                    .font(.caption2)
                    .foregroundStyle(.white.opacity(0.45))
                    .padding(.bottom, 12)
            }
            .padding(.horizontal, 20)
        }
        .ignoresSafeArea()
        .onReceive(timer) { _ in check() }
    }

    /// First frame closes it; so does a window that has been up for 8s without
    /// presenting anything (desktop and GDI programs never present a frame).
    private func check() {
        ticks += 1
        if madeira_get_present_count() > 0 {
            framesSeen += 1
            if framesSeen >= 2 { onDismiss() }
            return
        }
        if windowSeenAt == nil, log.entries.contains(where: { $0.lastRaw.contains("[win-pos]") }) {
            windowSeenAt = ticks
        }
        if let t = windowSeenAt, ticks - t >= 32 { onDismiss() }
    }

    private var status: String {
        guard let last = log.entries.max(by: { $0.lastTimestamp < $1.lastTimestamp }) else { return "Starting…" }
        return last.lastRaw.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var elapsed: String {
        let s = ticks / 4
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    @ViewBuilder private var background: some View {
        if let img = CoverStore.image(for: title) {
            Image(uiImage: img)
                .resizable()
                .scaledToFill()
                .blur(radius: 40)
                .opacity(0.45)
                .overlay(Color.black.opacity(0.35))
                .ignoresSafeArea()
        } else {
            LinearGradient(colors: CoverArt.palette(title), startPoint: .topLeading, endPoint: .bottomTrailing)
                .opacity(0.55)
                .overlay(Color.black.opacity(0.45))
                .ignoresSafeArea()
        }
    }

    @ViewBuilder private var cover: some View {
        if let img = CoverStore.image(for: title) {
            Image(uiImage: img)
                .resizable()
                .scaledToFit()
                .frame(maxWidth: 150, maxHeight: 200)
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                .shadow(radius: 18)
        } else {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(LinearGradient(colors: CoverArt.palette(title), startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 130, height: 170)
                .overlay(Text(CoverArt.initials(title)).font(.largeTitle.weight(.bold)).foregroundStyle(.white))
                .shadow(radius: 18)
        }
    }

    private func pill(_ text: String, icon: String, _ action: @escaping () -> Void) -> some View {
        Label(text, systemImage: icon)
            .font(.footnote.weight(.medium))
            .foregroundStyle(.white)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(.white.opacity(0.15), in: Capsule())
            .contentShape(Capsule())
            .onTapGesture(perform: action)
    }
}
