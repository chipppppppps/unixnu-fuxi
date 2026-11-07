# Upstream question for ljhhkx529/unixnu-fuxi: how is the first prelink candidate produced?

Title: `build/native-prelink/*-candidate.macho` cannot be produced from a clean tree — what is generation 0?

Body:

I am trying to reproduce the `fuxi_full_defconfig` build on a GitHub Actions `macos-26`
runner (Xcode 26.6, `UniXNU/UniXNU@main` + `ljhhkx529/unixnu-fuxi@old` +
`ljhhkx529/uefi-fuxi@old`, `drivers/QcomXnuUsbDebug` repointed to
`xiaoleGun/QcomXnuUsbDebug@e2e2b7c7`).

`./unixnu build kernel` stops in the archive chain because these recipes read a
previous build's output from `build/native-prelink/`, and `build/` is pruned from
the repository (there is no binary larger than 200 KB in the tree, and neither
the `old`, `main` nor `V2` branch carries one):

- `scripts/build-sheng-platform-power.py:91` — `native-owner-ufs-rootauth3-candidate.macho`
- `scripts/build-native-legacy-display.py:111` — `native-owner-mobile-graphics-candidate.macho`
- `scripts/build-native-aes-bindings.py:62` — `native-defaults-candidate.macho` (+ `.json` plan)
- `scripts/build-native-hid-input.py:17` — `native-owner-ufs-cpu-desktop-v9-hid-init-candidate.macho`

I tried the documented escape hatch in `scripts/build-legacy-stub.py` (Stage A: link a
kernel without the storage and display stacks, then prelink it) and got a valid
candidate out of `kernel.prelink`:

```
Experimental candidate: 51818496 bytes; executable bytes unchanged; runtime unqualified
```

but it cannot serve as the reference: `build-native-legacy-display.py:105` needs the
candidate to carry relocations for the 358-entry `IOMobileFramebufferLegacy` vtable
(`compiled=[relocations.get(vtable+16+i*8) for i in range(358)]`,
`require(all(compiled), ...)`), and a kernel linked with stubs has no such relocations —
`IOMobileFramebufferLegacy` and `ApplePMGR` only appear in `__PRELINK_INFO`/kext text,
never as relocations or mangled symbols.

So generation 0 seems impossible to derive from public sources. Could you either

1. attach (or link) one `native-owner-ufs-cpu-desktop-*-candidate.macho` + its
   `.json` plan so the chain can bootstrap, or
2. describe the order in which the archives were built the first time
   (which recipe produced the first candidate, and what was stubbed/disabled for it)?

Reproduction: <run link>, workflow `fuxi t50 stage A (macOS)` produces the prelink
candidate above; `fuxi t50 (macOS)` stops at archive-11 with
`No reference candidate available`.
