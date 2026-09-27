import { Component, type ReactNode } from 'react';
interface Props { children: ReactNode; onFailure: () => void }
export class ChatSurfaceBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch() { this.props.onFailure(); }
  render() { return this.state.failed ? <p role="alert">The chat interface could not load.</p> : this.props.children; }
}
