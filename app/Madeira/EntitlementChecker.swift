import Foundation
import Security

private typealias SecTaskRef = OpaquePointer

@_silgen_name("SecTaskCopyValueForEntitlement")
private func _SecTaskCopyValueForEntitlement(
    _ task: SecTaskRef,
    _ entitlement: NSString,
    _ error: NSErrorPointer
) -> CFTypeRef?

@_silgen_name("SecTaskCreateFromSelf")
private func _SecTaskCreateFromSelf(
    _ allocator: CFAllocator?
) -> SecTaskRef?

func checkAppEntitlement(_ ent: String) -> Bool {
    guard let task = _SecTaskCreateFromSelf(nil) else { return false }

    guard let value = _SecTaskCopyValueForEntitlement(task, ent as NSString, nil) else {
        return false
    }

    if let number = value as? NSNumber {
        return number.boolValue
    }

    return false
}

struct EntitlementStatus {
    let jitAllowed: Bool
    let increasedMemory: Bool
    let extendedVA: Bool
    let increasedDebugMemory: Bool

    static func check() -> EntitlementStatus {
        EntitlementStatus(
            jitAllowed: checkAppEntitlement("com.apple.security.cs.allow-jit"),
            increasedMemory: checkAppEntitlement("com.apple.developer.kernel.increased-memory-limit"),
            extendedVA: checkAppEntitlement("com.apple.developer.kernel.extended-virtual-addressing"),
            increasedDebugMemory: checkAppEntitlement("com.apple.developer.kernel.increased-debugging-memory-limit")
        )
    }
}

/* Runtime check: is a debugger attached to this process (P_TRACED)?
 * This is the signal StikDebug JIT actually rides on — CS_DEBUGGED gets
 * set while traced, enabling JIT-region execution. The allow-jit
 * ENTITLEMENT is macOS-only and never granted on iOS, so the old badge
 * built on it was permanently ✗ no matter what StikDebug did. */
func isDebuggerAttached() -> Bool {
    // CS_DEBUGGED first, because that is what JIT actually rides on.
    // StikDebug attaches, sets CS_DEBUGGED, and then DETACHES; P_TRACED
    // goes back to false at that moment while CS_DEBUGGED persists, and
    // CS_DEBUGGED is the flag the kernel consults before allowing an RX
    // mapping. Asking P_TRACED alone made the badge sit orange through a
    // run where JIT was demonstrably working -- the 896MB pool allocated,
    // Wine up, desktop drawing -- which is worse than no badge, since it
    // says "broken" about the one thing the user came to check.
    if jit_is_debugged_quiet() { return true }
    // Keep P_TRACED as the second term: it is true during the brief window
    // while the debugger is still attached, before CS_DEBUGGED is set.
    var info = kinfo_proc()
    var size = MemoryLayout<kinfo_proc>.stride
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, getpid()]
    let ret = sysctl(&mib, UInt32(mib.count), &info, &size, nil, 0)
    guard ret == 0 else { return false }
    return (info.kp_proc.p_flag & P_TRACED) != 0
}
