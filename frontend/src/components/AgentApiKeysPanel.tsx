import { useEffect, useState } from 'react';
import { api, type AgentApiKey } from '../api';

/**
 * Per-agent API key overrides.
 *
 * Each agent can override the owner's user-level saved key for a given
 * provider. Useful when:
 *   - This bot should bill against a different account
 *   - You want rate-limit isolation between bots
 *   - You're testing a new key without disturbing other agents
 *
 * Falls back to the user-saved key (visible here for context) when no
 * override is set. If neither exists, the agent has no key for that
 * provider.
 *
 * Owner-only. Hidden for members.
 */

const PROVIDER_LABELS: Record<string, { name: string; hint: string }> = {
  deepseek: { name: 'DeepSeek', hint: 'sk-… key from platform.deepseek.com' },
  openai: { name: 'OpenAI', hint: 'sk-… key from platform.openai.com' },
  openrouter: { name: 'OpenRouter', hint: 'sk-or-… key from openrouter.ai' },
  groq: { name: 'Groq', hint: 'gsk_… key from console.groq.com' },
  mistral: { name: 'Mistral', hint: 'API key from console.mistral.ai' },
  anthropic: { name: 'Anthropic (API)', hint: 'sk-ant-… key from console.anthropic.com' },
  'anthropic-subscription': {
    name: 'Anthropic (Claude subscription)',
    hint: 'OAuth/subscription token for Claude.ai (advanced)',
  },
};

export function AgentApiKeysPanel({ agentId }: { agentId: number }) {
  const [keys, setKeys] = useState<AgentApiKey[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  /** Provider id currently being edited, or null. */
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      const data = await api.listAgentApiKeys(agentId);
      setKeys(data);
    } catch (e: any) {
      setErr(e?.message || 'Failed to load API keys');
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  async function save(provider: string) {
    if (!draft.trim()) {
      setErr('Key cannot be empty');
      return;
    }
    setBusy(provider);
    setErr(null);
    try {
      await api.setAgentApiKey(agentId, provider, draft.trim());
      setEditing(null);
      setDraft('');
      await load();
    } catch (e: any) {
      setErr(e?.message || 'Failed to save key');
    } finally {
      setBusy(null);
    }
  }

  async function remove(provider: string) {
    const label = PROVIDER_LABELS[provider]?.name || provider;
    if (!confirm(`Remove this agent's override for ${label}? It will fall back to your saved key (if any).`)) return;
    setBusy(provider);
    setErr(null);
    try {
      await api.deleteAgentApiKey(agentId, provider);
      await load();
    } catch (e: any) {
      setErr(e?.message || 'Failed to remove override');
    } finally {
      setBusy(null);
    }
  }

  function startEdit(provider: string) {
    setEditing(provider);
    setDraft('');
    setErr(null);
  }

  function cancelEdit() {
    setEditing(null);
    setDraft('');
  }

  if (keys === null) {
    return (
      <div className="section">
        <h3>API key overrides</h3>
        <div className="muted small">Loading…</div>
      </div>
    );
  }

  // Sort so providers with an override or user-saved key float to the top —
  // they're the ones the user is most likely to care about.
  const sorted = [...keys].sort((a, b) => {
    const aScore = (a.has_override ? 2 : 0) + (a.user_has_saved ? 1 : 0);
    const bScore = (b.has_override ? 2 : 0) + (b.user_has_saved ? 1 : 0);
    return bScore - aScore;
  });

  return (
    <div className="section">
      <h3>API key overrides</h3>
      <p className="muted small" style={{ marginTop: -8 }}>
        Override your default key for this specific bot. Useful for separate
        billing accounts, rate-limit isolation, or testing keys. When no
        override is set, this bot uses your saved key from{' '}
        <strong>Settings → API keys</strong>.
      </p>

      {err && <div className="error" style={{ marginBottom: 12 }}>{err}</div>}

      <ul className="member-list" style={{ marginTop: 16 }}>
        {sorted.map((k) => {
          const meta = PROVIDER_LABELS[k.provider] || { name: k.provider, hint: '' };
          const isEditing = editing === k.provider;
          const isBusy = busy === k.provider;

          /** Plain-English summary of the *effective* key source for this agent. */
          const status = k.has_override
            ? { tone: 'override', label: 'Override active', detail: k.override_preview }
            : k.user_has_saved
              ? { tone: 'fallback', label: 'Using saved key', detail: k.user_saved_preview }
              : { tone: 'none', label: 'No key configured', detail: null };

          return (
            <li key={k.provider} className="member-row" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
                <div className="member-main">
                  <div className="member-name">{meta.name}</div>
                  <div className="member-email">
                    <span className={`key-status key-status-${status.tone}`}>{status.label}</span>
                    {status.detail && (
                      <>
                        {' '}<span style={{ fontFamily: 'monospace' }}>{status.detail}</span>
                      </>
                    )}
                    {k.has_override && k.user_has_saved && (
                      <span className="muted">
                        {' '}· saved fallback: <span style={{ fontFamily: 'monospace' }}>{k.user_saved_preview}</span>
                      </span>
                    )}
                    {k.override_updated_at && (
                      <span className="muted">
                        {' '}· updated {new Date(k.override_updated_at).toLocaleDateString()}
                      </span>
                    )}
                  </div>
                </div>
                {!isEditing && (
                  <>
                    <button
                      type="button"
                      className="btn btn-ghost btn-sm"
                      onClick={() => startEdit(k.provider)}
                      disabled={isBusy}
                    >
                      {k.has_override ? 'Update' : 'Override'}
                    </button>
                    {k.has_override && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => remove(k.provider)}
                        disabled={isBusy}
                        title="Remove override (fall back to saved key)"
                      >
                        ✕
                      </button>
                    )}
                  </>
                )}
              </div>

              {isEditing && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: 10 }}>
                  <input
                    type="password"
                    className="input"
                    placeholder={meta.hint}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    autoFocus
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck={false}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); void save(k.provider); }
                      if (e.key === 'Escape') cancelEdit();
                    }}
                  />
                  <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                    <button type="button" className="btn btn-ghost btn-sm" onClick={cancelEdit} disabled={isBusy}>
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="btn btn-primary btn-sm"
                      onClick={() => void save(k.provider)}
                      disabled={isBusy || !draft.trim()}
                    >
                      {isBusy ? 'Saving…' : 'Save override'}
                    </button>
                  </div>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
