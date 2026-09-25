import SwiftUI
import UIKit
import QuartzCore
import os

/// Keeps the ProMotion panel promoted to 120Hz while MAX mode is on.
/// CAMetalLayer presents alone don't express frame-rate intent — iOS
/// parks the display at 60Hz and only promotes on touch (observed
/// 2026-07-05: MAX mode ran 60 except ~119 bursts while touching). An
/// active CADisplayLink with preferredFrameRateRange(120) is the
/// documented way for present-driven Metal apps to hold the panel at
/// 120. The tick itself does nothing.
final class ProMotionIntent {
    static let shared = ProMotionIntent()
    private var link: CADisplayLink?

    func setActive(_ active: Bool) {
        if active {
            guard link == nil else { return }
            let l = CADisplayLink(target: self, selector: #selector(tick))
            l.preferredFrameRateRange = CAFrameRateRange(minimum: 60, maximum: 120, preferred: 120)
            l.add(to: .main, forMode: .common)
            link = l
        } else {
            link?.invalidate()
            link = nil
        }
    }

    @objc private func tick(_ sender: CADisplayLink) {}
}

/// Small overlay shown over the Metal render view. Reads DXMT's present
/// counter at 100ms intervals into a 5s rolling buffer, displays current
/// count + smoothed FPS computed over an adaptive window.
///
/// Adaptive display logic:
///   - Backend always samples every 100ms (50 samples in the 5s buffer).
///   - Displayed FPS uses a window long enough to contain ≥ ~3 frame samples,
///     so the readout is stable at any rate. At 60fps the window is ~100ms;
///     at 1fps it's ~3s.
///   - Display value refreshes every 250ms regardless.
///   - Tap to hide.
struct FPSOverlay: View {
    /// Compact = landscape side-bar variant: FPS + pacing pill stacked
    /// vertically, no present counter (fits a ~120pt pillarbox bar).
    var compact: Bool = false
    @State private var presentCount: UInt64 = 0
    @State private var fps: Double = 0
    @State private var visible: Bool = true
    @State private var timer: Timer? = nil
    @State private var displayTimer: Timer? = nil
    /// Mirrors DXMT's g_madeira_vsync_mode (read per present, live-safe).
    /// 1 = locked 60, 0 = display max (120 ProMotion), 2 = raw (frame-skip
    /// mailbox — game unthrottled, panel shows ≤ display rate).
    @State private var vsyncMode: Int32 = 1
    /// Ring buffer of (timestamp, count) pairs, 100ms cadence, 5s window.
    @State private var samples: [(t: CFAbsoluteTime, c: UInt64)] = []
    private let bufferCapacity = 50  // 5s @ 100ms
    /// ml606: live phys_footprint in MB, refreshed on the 250ms display tick.
    @State private var memMB: Int = 0

    /// The ceiling jetsam enforces ON THIS DEVICE, measured rather than
    /// assumed. 4096 was written down from one machine ("Jetsam = EXACTLY
    /// 4096MB"), but iOS grants the increased-memory-limit allowance per
    /// device class, so on other hardware that constant is simply wrong --
    /// and a wrong ceiling misplaces every colour threshold below. Too low
    /// is merely pessimistic; too high is dangerous, because the bar would
    /// still read green while the app is about to be killed without warning
    /// (ml605 died at 4080MB with nothing in the log).
    ///
    /// os_proc_available_memory() reports the bytes left before that kill,
    /// against the same phys_footprint counter readFootprintMB() reads, so
    /// footprint + available IS the limit. 4096 stays as the fallback for
    /// when the call reports nothing.
    @State private var limitMB: Int = 4096
    @State private var logTicks = 0
    @State private var lastLoggedPresent: UInt64 = .max

    private func readLimitMB(footprintMB: Int) -> Int {
        let avail = os_proc_available_memory()
        guard avail > 0 else { return limitMB }
        return footprintMB + Int(avail / (1024 * 1024))
    }

    private func readFootprintMB() -> Int {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<natural_t>.size)
        let kr = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        guard kr == KERN_SUCCESS else { return 0 }
        return Int(info.phys_footprint / (1024 * 1024))
    }

    /// Headroom-based, because the absolute number means nothing without the
    /// ceiling: green >768MB free, yellow >384MB, orange >128MB, red below.
    private var memColor: Color {
        let free = limitMB - memMB
        if memMB == 0 { return .secondary }
        if free > 768 { return .green }
        if free > 384 { return .yellow }
        if free > 128 { return .orange }
        return .red
    }

