import { useCallback, useEffect, useState } from 'react';
import { api, type AgentWhatsApp } from '../api';

/**
 * Per-bot WhatsApp binding.
 *
 * Lets the bot's owner assign one of their linked WA numbers to this bot.
 * On save we invite the bot's MXID to every existing DM portal room for
 * that login; the bot then auto-accepts via its standard invite loop.
 *
 * Only DMs are bridged for v1. Group support comes later.
 */
export function AgentWhatsAppPanel({ agentId }: { agentId: number }) {
  const [state, setState] = useState<AgentWhatsApp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Local edit buffer for the dropdown — separate from server state so we
  // can show "Save"/"Cancel" affordances when it differs.
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const data = await api.getAgentWhatsApp(agentId);
      setState(data);
      setSelected(data.whatsapp_login_id);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [agentId]);

  useEffect(() => { void load(); }, [load]);

  const save = useCallback(async () => {
    if (!state) return;
    setBusy(true);
    setErr(null);
    try {
      const next = await api.setAgentWhatsApp(agentId, selected || null);
      setState(next);
      setSelected(next.whatsapp_login_id);
      setSavedAt(Date.now());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [agentId, selected, state]);

  if (!state) {
    return (
      <div className="section">
        <h3>WhatsApp</h3>
        {err ? (
          <div className="muted small" style={{ color: 'var(--err)' }}>{err}</div>
        ) : (
          <div className="muted small">Loading…</div>
        )}
      </div>
    );
  }

  const hasLogins = state.available_logins.length > 0;
  const isDirty = (selected || null) !== state.whatsapp_login_id;
  // Logins NOT taken by another bot — those are usable here.
  // The currently-bound login is always usable (no other bot owns it).
  const isUsable = (opt: typeof state.available_logins[number]) =>
    opt.taken_by_agent_id === null || opt.id === state.whatsapp_login_id;

  return (
    <div className="section">
      <h3>WhatsApp</h3>

      {!hasLogins ? (
        <div className="muted small">
          You haven't linked any WhatsApp numbers yet.{' '}
          <em>Settings → WhatsApp → Link a number</em>, then come back here.
        </div>
      ) : (
        <>
          <div className="muted small" style={{ marginBottom: 8 }}>
            Pick one of your linked WhatsApp numbers. DMs to that number will
            be delivered to this bot (groups are not bridged in this version).
          </div>

          <div className="row" style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
            <select
              className="input"
              value={selected || ''}
              onChange={(e) => setSelected(e.target.value || null)}
              disabled={busy}
              style={{ flex: 1 }}
            >
              <option value="">— None (don't handle WhatsApp) —</option>
              {state.available_logins.map((opt) => {
                const label = opt.name || `+${opt.id}`;
                const suffix = opt.taken_by_agent_id && opt.id !== state.whatsapp_login_id
                  ? ` — assigned to ${opt.taken_by_agent_name}`
                  : opt.dm_count > 0
                    ? ` — ${opt.dm_count} chat${opt.dm_count === 1 ? '' : 's'}`
                    : '';
                return (
                  <option
                    key={opt.id}
                    value={opt.id}
                    disabled={!isUsable(opt)}
                  >
                    {label}{suffix}
                  </option>
                );
              })}
            </select>

            <button
              className="btn"
              onClick={save}
              disabled={busy || !isDirty}
            >
              {busy ? 'Saving…' : 'Save'}
            </button>
            {isDirty && (
              <button
                className="btn-secondary"
                onClick={() => setSelected(state.whatsapp_login_id)}
                disabled={busy}
              >
                Cancel
              </button>
            )}
          </div>

          {err && (
            <div className="banner err" style={{ marginBottom: 12 }}>
              {err}
            </div>
          )}
          {savedAt && !err && Date.now() - savedAt < 4000 && (
            <div className="banner ok" style={{ marginBottom: 12 }}>
              Saved. Bot was invited to {state.bound_portals.length} chat
              {state.bound_portals.length === 1 ? '' : 's'}.
            </div>
          )}

          {state.whatsapp_login_id && (
            <>
              <div className="muted small" style={{ marginBottom: 6 }}>
                Currently bridged DMs:
              </div>
              {state.bound_portals.length === 0 ? (
                <div className="muted small">
                  No active chats yet. As people DM your WhatsApp number, this
                  bot will be auto-invited to each new chat.
                </div>
              ) : (
                <ul className="list-unstyled" style={{ marginBottom: 0 }}>
                  {state.bound_portals.map((bp) => (
                    <li key={bp.mxid} style={{
                      padding: '6px 10px',
                      border: '1px solid var(--border)',
                      borderRadius: 6,
                      marginBottom: 4,
                      fontSize: 13,
                    }}>
                      <span style={{ fontWeight: 500 }}>{bp.name || bp.portal_id}</span>
                      <span className="muted small" style={{
                        marginLeft: 8, fontFamily: 'monospace', fontSize: 11,
                      }}>
                        {bp.mxid}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
