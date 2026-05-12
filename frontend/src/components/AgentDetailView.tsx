import { useEffect, useState } from 'react';
import { api, type AgentDetail, type Model } from '../api';
import { MembersPanel } from './MembersPanel';
import { AgentApiKeysPanel } from './AgentApiKeysPanel';
import { AgentWhatsAppPanel } from './AgentWhatsAppPanel';
import { WhatsAppRoutingPanel } from './WhatsAppRoutingPanel';

export function AgentDetailView({ agentId, onBack, onChat }: { agentId: number; onBack: () => void; onChat: (id: number) => void }) {
  const [agent, setAgent] = useState<AgentDetail | null>(null);
  const [models, setModels] = useState<Model[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const [displayName, setDisplayName] = useState('');
  const [model, setModel] = useState('');
  const [emoji, setEmoji] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [telegramToken, setTelegramToken] = useState('');
  const [busy, setBusy] = useState(false);

  // Runtime info comes from the harness (OpenClaw CLI) and is fetched
  // lazily — the form itself reads from the DB.
  const [runtime, setRuntime] = useState<any | null>(null);
  const [runtimeBusy, setRuntimeBusy] = useState(false);

  useEffect(() => {
    api.getAgent(agentId).then((a) => {
      setAgent(a);
      setDisplayName(a.display_name);
      setModel(a.model || '');
      setEmoji(a.emoji || '');
      setSystemPrompt(a.system_prompt || '');
    }).catch((e) => setErr(e.message));
    api.models().then(setModels).catch(() => {});
  }, [agentId]);

  function flash(s: string) {
    setMsg(s);
    setTimeout(() => setMsg(null), 3000);
  }

  async function save() {
    if (!agent) return;
    setBusy(true);
    setErr(null);
    try {
      const updated = await api.updateAgent(agent.id, {
        display_name: displayName,
        model: model || undefined,
        emoji: emoji || undefined,
        system_prompt: systemPrompt,
        telegram_bot_token: telegramToken.trim() || undefined,
      });
      setAgent(updated);
      setTelegramToken('');
      flash('Saved.');
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function destroy() {
    if (!agent) return;
    if (!confirm(`Delete agent "${agent.display_name}"? This removes the OpenClaw agent, its workspace, and all sessions. Cannot be undone.`)) return;
    setBusy(true);
    try {
      await api.deleteAgent(agent.id);
      onBack();
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
    }
  }

  async function loadRuntime() {
    setRuntimeBusy(true);
    try {
      const rt = await api.getAgentRuntime(agentId);
      setRuntime(rt);
    } catch (e: any) {
      setRuntime({ error: e.message });
    } finally {
      setRuntimeBusy(false);
    }
  }

  async function resync() {
    setRuntimeBusy(true);
    setErr(null);
    try {
      const r = await api.resyncAgent(agentId);
      flash(`Synced (${r.op}).`);
      await loadRuntime();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setRuntimeBusy(false);
    }
  }

  if (err && !agent) return <div className="banner-error">{err}</div>;
  if (!agent) return <div className="loading">Loading…</div>;

  // OpenClaw `agents list --json` returns:
  //   bindings: <number>          (a count)
  //   bindingDetails: ["telegram accountId=foo", ...]   (display strings)
  // Older versions returned bindings as an array of {match:{channel,accountId}}.
  // Normalize either shape into display strings.
  function normalizeBindings(rt: any): string[] {
    if (!rt) return [];
    if (Array.isArray(rt.bindingDetails)) return rt.bindingDetails.map((s: any) => String(s));
    if (Array.isArray(rt.bindings)) {
      return rt.bindings.map((b: any) => {
        const ch = b?.match?.channel || b?.channel;
        const acct = b?.match?.accountId || b?.accountId;
        return acct ? `${ch} (${acct})` : (ch || JSON.stringify(b));
      });
    }
    return [];
  }
  const bindings: string[] = normalizeBindings(runtime);

  return (
    <div>
      <div className="detail-head">
        <div>
          <button className="btn btn-ghost btn-sm" onClick={onBack}>← All agents</button>
          <h2 style={{ marginTop: 8 }}>
            <span style={{ fontSize: 28 }}>{agent.emoji || '🤖'}</span>
            {agent.display_name}
          </h2>
          <div className="agent-id">
            {agent.harness_agent_id} <span className="muted">·</span> {agent.harness}
          </div>
        </div>
        {agent.matrix_user_id && (
          <button className="btn btn-primary grow-0" onClick={() => onChat(agentId)}>
            💬 Chat
          </button>
        )}
      </div>

      {msg && <div className="banner-success">{msg}</div>}
      {err && <div className="banner-error">{err}</div>}

      <div className="section">
        <h3>Settings</h3>

        <div className="field">
          <label className="field-label">Display name</label>
          <input className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
        </div>

        <div className="field">
          <label className="field-label">Emoji</label>
          <input
            className="input"
            value={emoji}
            onChange={(e) => setEmoji(e.target.value)}
            style={{ width: 100 }}
            maxLength={8}
          />
        </div>

        <div className="field">
          <label className="field-label">Model</label>
          <select className="select" value={model} onChange={(e) => setModel(e.target.value)}>
            <option value="">(use default)</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.name} — {m.id}</option>
            ))}
          </select>
        </div>

        <div className="field">
          <label className="field-label">
            Telegram bot token
            {agent.has_telegram && (
              <span className="badge badge-telegram" style={{ marginLeft: 8 }}>connected</span>
            )}
          </label>
          <input
            className="input"
            type="password"
            value={telegramToken}
            onChange={(e) => setTelegramToken(e.target.value)}
            placeholder={agent.has_telegram ? '••••••••  (enter a new token to replace)' : '123456789:ABCdef…'}
          />
          <div className="help">
            Tokens are stored on the server and used to register this agent with Telegram.
          </div>
        </div>

        <div className="field">
          <label className="field-label">System prompt (SOUL.md)</label>
          <textarea
            className="textarea"
            rows={12}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="How should this agent behave?"
            spellCheck={false}
          />
        </div>

        <div className="row" style={{ justifyContent: 'flex-end' }}>
          <button className="btn btn-primary grow-0" onClick={save} disabled={busy}>
            {busy ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </div>

      <div className="section">
        <div className="row" style={{ marginBottom: 12 }}>
          <h3 style={{ margin: 0 }}>Runtime info</h3>
          <div className="spacer" />
          <button
            className="btn btn-secondary btn-sm grow-0"
            onClick={resync}
            disabled={runtimeBusy}
            title="Re-push DB state to OpenClaw"
          >
            {runtimeBusy ? 'Working…' : 'Sync to OpenClaw'}
          </button>
          <button
            className="btn btn-secondary btn-sm grow-0"
            onClick={loadRuntime}
            disabled={runtimeBusy}
            style={{ marginLeft: 6 }}
          >
            {runtime ? 'Refresh state' : 'Check live state'}
          </button>
        </div>
        <div className="kv">
          <div><strong>Workspace:</strong> <code>{agent.workspace_path}</code></div>
          {agent.matrix_user_id && (
            <div><strong>Matrix user:</strong> <code>{agent.matrix_user_id}</code>
              <span className="muted small" style={{ marginLeft: 8 }}>
                → chat with this bot in Element / your Matrix client
              </span>
            </div>
          )}
          {runtime?.error && (
            <div className="banner-error" style={{ marginTop: 8 }}>
              Harness error: {runtime.error}
            </div>
          )}
          {runtime?.agentDir && (
            <div><strong>Agent dir:</strong> <code>{runtime.agentDir}</code></div>
          )}
          {runtime && (
            <div>
              <strong>Bindings:</strong>{' '}
              {bindings.length === 0 ? <span className="muted">none</span> : (
                <span>
                  {bindings.map((txt, i) => (
                    <span key={i} className="badge" style={{ marginRight: 6 }}>{txt}</span>
                  ))}
                </span>
              )}
            </div>
          )}
          {!runtime && !runtimeBusy && (
            <div className="muted small">
              Click "Check live state" to query OpenClaw for current bindings, agent dir, etc.
            </div>
          )}
        </div>
      </div>

      {agent.my_role === 'owner' && <AgentApiKeysPanel agentId={agent.id} />}

      {agent.my_role === 'owner' && <AgentWhatsAppPanel agentId={agent.id} />}

      {agent.my_role === 'owner' && agent.whatsapp_login_id && (
        <WhatsAppRoutingPanel waLoginId={agent.whatsapp_login_id} />
      )}

      <MembersPanel agentId={agent.id} isOwner={agent.my_role === 'owner'} />

      <div className="section section-danger">
        <h3>Danger zone</h3>
        <p className="muted small">
          Deleting an agent removes its OpenClaw configuration, workspace, and all sessions.
          This action cannot be undone.
        </p>
        <button className="btn btn-danger" onClick={destroy} disabled={busy}>
          Delete this agent
        </button>
      </div>
    </div>
  );
}
