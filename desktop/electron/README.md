# TrafficSuperVision Electron shell

This directory contains the hardened Electron boundary. The Vite renderer lives
outside this directory and can only reach the local bridge through the methods
exposed by `preload.cjs`.

Security invariants:

- `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true`.
- All popup, webview, permission, redirect, and unexpected navigation attempts
  are denied.
- The renderer never receives the bridge token, bridge port, local filesystem
  paths, `ipcRenderer`, process execution, shell execution, or COM access.
- Child environment variables and child logs stay in Electron main and are not
  included in renderer-visible bridge status.
- Main-process IPC handlers use an allowlist and strict payload validation.
- Native video selection returns an opaque UUID plus safe file metadata; absolute
  paths remain in an expiring main-process registry and are never serialized to
  the renderer.
- Local-file registration accepts only that opaque UUID from the renderer.
  Electron main resolves exactly one file and injects its path only into the
  authenticated loopback bridge request.
- The Python child is started with an argument array and `shell: false`.
- Production renderer assets use the private `trafficvisson://app` scheme with
  a strict CSP and no remote scripts.

Development from the desktop root:

```powershell
npm run dev:electron
```

The renderer URL is accepted only when it is loopback HTTP. Packaged builds are
expected to bundle `dist/client` plus `bridge` and an embedded Python runtime.
