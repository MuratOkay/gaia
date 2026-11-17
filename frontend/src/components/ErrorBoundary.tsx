// A page that throws while rendering shows what happened — and the navigation stays — instead
// of leaving the whole window blank (D76: without a boundary React unmounts everything).
import { Component, type ErrorInfo, type ReactNode } from "react";

import i18n from "../i18n";

interface Props {
  children: ReactNode;
  /** when this changes (the page's address), the boundary tries again */
  resetKey?: string;
}

interface State {
  error: Error | null;
  key?: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.key ? { error: null, key: props.resetKey } : null;
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("a page failed to render", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div className="note error" role="alert">
        <p><strong>{i18n.t("common.pageFailed")}</strong></p>
        <p className="small">{this.state.error.message}</p>
        <button type="button" onClick={() => window.location.reload()}>{i18n.t("common.reload")}</button>
      </div>
    );
  }
}
