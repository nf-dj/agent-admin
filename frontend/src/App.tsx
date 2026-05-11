import { useEffect, useState, useCallback, lazy, Suspense } from 'react';
import { api, type User } from './api';
import { AuthScreen } from './components/AuthScreen';
import { Dashboard } from './components/Dashboard';
import { AgentDetailView } from './components/AgentDetailView';
import { CreateAgentView } from './components/CreateAgentView';
import { SettingsView } from './components/SettingsView';
import { RoomsView } from './components/RoomsView';
import { ErrorBoundary } from './components/ErrorBoundary';
import { getBrand } from './brand';
import './App.css';

// matrix-js-sdk is ~500 KB; only load it when the user opens a chat.
const ChatView = lazy(() =>
  import('./components/ChatView').then((m) => ({ default: m.ChatView })));

type View =
  | { kind: 'list' }
  | { kind: 'create' }
  | { kind: 'detail'; agentId: number }
  | { kind: 'chat'; agentId: number }
  | { kind: 'rooms'; agentId: number; agentName: string }
  | { kind: 'settings' };

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [allowSignup, setAllowSignup] = useState(true);
  const [view, setView] = useState<View>({ kind: 'list' });
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    api.me()
      .then((r) => {
        if (r.authenticated && r.user) setUser(r.user);
        setAllowSignup(r.allow_signup ?? true);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const onLogout = useCallback(async () => {
    await api.logout();
    setUser(null);
    setView({ kind: 'list' });
  }, []);

  if (loading) return <div className="loading">Loading…</div>;
  if (!user) return <AuthScreen allowSignup={allowSignup} onAuthed={setUser} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand" onClick={() => setView({ kind: 'list' })}>
          {(() => { const b = getBrand(); return (<>
            <img
              src={b.logo}
              srcSet={`${b.logo} 1x, ${b.logo2x} 2x`}
              alt={b.alt}
              className="brand-logo"
            />
            <span>{b.name}</span>
          </>); })()}
        </div>
        <div className="topbar-actions">
          <span className="topbar-user">
            {user.display_name || user.email}
            {user.is_admin && <span className="badge badge-admin">admin</span>}
          </span>
          <button
            className="btn btn-ghost btn-sm"
            title="Settings"
            aria-label="Settings"
            onClick={() => setView({ kind: 'settings' })}
          >
            ⚙️
          </button>
          <button className="btn btn-ghost btn-sm" onClick={onLogout}>Sign out</button>
        </div>
      </header>

      <main className="main">
        <ErrorBoundary key={`${view.kind}-${refreshKey}`}>
          {view.kind === 'list' && (
            <Dashboard
              onCreate={() => setView({ kind: 'create' })}
              onOpen={(id) => setView({ kind: 'detail', agentId: id })}
              onChat={(id) => setView({ kind: 'chat', agentId: id })}
              onRooms={(id, name) => setView({ kind: 'rooms', agentId: id, agentName: name })}
            />
          )}
          {view.kind === 'create' && (
            <CreateAgentView
              onCancel={() => setView({ kind: 'list' })}
              onCreated={(id) => {
                setRefreshKey((k) => k + 1);
                setView({ kind: 'detail', agentId: id });
              }}
            />
          )}
          {view.kind === 'detail' && (
            <AgentDetailView
              agentId={view.agentId}
              onBack={() => {
                setRefreshKey((k) => k + 1);
                setView({ kind: 'list' });
              }}
              onChat={(id) => setView({ kind: 'chat', agentId: id })}
            />
          )}
          {view.kind === 'chat' && (
            <Suspense fallback={<div className="loading">Loading chat…</div>}>
              <ChatView
                agentId={view.agentId}
                /* Back to the list — members can't reach the detail view. */
                onBack={() => setView({ kind: 'list' })}
              />
            </Suspense>
          )}
          {view.kind === 'rooms' && (
            <RoomsView
              agentId={view.agentId}
              agentName={view.agentName}
              onBack={() => setView({ kind: 'list' })}
            />
          )}
          {view.kind === 'settings' && (
            <SettingsView
              initialUser={user}
              onSaved={(u) => setUser(u)}
              onBack={() => setView({ kind: 'list' })}
            />
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}
