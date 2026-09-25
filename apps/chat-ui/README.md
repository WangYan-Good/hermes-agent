# @hermes/chat-ui

Host-neutral chat presentation shared by Desktop and Web Native
Chat. The backend remains authoritative for sessions, turns, tools and approvals.

## Shared surface

- `ChatTranscriptPart`, `ToolPresentation`, `ContentReference`, `ContentArtifact`
  contain data only. Transport authority, `File`, `Blob` and callbacks are not
  persisted in these objects.
- `RichMarkdown` uses Streamdown, the Desktop Markdown preprocessing and block
  cache, memoized math, lazy code highlighting and artifact detection.
- `ArtifactView`, `MediaView`, `ReferenceView`, `ToolResultView` and
  `BoundedOutput` render portable content. `ChatHostContext` supplies a
  `ChatHostAdapter` for opening, authenticated media resolution/release, preview
  and download. Cards never open a window merely by mounting.
- Desktop consumes the shared preprocessing, block parsing, artifact detection,
  math cache, lazy code plugin and diff parser through focused package exports.
  Its existing preview rail, stores, Electron bridge and renderer remain owned
  by Desktop. Web supplies authenticated HTTP, browser downloads and source-only
  artifact previews. Import `@hermes/chat-ui/styles.css` for the portable renderer.
- `useIncrementalExternalStoreRuntime` preserves the assistant-ui incremental
  repository contract. React and assistant-ui are peers to share contexts.

Pure artifact IDs use durable turn/message identity plus content position. Tool
media IDs use the real tool-call ID and output index. The package does not infer
identity from a tool's prose, and missing historical diffs are not reconstructed.

## Boundaries and budgets

No Electron, Node filesystem, Desktop stores, host bridge globals or Web routes
may enter this package. ESLint and AST-based dependency tests enforce this for
static and dynamic imports. Host actions receive data, never executable markup.

Web Markdown skips raw HTML, allows only HTTP(S) links without credentials, and
never executes SVG, HTML artifacts, Mermaid or arbitrary embeds. Media resources
are resolved and released by the host, including responses arriving after
unmount. Output and diffs page at 200 lines or 32 KiB UTF-8; Markdown beyond
256 KiB falls back to bounded text. Collapsed outputs do not load highlighters.

Run `npm run check --workspace apps/chat-ui` in an isolated development container
for typechecking, portability lint and behavior tests. See the developer
[architecture documentation](../../website/docs/developer-guide/architecture.md)
for the attachment protocol and recovery contract.
