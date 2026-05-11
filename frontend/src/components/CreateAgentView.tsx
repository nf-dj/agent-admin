import { useEffect, useMemo, useState } from 'react';
import { api, type Model, type UserApiKey } from '../api';

/**
 * Per-provider credential requirements.
 *
 * - `apiKeyOnly`  : a single API key is required (e.g. DeepSeek, OpenAI).
 * - `claudeAuth`  : Anthropic — accepts EITHER a Claude.ai subscription session
 *                   key OR a regular Anthropic API key. We let the user pick.
 * - `none`        : no credentials needed (local model / proxy already configured).
 */
type AuthKind = 'apiKeyOnly' | 'claudeAuth' | 'none';

function authKindFor(provider: string | undefined): AuthKind {
  if (!provider) return 'none';
  const p = provider.toLowerCase();
  if (p === 'anthropic') return 'claudeAuth';
  if (p === 'deepseek' || p === 'openai' || p === 'openrouter' || p === 'groq' || p === 'mistral') {
    return 'apiKeyOnly';
  }
  return 'none';
}

/** Human label for the provider, used in field hints. */
function providerLabel(provider: string | undefined): string {
  if (!provider) return '';
  const p = provider.toLowerCase();
  return p.charAt(0).toUpperCase() + p.slice(1);
}