    var body: some View {
        Group {
            if visible && compact {
                VStack(spacing: 4) {
                    Text(String(format: "%.1f", fps))
                        .foregroundColor(fpsColor)
                    pacingPill
                    capturePill
                    ecoPill
                    fencePill
                }
                .font(.system(.caption, design: .monospaced))
                .padding(6)
                .background(Color.black.opacity(0.55))
                .cornerRadius(6)
            } else if visible {
                HStack(spacing: 8) {
                    // ml606: live phys_footprint — the SAME number jetsam kills on.
                    // ml605 died at 4080MB against a 4096MB limit with no warning
                    // of any kind in the log, so having it on screen turns "it
                    // vanished" into "we watched it climb".
                    // verbatim: SwiftUI's Text("\(Int)") goes through
                    // LocalizedStringKey, which applies the locale's grouping
                    // separator -- a 6655MB ceiling rendered as "6.655MB" on a
                    // Turkish device, i.e. it reads as 6.6MB, the opposite of
                    // the headroom it is reporting.
                    Text(verbatim: "\(memMB)/\(limitMB)MB")
                        .foregroundColor(memColor)
                        .frame(width: 92, alignment: .trailing)
                    Text("|")
                        .foregroundColor(.secondary)
                    Text("Present:")
                        .foregroundColor(.secondary)
                    Text("\(presentCount)")
                        .foregroundColor(.primary)
                    Text("|")
                        .foregroundColor(.secondary)
                    Text("FPS:")
                        .foregroundColor(.secondary)
                    Text(String(format: "%.1f", fps))
                        .foregroundColor(fpsColor)
                        .frame(width: 40, alignment: .trailing)
                    pacingPill
                    capturePill
                    ecoPill
                    fencePill
                }
                .font(.system(.caption, design: .monospaced))
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.black.opacity(0.55))
                .cornerRadius(6)
            } else {
                Circle()
                    .fill(Color.black.opacity(0.3))
                    .frame(width: 12, height: 12)
            }
        }
        .onTapGesture { visible.toggle() }
        .onAppear { startTimers() }
        .onDisappear { stopTimers() }
    }

    /// Pacing pill, cycles 60 → MAX(n) → RAW → 60. Shared by the wide
    /// (portrait) and compact (landscape bar) overlay variants.
    ///   60: presents paced to exactly 60Hz.
    ///   MAX(n): free-run to display refresh; n = current cap
    ///     (120 = ProMotion; 60 = thermal/LPM capped).
    ///   RAW: game unthrottled (frame-skip mailbox) — FPS readout =
    ///     raw stack throughput.
    private var pacingPill: some View {
        Text(pillLabel)
            .foregroundColor(pillColor)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .overlay(RoundedRectangle(cornerRadius: 4)
                .stroke(pillColor, lineWidth: 1))
            .onTapGesture {
                vsyncMode = vsyncMode == 1 ? 0 : (vsyncMode == 0 ? 2 : 1)
                madeira_set_vsync_locked(vsyncMode)
                ProMotionIntent.shared.setActive(FrameLimit.wantsHighRefresh(vsyncMode))
            }
    }

    /// ml1098: one tap = capture the next frame (every render pass's attachments
    /// to Documents/capture/, plus the full draw-dump in the log). The pill
    /// flashes for a second so a tap is visibly taken.
    @State private var captureFlash = false
    private var capturePill: some View {
        Text("CAP")
            .foregroundColor(captureFlash ? .black : .cyan)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(captureFlash ? Color.cyan : Color.clear)
            .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.cyan, lineWidth: 1))
            .onTapGesture {
                madeira_capture_request(1)
                captureFlash = true
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { captureFlash = false }
            }
    }

    /// ml1133: ECO. The SoC clamps the CPU clock once ~250 J of CPU energy has
    /// been spent above ~2.3 W, and a loading screen at full clock spends nearly
    /// all of it before gameplay starts. ECO on = guest threads run at a low QoS
    /// class (efficiency cores, lower clocks): loading is slower but keeps the
    /// budget for gameplay. Turn it off once in game. Green = on.
    @State private var ecoOn = madeira_get_eco() != 0
    private var ecoPill: some View {
        Text("ECO")
            .foregroundColor(ecoOn ? .black : .green)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(ecoOn ? Color.green : Color.clear)
            .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.green, lineWidth: 1))
            .onTapGesture {
                ecoOn.toggle()
                madeira_set_eco(ecoOn ? 1 : 0)
            }
    }

    /// ml1136: GPU encoder-sync mode, switchable live for in-place A/B tests.
    /// F1 = every encoder waits for the one before (accurate, default),
    /// F6 = barrier-driven, F5 = render passes wait at the fragment stage,
    /// F0 = no fences at all (diagnostic ceiling; expect flicker).
    // ml1137: the overlay view is recreated on layout changes, which reset a plain
    // @State to the config value (ph-rdr93: taps re-requested F6 three times).
    // The mode lives in a static so it survives, and @State mirrors it for redraws.
    @State private var fenceMode: Int = FPSOverlayFenceMode.current
    private var fencePill: some View {
        let color: Color = fenceMode == 1 ? .white : fenceMode == 6 ? .purple : fenceMode == 5 ? .blue : .red
        return Text("F\(fenceMode)")
            .foregroundColor(color)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .overlay(RoundedRectangle(cornerRadius: 4).stroke(color, lineWidth: 1))
            .onTapGesture {
                fenceMode = FPSOverlayFenceMode.current
                fenceMode = fenceMode == 1 ? 6 : fenceMode == 6 ? 5 : fenceMode == 5 ? 0 : 1
                FPSOverlayFenceMode.current = fenceMode
                madeira_set_fence_mode(Int32(fenceMode == 0 ? 7 : fenceMode))
            }
    }

    private var pillLabel: String {
        switch vsyncMode {
        case 1: return "60"
        case 3: return "30"
        case 4: return "40"
        case 0: return "MAX(\(UIScreen.main.maximumFramesPerSecond))"
        default: return "RAW"
        }
    }

    private var pillColor: Color {
        switch vsyncMode {
        case 1, 3, 4: return .cyan
        case 0: return .pink
        default: return .orange
        }
    }

    private var fpsColor: Color {
        if fps >= 50 { return .green }
        if fps >= 30 { return .yellow }
        if fps >= 1  { return .orange }
        if fps > 0   { return Color(red: 1.0, green: 0.4, blue: 0.2) }
        return .secondary
    }

    private func startTimers() {
        stopTimers()
        let now = CFAbsoluteTimeGetCurrent()
        let c = madeira_get_present_count()
        samples = [(now, c)]
        presentCount = c
        vsyncMode = madeira_get_vsync_locked()
        ProMotionIntent.shared.setActive(FrameLimit.wantsHighRefresh(vsyncMode))

        // 100ms sampling — keeps the buffer fresh
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
            let t = CFAbsoluteTimeGetCurrent()
            let cur = madeira_get_present_count()
            samples.append((t, cur))
            if samples.count > bufferCapacity { samples.removeFirst() }
            presentCount = cur
        }

        // 250ms display refresh — computes adaptive-window FPS
        memMB = readFootprintMB()
        limitMB = readLimitMB(footprintMB: memMB)
        displayTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in
            fps = computeAdaptiveFPS()
            // The Session panel sets the pacing mode too; follow it.
            vsyncMode = madeira_get_vsync_locked()
            // ml606: piggybacks on the existing tick, so it costs one extra
            // task_info per 250ms and no additional SwiftUI invalidation.
            memMB = readFootprintMB()
            limitMB = readLimitMB(footprintMB: memMB)
            // The present counter answers "did a frame ever reach the screen",
            // and it only lived on the HUD -- which switches to its compact
            // form (FPS only) exactly when a title starts drawing, so the
            // number is gone at the moment it matters, and a run that renders
            // nothing is indistinguishable from one that renders black. Put it
            // in the log too: every 2s, and only when it changes or is still
            // zero, so a steady run costs one line and a dead one is obvious.
            logTicks += 1
            if logTicks % 8 == 0 {
                let c = presentCount
                if c != lastLoggedPresent || c == 0 {
                    LogStore.shared.log("[present] count=\(c) fps=\(String(format: "%.1f", fps)) mem=\(memMB)/\(limitMB)MB",
                                        level: c == 0 ? .debug : .info)
                    lastLoggedPresent = c
                }
            }
        }
    }

    private func stopTimers() {
        timer?.invalidate()
        timer = nil
        displayTimer?.invalidate()
        displayTimer = nil
    }

    /// Compute FPS over an adaptive window: starting from the newest sample,
    /// walk backwards until the window holds ≥3 presents AND spans ≥1s (or we
    /// hit buffer start). The 1s minimum matters: with 100ms sampling, a
    /// short window quantizes the readout to presents/0.2s = multiples of
    /// 5.0 — at a true ~19 FPS it displayed a rock-steady "20.0" (4 presents
    /// per 0.2s) with "dips" to 15.0, which read as an artificial frame lock
    /// (2026-07-04, cost a day of pacing-hunt confusion). ≥1s gives 1-FPS
    /// resolution; still responsive for a debug readout.
    private func computeAdaptiveFPS() -> Double {
        guard samples.count >= 2 else { return 0 }
        let latest = samples.last!
        // Walk backwards
        var oldest = samples[0]
        for i in (0..<samples.count).reversed() {
            let candidate = samples[i]
            let delta = latest.c &- candidate.c
            let span = latest.t - candidate.t
            if delta >= 3 && span >= 1.0 {
                oldest = candidate
                break
            }
            oldest = candidate
        }
        let dt = latest.t - oldest.t
        let dc = latest.c &- oldest.c
        guard dt > 0.0001 else { return 0 }
        return Double(dc) / dt
    }
}

/// ml1137: process-wide fence-mode display state for the overlay pill.
enum FPSOverlayFenceMode {
    static var current: Int = Int(MadeiraConfig.get("fence-chain") ?? "1") ?? 1
}
