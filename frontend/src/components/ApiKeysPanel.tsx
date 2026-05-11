import { useEffect, useState } from 'react';
import { api, type UserApiKey } from '../api';

/**
 * Per-user API key vault.
 *
 * Lists every supported provider with its current status (set / not set).
 * Clicking "Set" / "Update" reveals an input row. Keys are encrypted at rest
 * on the server (Fernet); only a masked preview is ever returned by the API.
 */

/** UI labels for the canonical provider ids known by the backend. */
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

export function ApiKeysPanel() {
  const [keys, setKeys] = useState<UserApiKey[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  /** Provider id currently being edited (input visible), or null. */
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState<string | null>(null);

  async function load() {
    setErr(null);
    try {
      const data = await api.listApiKeys();
      setKeys(data);
    } catch (e: any) {
      setErr(e?.message || 'Failed to load API keys');
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function save(provider: string) {
    if (!draft.trim()) {
      setErr('Key cannot be empty');
      return;
    }
    setBusy(provider);
    setErr(null);
    try {
      await api.setApiKey(provider, draft.trim());
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
    if (!confirm(`Remove your saved ${PROVIDER_LABELS[provider]?.name || provider} key?`)) return;
    setBusy(provider);
    setErr(null);
    try {
      await api.deleteApiKey(provider);
      await load();
    } catch (e: any) {
      setErr(e?.message || 'Failed to remove key');
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
        <h3>API keys</h3>
        <div className="muted small">Loading…</div>
      </div>
    );
  }

  return (
    <div className="section">
      <h3>API keys</h3>
      <p className="muted small" style={{ marginTop: -8 }}>
        Store your provider keys here so you don't have to re-enter them every
        time you create a bot. Keys are encrypted at rest and never shown back
        to you in plaintext.
      </p>

      {err && <div className="error" style={{ marginBottom: 12 }}>{err}</div>}

      <ul className="member-list" style={{ marginTop: 16 }}>
        {keys.map((k) => {
          const meta = PROVIDER_LABELS[k.provider] || { name: k.provider, hint: '' };
          const isEditing = editing === k.provider;
          const isBusy = busy === k.provider;

          return (
            <li key={k.provider} className="member-row" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
                <div className="member-main">
                  <div className="member-name">{meta.name}</div>
                  <div className="member-email">
                    {k.has_key ? (
                      <>
                        <span style={{ fontFamily: 'monospace' }}>{k.preview}</span>
                        {k.updated_at && (
                          <span className="muted"> · updated {new Date(k.updated_at).toLocaleDateString()}</span>
                        )}
                      </>
                    ) : (
                      <span className="muted">Not set</span>
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
                      {k.has_key ? 'Update' : 'Set'}
                    </button>
                    {k.has_key && (
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        onClick={() => remove(k.provider)}
                        disabled={isBusy}
                        title="Remove saved key"
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
                      {isBusy ? 'Saving…' : 'Save key'}
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
