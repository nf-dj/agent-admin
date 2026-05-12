import { useEffect, useMemo, useState } from 'react';
import {
  api,
  type CustomProvider,
  type CustomModelDef,
  type ProviderTestResult,
} from '../api';

/**
 * BYO LLM providers panel.
 *
 * Lets the signed-in user register their own model endpoints (e.g. a
 * local llama-server reachable over Tailscale) so they can spawn agents
 * pointed at those endpoints. CRUD + a "Test connection" probe that hits
 * the right discovery endpoint per ``api_type``.
 *
 * Models are entered as a JSON array (power-user shape) — keeps the form
 * simple and matches the underlying ``openclaw.json`` shape 1:1. A
 * nicer field-based editor can come later.
 */

const API_TYPES = [
  { value: 'openai-completions', label: 'OpenAI-compatible (completions)' },
  { value: 'openai-chat', label: 'OpenAI-compatible (chat)' },
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'google', label: 'Google (Gemini)' },
  { value: 'ollama', label: 'Ollama' },
];

/**
 * Defaults for a fresh model row. 128k context fits Gemma 3, Llama 3.1+,
 * Mistral Large, Claude 3.5+, GPT-4o-class — i.e. "most modern models".
 * Users can dial it down for older 8k/32k models.
 */
const DEFAULT_CONTEXT_WINDOW = 131072;   // 128k
const DEFAULT_MAX_TOKENS = 8192;         // 8k output

function freshModelRow(): CustomModelDef {
  return {
    id: '',
    name: '',
    reasoning: false,
    input: ['text'],
    cost: { input: 0, output: 0 },
    contextWindow: DEFAULT_CONTEXT_WINDOW,
    maxTokens: DEFAULT_MAX_TOKENS,
  };
}

/**
 * Fill in any optional fields the backend may have omitted (or that a
 * legacy row didn't set) so the row editor always has concrete values to
 * bind to. Preserves any extras the user added in Advanced mode so a
 * round-trip through Simple→Advanced→Simple doesn't drop them.
 */
function normalizeModel(m: CustomModelDef): CustomModelDef {
  return {
    ...m,
    id: m.id ?? '',
    name: m.name ?? '',
    reasoning: !!m.reasoning,
    input: m.input?.length ? m.input : ['text'],
    cost: m.cost ?? { input: 0, output: 0 },
    contextWindow: m.contextWindow ?? DEFAULT_CONTEXT_WINDOW,
    maxTokens: m.maxTokens ?? DEFAULT_MAX_TOKENS,
  };
}

/** Template models array shown in a fresh form (used by Advanced/JSON mode). */
const DEFAULT_MODELS_JSON = JSON.stringify(
  [
    {
      id: 'my-model-id',
      name: 'My Model',
      reasoning: false,
      input: ['text'],
      cost: { input: 0, output: 0 },
      contextWindow: DEFAULT_CONTEXT_WINDOW,
      maxTokens: DEFAULT_MAX_TOKENS,
    },
  ],
  null,
  2,
);

type FormState = {
  /** Set for edits; null for "create new" mode. */
  editingId: number | null;
  slug: string;
  display_name: string;
  base_url: string;
  api_type: string;
  api_key: string;
  /** Was an api_key previously stored? Controls the "clear" affordance. */
  had_key: boolean;
  /** Editable per-row representation of the models array. */
  models: CustomModelDef[];
  /** When true, hide the row editor and let the user hand-edit the JSON. */
  advanced: boolean;
  /** Raw JSON shown in advanced mode. Kept in sync with ``models`` when toggling. */
  models_text: string;
};

const EMPTY_FORM: FormState = {
  editingId: null,
  slug: '',
  display_name: '',
  base_url: '',
  api_type: 'openai-completions',
  api_key: '',
  had_key: false,
  models: [freshModelRow()],
  advanced: false,
  models_text: DEFAULT_MODELS_JSON,
};

