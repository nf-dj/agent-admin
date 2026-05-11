import { Component, type ReactNode } from 'react';

interface Props { children: ReactNode }
interface State { error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: any) {
    // eslint-disable-next-line no-console
    console.error('UI error:', error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="banner-error" style={{ margin: 24 }}>
          <strong>Something went wrong rendering this page.</strong>
          <div style={{ marginTop: 6, fontFamily: 'monospace', fontSize: 12 }}>
            {this.state.error.message}
          </div>
          <button
            className="btn btn-secondary"
            style={{ marginTop: 12 }}
            onClick={() => { this.setState({ error: null }); }}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
