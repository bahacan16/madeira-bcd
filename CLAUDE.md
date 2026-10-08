# Instructions for coding agents (Codex, ChatGPT, Claude, anyone)

Reply to the owner in Turkish.

**Notes are private (owner's decision, 2026-10-02).** The handoff log and the
investigation write-ups are not in this public repository. They live in the
owner's private claude.ai page "Madeira Fork Notes"
(https://claude.ai/artifact/BHBKabmD2otb5Mr2TCSRSg), which only the owner's
account can open. Claude reads it with the Artifact tool (`action: read`,
`path: notes/HANDOFF.md`) and republishes the page after every change. Other
assistants: ask the owner for the current notes.

**Mandatory:** record every change, however small, in that private
`notes/HANDOFF.md`: what changed, why, the evidence (log time / build number)
and what is still open. Append; never delete earlier findings. Findings
without code changes are recorded too.

**Also mandatory (owner's decision, 2026-10-08):** keep the private
`notes/OYUN-AYARLARI.md` on the same page: for every build and game, the
config lines that worked (or failed) in a device test, newest build first,
nothing deleted.

**Keep progress out of this repository:** no handoff or investigation
documents in `docs/`, and commit messages stay one short line (no logs,
evidence or analysis).

Hard rules (do not break):

* Never commit Microsoft VC++ runtime DLLs (CI fetches them).
* Apple's Metal Shader Converter is handled as upstream does (owner's
  decision 2026-10-07): the iOS library is tracked at
  `app/Madeira/d3d12/libmetalirconverter.dylib` and the Apache-2.0 headers
  are vendored in `madeira-d3d12/third_party/metal-shader-converter`; both
  are pinned by hash in `build/madeira-d3d12/deps.sh`. Never commit Apple's
  installer package (.pkg/.dmg) or the macOS library.
* No public IPA releases without the owner's decision.
* Do not commit externally supplied binaries (exception: the 125hz PR
  #28/#29 DLLs).
* Never use tokens pasted in chat. Never commit or print the signing `.p12`,
  its password or the `.mobileprovision`; CI reads them from the private
  bucket.
* Never print secrets or install URLs in the public Actions logs.