export function CustomProvidersPanel() {
  const [providers, setProviders] = useState<CustomProvider[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [busy, setBusy] = useState(false);
  /** Map of provider id → last test result; null = saved but not tested yet. */
  const [testResults, setTestResults] = useState<Record<number, ProviderTestResult | null>>({});
  const [testingId, setTestingId] = useState<number | 'form' | null>(null);
  /** Test result for the unsaved form (preview). */
  const [formTestResult, setFormTestResult] = useState<ProviderTestResult | null>(null);

  async function load() {
    setErr(null);
    try {
      const data = await api.listCustomProviders();
      setProviders(data);
    } catch (e: any) {
      setErr(e?.message || 'Failed to load custom providers');
    }
  }

  useEffect(() => { void load(); }, []);

  function startCreate() {
    setForm({ ...EMPTY_FORM, models: [freshModelRow()] });
    setFormTestResult(null);
    setErr(null);
  }

  function startEdit(p: CustomProvider) {
    setForm({
      editingId: p.id,
      slug: p.slug,
      display_name: p.display_name,
      base_url: p.base_url,
      api_type: p.api_type,
      api_key: '',
      had_key: p.has_api_key,
      models: p.models.length ? p.models.map(normalizeModel) : [freshModelRow()],
      advanced: false,
      models_text: JSON.stringify(p.models, null, 2),
    });
    setFormTestResult(null);
    setErr(null);
  }

  function cancelForm() {
    setForm(null);
    setFormTestResult(null);
  }

  /**
   * Resolve the models array we'll send to the backend:
   *  - in advanced mode, parse + validate the JSON textarea
   *  - in row mode, validate the row state
   * Returns null + sets err on failure.
   */
  function parseModels(): CustomModelDef[] | null {
    if (!form) return null;

    if (form.advanced) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(form.models_text);
      } catch (e: any) {
        setErr(`Models JSON is invalid: ${e?.message || e}`);
        return null;
      }
      if (!Array.isArray(parsed)) {
        setErr('Models must be a JSON array of { id, name, ... } objects.');
        return null;
      }
      for (const m of parsed) {
        if (!m || typeof m !== 'object'
            || typeof (m as any).id !== 'string'
            || typeof (m as any).name !== 'string') {
          setErr('Every model needs an "id" and "name" string.');
          return null;
        }
      }
      return parsed as CustomModelDef[];
    }

    // Row mode — validate and trim.
    const out: CustomModelDef[] = [];
    for (const m of form.models) {
      const id = (m.id || '').trim();
      const name = (m.name || '').trim();
      if (!id && !name) continue;  // skip empty rows silently
      if (!id || !name) {
        setErr('Every model row needs both an id and a display name.');
        return null;
      }
      out.push({
        id,
        name,
        reasoning: !!m.reasoning,
        input: m.input?.length ? m.input : ['text'],
        cost: m.cost ?? { input: 0, output: 0 },
        contextWindow: m.contextWindow ?? DEFAULT_CONTEXT_WINDOW,
        maxTokens: m.maxTokens ?? DEFAULT_MAX_TOKENS,
      });
    }
    if (out.length === 0) {
      setErr('Add at least one model.');
      return null;
    }
    return out;
  }

  /** Helpers for row editing. */
  function updateModel(index: number, patch: Partial<CustomModelDef>) {
    if (!form) return;
    const next = form.models.slice();
    next[index] = { ...next[index], ...patch };
    setForm({ ...form, models: next });
  }
  function addModelRow() {
    if (!form) return;
    setForm({ ...form, models: [...form.models, freshModelRow()] });
  }
  function removeModelRow(index: number) {
    if (!form) return;
    const next = form.models.filter((_, i) => i !== index);
    setForm({ ...form, models: next.length ? next : [freshModelRow()] });
  }

  /**
   * Toggle between row mode and JSON mode. Going row→JSON serializes the
   * current rows into the textarea so the user doesn't lose their edits.
   * Going JSON→row parses the textarea into rows (best-effort).
   */
  function toggleAdvanced() {
    if (!form) return;
    if (!form.advanced) {
      // entering advanced
      const cleaned = form.models
        .filter((m) => (m.id || '').trim() && (m.name || '').trim())
        .map(normalizeModel);
      const text = JSON.stringify(
        cleaned.length ? cleaned : JSON.parse(DEFAULT_MODELS_JSON),
        null, 2,
      );
      setForm({ ...form, advanced: true, models_text: text });
    } else {
      // leaving advanced — try to parse back into rows
      try {
        const parsed = JSON.parse(form.models_text);
        if (Array.isArray(parsed) && parsed.every((m) => m && typeof m === 'object')) {
          setForm({
            ...form,
            advanced: false,
            models: parsed.length ? parsed.map(normalizeModel) : [freshModelRow()],
          });
          setErr(null);
          return;
        }
      } catch {
        /* fall through to error */
      }
      setErr('JSON is invalid — fix it before switching back to simple mode, or stay in advanced mode.');
    }
  }

  async function save() {
    if (!form) return;
    setErr(null);

    const models = parseModels();
    if (models === null) return;

    setBusy(true);
    try {
      if (form.editingId === null) {
        await api.createCustomProvider({
          slug: form.slug.trim().toLowerCase(),
          display_name: form.display_name.trim(),
          base_url: form.base_url.trim(),
          api_type: form.api_type,
          api_key: form.api_key.trim() || null,
          models,
        });
      } else {
        // Build a partial update — only send fields with meaningful changes.
        const patch: any = {
          display_name: form.display_name.trim(),
          base_url: form.base_url.trim(),
          api_type: form.api_type,
          models,
        };
        if (form.api_key.trim()) {
          patch.api_key = form.api_key.trim();
        }
        await api.updateCustomProvider(form.editingId, patch);
      }
      setForm(null);
      setFormTestResult(null);
      await load();
    } catch (e: any) {
      setErr(e?.message || 'Failed to save provider');
    } finally {
      setBusy(false);
    }
  }

  async function remove(p: CustomProvider) {
    if (!confirm(
      `Delete custom provider "${p.display_name}"?\n\n` +
      `Agents using its models will fall back to whatever they're configured ` +
      `to use next, or fail to start.`
    )) return;
    setErr(null);
    try {
      await api.deleteCustomProvider(p.id);
      await load();
    } catch (e: any) {
      setErr(e?.message || 'Failed to delete provider');
    }
  }

  async function testSaved(p: CustomProvider) {
    setTestingId(p.id);
    setErr(null);
    try {
      const result = await api.testCustomProvider(p.id);
      setTestResults((prev) => ({ ...prev, [p.id]: result }));
    } catch (e: any) {
      setTestResults((prev) => ({
        ...prev,
        [p.id]: { ok: false, status_code: null, latency_ms: null,
                  discovered_models: null, error: e?.message || 'Test failed',
                  endpoint: null },
      }));
    } finally {
      setTestingId(null);
    }
  }

  async function testForm() {
    if (!form) return;
    if (!form.base_url.trim()) {
      setErr('Set a base URL before testing.');
      return;
    }
    setTestingId('form');
    setErr(null);
    try {
      const result = await api.testCustomProviderPayload({
        base_url: form.base_url.trim(),
        api_type: form.api_type,
        api_key: form.api_key.trim() || null,
      });
      setFormTestResult(result);
    } catch (e: any) {
      setFormTestResult({
        ok: false, status_code: null, latency_ms: null,
        discovered_models: null, error: e?.message || 'Test failed',
        endpoint: null,
      });
    } finally {
      setTestingId(null);
    }
  }

  const slugIsValid = useMemo(() => {
    if (!form) return true;
    const v = form.slug.trim().toLowerCase();
    if (!v) return false;
    return /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(v) && v.length <= 64;
  }, [form]);

  if (providers === null) {
    return (
      <div className="section">
        <h3>Custom model providers</h3>
        <div className="muted small">Loading…</div>
      </div>
    );
  }

  return (
    <div className="section">
      <h3>Custom model providers</h3>
      <p className="muted small" style={{ marginTop: -8 }}>
        Bring your own LLM endpoint — a local llama-server, vLLM, an OpenRouter
        account, etc. Once registered, your models show up in the picker when
        creating new bots.
      </p>

      {err && <div className="error" style={{ marginBottom: 12 }}>{err}</div>}

      <ul className="member-list" style={{ marginTop: 16 }}>
        {providers.map((p) => {
          const result = testResults[p.id];
          const isTesting = testingId === p.id;
          return (
            <li key={p.id} className="member-row" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, width: '100%' }}>
                <div className="member-main">
                  <div className="member-name">
                    {p.display_name}{' '}
                    <span className="muted small" style={{ fontWeight: 400 }}>
                      ({p.namespaced_id})
                    </span>
                  </div>
                  <div className="member-email" style={{ fontFamily: 'monospace', fontSize: 12 }}>
                    {p.api_type} · {p.base_url}
                    {p.has_api_key && p.api_key_preview && (
                      <span className="muted"> · key {p.api_key_preview}</span>
                    )}
                  </div>
                  <div className="muted small" style={{ marginTop: 2 }}>
                    {p.models.length} model{p.models.length === 1 ? '' : 's'}:{' '}
                    {p.models.length
                      ? p.models.map((m) => m.id).join(', ')
                      : <em>none yet</em>}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void testSaved(p)}
                  disabled={isTesting}
                  title="Probe the endpoint to verify it's reachable"
                >
                  {isTesting ? 'Testing…' : 'Test'}
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => startEdit(p)}
                  disabled={form?.editingId === p.id}
                >
                  Edit
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={() => void remove(p)}
                  title="Delete provider"
                >
                  ✕
                </button>
              </div>

              {result && <TestResultBanner result={result} />}
            </li>
          );
        })}
        {providers.length === 0 && (
          <li className="muted small" style={{ padding: '12px 0' }}>
            No custom providers yet.
          </li>
        )}
      </ul>

      <div style={{ marginTop: 12 }}>
        {form === null ? (
          <button type="button" className="btn btn-primary btn-sm" onClick={startCreate}>
            + Add custom provider
          </button>
        ) : (
          <div className="provider-form" style={{
            border: '1px solid var(--border, #ddd)', borderRadius: 8,
            padding: 16, marginTop: 12, display: 'flex',
            flexDirection: 'column', gap: 12,
          }}>
            <h4 style={{ margin: 0 }}>
              {form.editingId === null ? 'Add provider' : `Edit "${form.display_name || form.slug}"`}
            </h4>

            {form.editingId === null && (
              <div className="field">
                <label className="field-label">Slug</label>
                <input
                  className="input"
                  value={form.slug}
                  onChange={(e) => setForm({ ...form, slug: e.target.value })}
                  placeholder="nucbox-llama"
                  maxLength={64}
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                />
                <div className="help">
                  Short id, lowercase letters/digits/hyphens. Will appear as{' '}
                  <code>u…-{form.slug.trim().toLowerCase() || 'your-slug'}</code>{' '}
                  in the config.
                </div>
                {!slugIsValid && form.slug.length > 0 && (
                  <div className="error" style={{ marginTop: 6 }}>
                    Invalid slug format.
                  </div>
                )}
              </div>
            )}

            <div className="field">
              <label className="field-label">Display name</label>
              <input
                className="input"
                value={form.display_name}
                onChange={(e) => setForm({ ...form, display_name: e.target.value })}
                placeholder="NucBox Llama"
                maxLength={120}
              />
            </div>

            <div className="field">
              <label className="field-label">Base URL</label>
              <input
                className="input"
                value={form.base_url}
                onChange={(e) => setForm({ ...form, base_url: e.target.value })}
                placeholder="http://100.124.131.18:8181"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
              />
              <div className="help">
                Where your endpoint lives. Must be reachable from this server —
                Tailscale IPs work if both boxes are on your tailnet.
              </div>
            </div>

            <div className="field">
              <label className="field-label">API type</label>
              <select
                className="input"
                value={form.api_type}
                onChange={(e) => setForm({ ...form, api_type: e.target.value })}
              >
                {API_TYPES.map((t) => (
                  <option key={t.value} value={t.value}>{t.label}</option>
                ))}
              </select>
            </div>

            <div className="field">
              <label className="field-label">
                API key {form.had_key && <span className="muted small">(currently set)</span>}
              </label>
              <input
                type="password"
                className="input"
                value={form.api_key}
                onChange={(e) => setForm({ ...form, api_key: e.target.value })}
                placeholder={form.had_key
                  ? '(leave empty to keep existing key)'
                  : 'Optional — empty for keyless local servers'}
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
              />
            </div>

            <div className="field">
              <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
                <label className="field-label">Models</label>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={toggleAdvanced}
                  title="Edit the full model JSON directly"
                >
                  {form.advanced ? 'Simple editor' : 'Advanced (JSON)'}
                </button>
              </div>

              {form.advanced ? (
                <>
                  <textarea
                    className="input"
                    value={form.models_text}
                    onChange={(e) => setForm({ ...form, models_text: e.target.value })}
                    rows={12}
                    style={{ fontFamily: 'monospace', fontSize: 12 }}
                    spellCheck={false}
                  />
                  <div className="help">
                    Array of model defs in OCPlatform's shape. Each needs at least{' '}
                    <code>id</code> and <code>name</code>.
                  </div>
                </>
              ) : (
                <>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                    {form.models.map((m, i) => (
                      <div
                        key={i}
                        style={{
                          border: '1px solid var(--border, #ddd)',
                          borderRadius: 6, padding: 12,
                          display: 'flex', flexDirection: 'column', gap: 8,
                        }}
                      >
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                          <input
                            className="input"
                            value={m.id}
                            onChange={(e) => updateModel(i, { id: e.target.value })}
                            placeholder="model id (e.g. gemma-4-E4B-it-Q4_K_M.gguf)"
                            autoCapitalize="none"
                            autoCorrect="off"
                            spellCheck={false}
                            style={{ flex: 2 }}
                          />
                          <input
                            className="input"
                            value={m.name}
                            onChange={(e) => updateModel(i, { name: e.target.value })}
                            placeholder="display name (e.g. Gemma 4)"
                            style={{ flex: 1 }}
                          />
                          {form.models.length > 1 && (
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              onClick={() => removeModelRow(i)}
                              title="Remove this model"
                              style={{ flexShrink: 0 }}
                            >
                              ✕
                            </button>
                          )}
                        </div>
                        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                          <label className="muted small" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            Context
                            <input
                              className="input"
                              type="number"
                              min={1}
                              max={10_000_000}
                              value={m.contextWindow ?? DEFAULT_CONTEXT_WINDOW}
                              onChange={(e) => updateModel(i, {
                                contextWindow: Number(e.target.value) || DEFAULT_CONTEXT_WINDOW,
                              })}
                              style={{ width: 110 }}
                            />
                            <span className="muted small">tokens</span>
                          </label>
                          <label className="muted small" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            Max output
                            <input
                              className="input"
                              type="number"
                              min={1}
                              max={1_000_000}
                              value={m.maxTokens ?? DEFAULT_MAX_TOKENS}
                              onChange={(e) => updateModel(i, {
                                maxTokens: Number(e.target.value) || DEFAULT_MAX_TOKENS,
                              })}
                              style={{ width: 100 }}
                            />
                            <span className="muted small">tokens</span>
                          </label>
                          <label className="muted small" style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                            <input
                              type="checkbox"
                              checked={!!m.reasoning}
                              onChange={(e) => updateModel(i, { reasoning: e.target.checked })}
                            />
                            Reasoning model
                          </label>
                        </div>
                      </div>
                    ))}
                  </div>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    onClick={addModelRow}
                    style={{ marginTop: 8 }}
                  >
                    + Add another model
                  </button>
                  <div className="help" style={{ marginTop: 8 }}>
                    Context window defaults to 128k. Lower it for older 8k/32k
                    models. Switch to Advanced for full control over the
                    underlying JSON (cost, input modalities, compat flags).
                  </div>
                </>
              )}
            </div>

            {formTestResult && <TestResultBanner result={formTestResult} />}

            <div className="form-actions" style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => void testForm()}
                disabled={testingId === 'form' || !form.base_url.trim()}
                title="Probe this endpoint without saving"
              >
                {testingId === 'form' ? 'Testing…' : 'Test connection'}
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={cancelForm}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={() => void save()}
                disabled={
                  busy
                  || !form.display_name.trim()
                  || !form.base_url.trim()
                  || (form.editingId === null && !slugIsValid)
                }
              >
                {busy ? 'Saving…' : form.editingId === null ? 'Create' : 'Save changes'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


/**
 * Render a probe result as a green/red banner. Shows latency, status code,
 * discovered model ids (if any), and a friendly error.
 */
function TestResultBanner({ result }: { result: ProviderTestResult }) {
  const tone = result.ok ? 'banner-info' : 'error';
  const style: React.CSSProperties = {
    marginTop: 10,
    padding: '8px 12px',
    borderRadius: 6,
    fontSize: 13,
    background: result.ok ? '#e7f7ee' : '#fdecea',
    color: result.ok ? '#0a6b2c' : '#a3261b',
    border: `1px solid ${result.ok ? '#bfe7cd' : '#f5b5af'}`,
  };
  return (
    <div className={tone} style={style}>
      {result.ok ? (
        <>
          ✅ Connected
          {result.latency_ms != null && <> · {result.latency_ms} ms</>}
          {result.status_code != null && <> · HTTP {result.status_code}</>}
          {result.discovered_models && result.discovered_models.length > 0 && (
            <div style={{ marginTop: 4, fontFamily: 'monospace', fontSize: 12, opacity: 0.85 }}>
              Discovered: {result.discovered_models.slice(0, 10).join(', ')}
              {result.discovered_models.length > 10 && ` (+${result.discovered_models.length - 10} more)`}
            </div>
          )}
        </>
      ) : (
        <>
          ❌ {result.error || 'Connection failed'}
          {result.endpoint && (
            <div style={{ marginTop: 4, fontFamily: 'monospace', fontSize: 12, opacity: 0.85 }}>
              Probed: {result.endpoint}
            </div>
          )}
        </>
      )}
    </div>
  );
}