export function CreateAgentView({ onCancel, onCreated }: { onCancel: () => void; onCreated: (id: number) => void }) {
  const [displayName, setDisplayName] = useState('');
  const [slug, setSlug] = useState('');
  const [model, setModel] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [telegramEnabled, setTelegramEnabled] = useState(false);
  const [telegramToken, setTelegramToken] = useState('');
  const [models, setModels] = useState<Model[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Provider credential state.
  // For `claudeAuth` providers, the user picks one mode.
  const [claudeMode, setClaudeMode] = useState<'subscription' | 'api'>('subscription');
  const [providerApiKey, setProviderApiKey] = useState('');
  const [claudeSubKey, setClaudeSubKey] = useState('');

  /**
   * Saved keys from /api/me/api-keys (set in Settings).
   * When the user picks a model whose provider already has a stored key,
   * we hide the input by default and let them opt-in to override it.
   */
  const [savedKeys, setSavedKeys] = useState<UserApiKey[]>([]);
  /** Per-provider override toggle: true means "ignore my saved key for this bot". */
  const [overrideKey, setOverrideKey] = useState(false);

  useEffect(() => {
    api.models().then(setModels).catch(() => {});
    // Saved keys are non-critical — silently degrade if unavailable.
    api.listApiKeys().then(setSavedKeys).catch(() => {});
  }, []);

  /** Lookup helper: does the user have a saved key for this provider id? */
  function savedFor(providerId: string): UserApiKey | undefined {
    return savedKeys.find((k) => k.provider === providerId && k.has_key);
  }

  const selectedModel = useMemo(
    () => models.find((m) => m.id === model) || null,
    [models, model],
  );
  const provider = selectedModel?.provider;
  const authKind = authKindFor(provider);
  const provLabel = providerLabel(provider);

  /**
   * The saved-key entry that applies to the *current* model + mode, if any.
   * - apiKeyOnly providers → lookup by provider id
   * - claudeAuth subscription → lookup ``anthropic-subscription``
   * - claudeAuth api          → lookup ``anthropic``
   */
  const applicableSavedKey = useMemo(() => {
    if (!provider) return undefined;
    if (authKind === 'apiKeyOnly') return savedFor(provider);
    if (authKind === 'claudeAuth') {
      return savedFor(claudeMode === 'subscription' ? 'anthropic-subscription' : 'anthropic');
    }
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, authKind, claudeMode, savedKeys]);

  /** True when we'll silently reuse the saved key (input hidden). */
  const useSavedKey = !!applicableSavedKey && !overrideKey;

  /** What do we need for the currently-selected model to be submittable? */
  const credsReady = useMemo(() => {
    if (authKind === 'none') return true;
    // If we have a saved key and the user hasn't opted to override, we're good.
    if (useSavedKey) return true;
    if (authKind === 'apiKeyOnly') return providerApiKey.trim().length > 0;
    if (authKind === 'claudeAuth') {
      return claudeMode === 'subscription'
        ? claudeSubKey.trim().length > 0
        : providerApiKey.trim().length > 0;
    }
    return true;
  }, [authKind, providerApiKey, claudeSubKey, claudeMode, useSavedKey]);

  const canSubmit =
    !busy &&
    displayName.trim().length > 0 &&
    model.length > 0 &&
    credsReady &&
    (!telegramEnabled || telegramToken.trim().length > 0);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try {
      // Only attach the cred field that matches the chosen auth mode,
      // so the backend doesn't have to guess which one is "active".
      //
      // When ``useSavedKey`` is true we deliberately send nothing — the
      // backend should fall back to the user's stored key. (If the backend
      // doesn't yet auto-fill from saved keys, this just means the agent
      // will be created without an explicit key, which matches today's
      // behaviour where keys aren't actually persisted to the harness
      // config yet.)
      let provider_api_key: string | undefined;
      let claude_subscription_key: string | undefined;
      if (!useSavedKey) {
        if (authKind === 'apiKeyOnly') {
          provider_api_key = providerApiKey.trim() || undefined;
        } else if (authKind === 'claudeAuth') {
          if (claudeMode === 'subscription') {
            claude_subscription_key = claudeSubKey.trim() || undefined;
          } else {
            provider_api_key = providerApiKey.trim() || undefined;
          }
        }
      }

      const created = await api.createAgent({
        display_name: displayName.trim(),
        slug: slug.trim() || undefined,
        model,
        system_prompt: systemPrompt.trim() || undefined,
        telegram_bot_token: telegramEnabled ? (telegramToken.trim() || undefined) : undefined,
        provider_api_key,
        claude_subscription_key,
      });
      onCreated(created.id);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <div className="list-head">
        <h2>Create new agent</h2>
        <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
      </div>

      <form className="section" onSubmit={submit}>
        <div className="field">
          <label className="field-label">Display name *</label>
          <input
            className="input"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="e.g. Research Assistant"
            required
            autoFocus
            maxLength={120}
          />
          <div className="help">What this agent is called when it talks.</div>
        </div>

        <div className="field">
          <label className="field-label">Slug <span className="muted small">(optional)</span></label>
          <input
            className="input"
            value={slug}
            onChange={(e) => setSlug(e.target.value.toLowerCase())}
            placeholder="auto-generated from display name"
            pattern="[a-z0-9][a-z0-9_-]*"
            maxLength={60}
          />
          <div className="help">Used as the agent ID (lowercase letters, digits, <code>_</code>, <code>-</code>). Leave blank to derive it from the display name.</div>
        </div>

        <div className="field">
          <label className="field-label">Model *</label>
          <select
            className="select"
            value={model}
            onChange={(e) => {
              setModel(e.target.value);
              // Clear creds + override state when the provider changes so we
              // don't accidentally submit a stale key to a different provider.
              setProviderApiKey('');
              setClaudeSubKey('');
              setOverrideKey(false);
            }}
            required
          >
            <option value="" disabled>— select a model —</option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>{m.name} — {m.id}</option>
            ))}
          </select>
          {selectedModel && (
            <div className="help">Provider: <strong>{provLabel}</strong></div>
          )}
        </div>

        {/* DeepSeek / OpenAI / OpenRouter / Groq / Mistral — one API key */}
        {authKind === 'apiKeyOnly' && (
          <div className="field">
            <label className="field-label">
              {provLabel} API key {useSavedKey ? <span className="muted small">(using saved)</span> : '*'}
            </label>

            {useSavedKey ? (
              <SavedKeyBanner
                preview={applicableSavedKey!.preview}
                onOverride={() => setOverrideKey(true)}
              />
            ) : (
              <>
                <input
                  className="input"
                  value={providerApiKey}
                  onChange={(e) => setProviderApiKey(e.target.value)}
                  placeholder={provider === 'deepseek' ? 'sk-…' : 'API key'}
                  type="password"
                  required
                  autoComplete="off"
                />
                <div className="help">
                  This key is used to call the {provLabel} API on behalf of this agent.
                  {applicableSavedKey && (
                    <>
                      {' '}
                      <button
                        type="button"
                        className="link"
                        onClick={() => { setOverrideKey(false); setProviderApiKey(''); }}
                      >
                        Use saved key instead
                      </button>
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        )}

        {/* Anthropic — pick subscription session key OR API key */}
        {authKind === 'claudeAuth' && (
          <>
            <div className="field">
              <label className="field-label">Claude authentication *</label>
              <div className="row" style={{ gap: 8, marginTop: 4 }}>
                <label className="checkbox">
                  <input
                    type="radio"
                    name="claude-mode"
                    checked={claudeMode === 'subscription'}
                    onChange={() => setClaudeMode('subscription')}
                  />
                  <span>Claude.ai subscription</span>
                </label>
                <label className="checkbox">
                  <input
                    type="radio"
                    name="claude-mode"
                    checked={claudeMode === 'api'}
                    onChange={() => setClaudeMode('api')}
                  />
                  <span>Anthropic API key</span>
                </label>
              </div>
              <div className="help">
                Use your <strong>Claude.ai subscription</strong> if you already pay for Claude
                Pro/Max — no per-token billing. Otherwise use an{' '}
                <strong>Anthropic API key</strong> (pay-as-you-go).
              </div>
            </div>

            {claudeMode === 'subscription' && (
              <div className="field">
                <label className="field-label">
                  Claude subscription session key {useSavedKey ? <span className="muted small">(using saved)</span> : '*'}
                </label>
                {useSavedKey ? (
                  <SavedKeyBanner
                    preview={applicableSavedKey!.preview}
                    onOverride={() => setOverrideKey(true)}
                  />
                ) : (
                  <>
                    <input
                      className="input"
                      value={claudeSubKey}
                      onChange={(e) => setClaudeSubKey(e.target.value)}
                      placeholder="sk-ant-sid01-…"
                      type="password"
                      required
                      autoComplete="off"
                    />
                    <div className="help">
                      The <code>sessionKey</code> cookie value from claude.ai (starts with <code>sk-ant-sid01-</code>).
                    </div>
                  </>
                )}
              </div>
            )}

            {claudeMode === 'api' && (
              <div className="field">
                <label className="field-label">
                  Anthropic API key {useSavedKey ? <span className="muted small">(using saved)</span> : '*'}
                </label>
                {useSavedKey ? (
                  <SavedKeyBanner
                    preview={applicableSavedKey!.preview}
                    onOverride={() => setOverrideKey(true)}
                  />
                ) : (
                  <>
                    <input
                      className="input"
                      value={providerApiKey}
                      onChange={(e) => setProviderApiKey(e.target.value)}
                      placeholder="sk-ant-api03-…"
                      type="password"
                      required
                      autoComplete="off"
                    />
                    <div className="help">
                      Get one from <a href="https://console.anthropic.com/" target="_blank" rel="noreferrer">console.anthropic.com</a>.
                    </div>
                  </>
                )}
              </div>
            )}
          </>
        )}

        <div className="field">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={telegramEnabled}
              onChange={(e) => setTelegramEnabled(e.target.checked)}
            />
            <span>Enable Telegram</span>
          </label>
          <div className="help">Wire this agent up to a Telegram bot.</div>
        </div>

        {telegramEnabled && (
          <div className="field">
            <label className="field-label">Telegram bot token *</label>
            <input
              className="input"
              value={telegramToken}
              onChange={(e) => setTelegramToken(e.target.value)}
              placeholder="123456789:ABCdef…"
              type="password"
              required
            />
            <div className="help">
              Get one from <a href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a>.
            </div>
          </div>
        )}

        <div className="field">
          <label className="field-label">System prompt <span className="muted small">(optional)</span></label>
          <textarea
            className="textarea"
            rows={6}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            placeholder="Describe how the agent should behave. (Written into SOUL.md)"
          />
        </div>

        {err && <div className="banner-error">{err}</div>}

        <div className="row" style={{ marginTop: 16, justifyContent: 'flex-end' }}>
          <button type="button" className="btn btn-secondary grow-0" onClick={onCancel}>Cancel</button>
          <button type="submit" className="btn btn-primary grow-0" disabled={!canSubmit}>
            {busy ? 'Creating…' : 'Create agent'}
          </button>
        </div>
      </form>
    </div>
  );
}

/**
 * Banner shown in place of a key input when the user has a saved key.
 * Includes an "Override" link so they can supply a one-off key for this
 * bot without touching their saved default.
 */
function SavedKeyBanner({
  preview,
  onOverride,
}: {
  preview: string | null;
  onOverride: () => void;
}) {
  return (
    <div className="banner-info" style={{ margin: 0 }}>
      Using your saved key{' '}
      <span style={{ fontFamily: 'monospace' }}>{preview}</span>{' '}
      ·{' '}
      <button type="button" className="link" onClick={onOverride}>
        Use a different key for this bot
      </button>
    </div>
  );
}
