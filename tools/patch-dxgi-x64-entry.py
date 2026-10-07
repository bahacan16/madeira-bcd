#!/usr/bin/env python3
"""Add opt-in, typed x64 patchable entries to a copy of DXMT's factory.

The original factory is still instantiated unless MADEIRA_X64_GRAPHICS_ENTRY=1.
Only the overridden methods get clang's ARM64EC hybrid_patchable entries;
the adapter lookup, capabilities and all method implementations are preserved.
"""
from pathlib import Path
import re
import sys

MARKER = '/* madeira x64 graphics factory begin */'

FACTORY = r'''
/* madeira x64 graphics factory begin */
class MTLDXGIHookFactory : public MTLDXGIFactory {
public:
  explicit MTLDXGIHookFactory(UINT flags) : MTLDXGIFactory(flags) {}
  void InitializeEntries() {
    void **original, **replacement = entry_vtable_ + 2;
    void *com_object = static_cast<IDXGIFactory7 *>(this);
    __builtin_memcpy(&original, com_object, sizeof(original));
    mad_x64_graphics_factory_table(original, entry_vtable_, &__ImageBase);
    __builtin_memcpy(com_object, &replacement, sizeof(replacement));
  }
  MAD_X64_GRAPHICS_ENTRY HRESULT STDMETHODCALLTYPE
  MakeWindowAssociation(HWND window, UINT flags) override;
  MAD_X64_GRAPHICS_ENTRY HRESULT STDMETHODCALLTYPE
  CreateSwapChain(IUnknown *device, DXGI_SWAP_CHAIN_DESC *desc,
                  IDXGISwapChain **out) override;
  MAD_X64_GRAPHICS_ENTRY HRESULT STDMETHODCALLTYPE
  CreateSwapChainForHwnd(IUnknown *device, HWND window,
      const DXGI_SWAP_CHAIN_DESC1 *desc, const DXGI_SWAP_CHAIN_FULLSCREEN_DESC *fs,
      IDXGIOutput *restrict_output, IDXGISwapChain1 **out) override;
  MAD_X64_GRAPHICS_ENTRY HRESULT STDMETHODCALLTYPE
  CreateSwapChainForCoreWindow(IUnknown *device, IUnknown *window,
      const DXGI_SWAP_CHAIN_DESC1 *desc, IDXGIOutput *restrict_output,
      IDXGISwapChain1 **out) override;
  MAD_X64_GRAPHICS_ENTRY HRESULT STDMETHODCALLTYPE
  CreateSwapChainForComposition(IUnknown *device, const DXGI_SWAP_CHAIN_DESC1 *desc,
      IDXGIOutput *restrict_output, IDXGISwapChain1 **out) override;
private:
  void *entry_vtable_[36];
};

HRESULT STDMETHODCALLTYPE
MTLDXGIHookFactory::MakeWindowAssociation(HWND window, UINT flags) {
  return MTLDXGIFactory::MakeWindowAssociation(window, flags);
}
HRESULT STDMETHODCALLTYPE
MTLDXGIHookFactory::CreateSwapChain(IUnknown *device, DXGI_SWAP_CHAIN_DESC *desc,
                                 IDXGISwapChain **out) {
  return MTLDXGIFactory::CreateSwapChain(device, desc, out);
}
HRESULT STDMETHODCALLTYPE
MTLDXGIHookFactory::CreateSwapChainForHwnd(IUnknown *device, HWND window,
    const DXGI_SWAP_CHAIN_DESC1 *desc, const DXGI_SWAP_CHAIN_FULLSCREEN_DESC *fs,
    IDXGIOutput *restrict_output, IDXGISwapChain1 **out) {
  return MTLDXGIFactory::CreateSwapChainForHwnd(device, window, desc, fs, restrict_output, out);
}
HRESULT STDMETHODCALLTYPE
MTLDXGIHookFactory::CreateSwapChainForCoreWindow(IUnknown *device, IUnknown *window,
    const DXGI_SWAP_CHAIN_DESC1 *desc, IDXGIOutput *restrict_output,
    IDXGISwapChain1 **out) {
  return MTLDXGIFactory::CreateSwapChainForCoreWindow(device, window, desc, restrict_output, out);
}
HRESULT STDMETHODCALLTYPE
MTLDXGIHookFactory::CreateSwapChainForComposition(IUnknown *device,
    const DXGI_SWAP_CHAIN_DESC1 *desc, IDXGIOutput *restrict_output,
    IDXGISwapChain1 **out) {
  return MTLDXGIFactory::CreateSwapChainForComposition(device, desc, restrict_output, out);
}
/* madeira x64 graphics factory end */

'''


def patch(src):
    if MARKER in src:
        return src
    include = '#include "Metal.hpp"\n'
    if src.count(include) != 1:
        raise ValueError('factory include anchor missing or ambiguous')
    src = src.replace(include, include + '#include "madeira_graphics_entry.h"\n')
    for method in ('MakeWindowAssociation', 'CreateSwapChain', 'CreateSwapChainForHwnd',
                   'CreateSwapChainForCoreWindow', 'CreateSwapChainForComposition'):
        pattern = rf'(\b{method}\s*\([^{{;]*?\))\s+final\s*\{{'
        src, count = re.subn(pattern, r'\1 override {', src)
        if count != 1:
            raise ValueError(f'{method}: expected one final method, found {count}')
    anchor = 'extern "C" HRESULT __stdcall CreateDXGIFactory2('
    if src.count(anchor) != 1:
        raise ValueError('CreateDXGIFactory2 anchor missing or ambiguous')
    src = src.replace(anchor, FACTORY + anchor)
    allocate = 'MTLDXGIFactory* factory = new MTLDXGIFactory(Flags);'
    if src.count(allocate) != 1:
        raise ValueError('factory allocation anchor missing or ambiguous')
    src = src.replace(allocate, '''const bool patchable = mad_x64_graphics_entry_enabled();
    MTLDXGIFactory* factory;
    if (patchable) {
      auto* hook_factory = new MTLDXGIHookFactory(Flags);
      hook_factory->InitializeEntries();
      factory = hook_factory;
    } else {
      factory = new MTLDXGIFactory(Flags);
    }
    static std::atomic<bool> noted{false};
    if (patchable && !noted.exchange(true))
      Logger::info("[graphics-entry] x64 patchable DXGI factory methods enabled (PE entries)");''')
    return src


def main():
    path = Path(sys.argv[1])
    try:
        before = path.read_text()
        after = patch(before)
    except (OSError, ValueError) as e:
        print(f'patch-dxgi-x64-entry: {e}', file=sys.stderr)
        return 1
    if after != before:
        path.write_text(after)
    print('patch-dxgi-x64-entry: ' + ('applied' if after != before else 'already patched'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
