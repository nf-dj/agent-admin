import { useEffect, useMemo, useState } from 'react';
import { api, type User } from '../api';
import { ApiKeysPanel } from './ApiKeysPanel';
import { CustomProvidersPanel } from './CustomProvidersPanel';
import { WhatsAppPanel } from './WhatsAppPanel';

/**
 * User Settings page.
 *
 * Lets the signed-in user edit:
 *   • display name
 *   • company prefix (used when generating new bot ids)
 *
 * Hitting Save calls PATCH /api/me/settings. Existing bot ids are not
 * renamed — only future bot creations use the new prefix.
 */
export function SettingsView({
  initialUser,
  onSaved,
  onBack,
}: {
  initialUser: User;
  /** Called with the updated user on successful save so the parent can refresh state. */
  onSaved: (u: User) => void;
  onBack: () => void;
}) {
  const [displayName, setDisplayName] = useState(initialUser.display_name ?? '');
  const [companyPrefix, setCompanyPrefix] = useState(initialUser.company_prefix ?? '');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Re-sync if the parent passes in a fresher user later (e.g. after another save).
  useEffect(() => {
    setDisplayName(initialUser.display_name ?? '');
    setCompanyPrefix(initialUser.company_prefix ?? '');
  }, [initialUser.id, initialUser.display_name, initialUser.company_prefix]);

  /**
   * Live preview of what the next bot id will look like.
   * Mirrors the backend logic in `_build_harness_agent_id`.
   */
  const previewBotId = useMemo(() => {
    const trimmed = companyPrefix.trim().toLowerCase();
    const prefix = trimmed ? `${trimmed}-` : `u${initialUser.id}-`;
    return `${prefix}mybot`;
  }, [companyPrefix, initialUser.id]);

  /** Same regex as the backend validator — friendly client-side hint. */
  const prefixIsValid = useMemo(() => {
    const v = companyPrefix.trim().toLowerCase();
    if (!v) return true;  // empty is fine (clears it)
    return /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(v) && v.length <= 32;
  }, [companyPrefix]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (!prefixIsValid) {
      setErr('Prefix must be lowercase letters, digits, and hyphens only (no leading/trailing/double hyphens).');
      return;
    }
    setBusy(true);
    try {
      // Build a diff: only send fields that actually changed, so an empty
      // string explicitly means "clear" (vs. undefined = leave alone).
      const body: { display_name?: string | null; company_prefix?: string | null } = {};
      const newDN = displayName.trim();
      if ((initialUser.display_name ?? '') !== newDN) {
        body.display_name = newDN;   // empty string → backend will null it
      }
      const newCP = companyPrefix.trim().toLowerCase();
      if ((initialUser.company_prefix ?? '') !== newCP) {
        body.company_prefix = newCP; // empty string → backend will null it
      }
      const updated = await api.updateSettings(body);
      onSaved(updated);
      setSavedAt(Date.now());
    } catch (e: any) {
      setErr(e?.message || 'Failed to save settings');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="settings-view">
      <div className="page-header">
        <button type="button" className="btn btn-ghost btn-sm" onClick={onBack}>
          ← Back
        </button>
        <h2>Settings</h2>
      </div>

      <form className="section" onSubmit={save}>
        <h3>Profile</h3>

        <div className="field">
          <label className="field-label">Email</label>
          <input className="input" value={initialUser.email} disabled />
          <div className="help">Email can't be changed here. Contact an admin if you need to update it.</div>
        </div>

        <div className="field">
          <label className="field-label">Display name</label>
          <input
            className="input"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="e.g. David"
            maxLength={120}
          />
        </div>

        <h3 style={{ marginTop: 24 }}>Bot naming</h3>

        <div className="field">
          <label className="field-label">Company prefix</label>
          <input
            className="input"
            value={companyPrefix}
            onChange={(e) => setCompanyPrefix(e.target.value)}
            placeholder={`(empty — uses u${initialUser.id})`}
            maxLength={32}
            // Hint mobile keyboards to stay lowercase / no autocorrect of slugs.
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
          <div className="help">
            Used as the prefix for new bot ids. Lowercase letters, digits, and
            hyphens only (max 32 chars). Leave empty to use the default{' '}
            <code>u{initialUser.id}</code> prefix.
          </div>
          <div className="settings-preview">
            Next bot id preview: <code>{previewBotId}</code>
          </div>
          {!prefixIsValid && (
            <div className="error" style={{ marginTop: 8 }}>
              Invalid prefix format.
            </div>
          )}
        </div>

        {err && <div className="error">{err}</div>}
        {savedAt && !err && (
          <div className="banner-info" style={{ margin: '12px 0' }}>
            Settings saved.
          </div>
        )}

        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={busy || !prefixIsValid}>
            {busy ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      </form>

      <div className="muted small" style={{ marginTop: 16, marginBottom: 24 }}>
        Note: Changing the prefix only affects <em>new</em> bots. Existing bot
        ids stay the same.
      </div>

      <ApiKeysPanel />

      <CustomProvidersPanel />

      <WhatsAppPanel />
    </div>
  );
}
