/// <reference types="vite/client" />
/// <reference types="google.maps" />
/// <reference types="node" />
// `google.maps` namespace is exposed via @types/google.maps. tsc 5.x
// auto-loads all @types/* packages, but tsgo (TypeScript 7 native
// preview) does not pick the namespace up reliably under a symlinked
// node_modules layout (pnpm before, bun's isolated linker now) — the
// explicit reference here is the
// canonical fix per https://developers.google.com/maps/documentation/javascript/using-typescript
// and works for both compilers.

interface ImportMetaEnv {
  readonly VITE_ANDROID_RELAY_URL: string;
  readonly VITE_CLIENT_PORT: string;
  readonly VITE_PYTHON_BACKEND_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Allow importing SVG files as raw strings
declare module '*.svg?raw' {
  const content: string;
  export default content;
}
