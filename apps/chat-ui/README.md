# @hermes/chat-ui

Private workspace for host-neutral Native Chat primitives. Desktop is the
current consumer; Web Native Chat becomes a consumer in UI-P3.

The public entry point exports `useIncrementalExternalStoreRuntime`, preserving
the existing assistant-ui adapter contract and incremental repository behavior.
The host supplies messages, callbacks and capabilities. This package manages
portable assistant-ui presentation state; the backend remains authoritative for
Agent and session semantics.

This package does not own Electron behavior, WebSocket transport, filesystem
access, SessionDB, Agent state, Desktop stores, or Web routing. It does not yet
provide a transcript renderer, composer, or host adapter framework.

React and assistant-ui are peers so the consumer and runtime share their
instances and contexts. Their versions follow the monorepo lockfile.

Run `npm run check --workspace apps/chat-ui` from the repository root for
typechecking, the ESLint portability boundary, and runtime behavior tests.
