import { useCallback, useEffect, useState } from 'react';
import { api, type WhatsAppRoutingState } from '../api';

/**
 * WhatsApp per-contact routing.
 *
 * A WA number can be shared across multiple bots. The "default bot" (from
 * ``agents.whatsapp_login_id``) answers any contact without a rule. Each
 * row in ``rules`` overrides routing for a specific contact JID.
 *
 * Phase 1 of this feature is **read-only**: this panel surfaces the
 * current state so the user can verify the data model is correct before
 * we add Add/Remove rule controls (Phase 2) and the worker that applies
 * rules to portals (Phase 3).
 */
export function WhatsAppRoutingPanel({ waLoginId }: { waLoginId: string }) {
  const [state, setState] = useState<WhatsAppRoutingState | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setErr(null);
    try {
      const data = await api.listWhatsAppRouting(waLoginId);
      setState(data);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [waLoginId]);

  useEffect(() => { void load(); }, [load]);

  if (err) {
    return (
      <div className="section">
        <h3>Routing rules</h3>
        <div className="banner err">{err}</div>
      </div>
    );
  }
  if (!state) {
    return (
      <div className="section">
        <h3>Routing rules</h3>
        <div className="muted small">Loading…</div>
      </div>
    );
  }

  const fallback = state.rules.find((r) => r.contact_jid === '*');
  const contactRules = state.rules.filter((r) => r.contact_jid !== '*');

  // Index rules by contact_jid so the "Current portals" list can show
  // which bot would answer each contact under the current rule set.
  const ruleByJid = new Map(contactRules.map((r) => [r.contact_jid, r]));

  return (
    <div className="section">
      <h3>Routing rules <span className="muted small">for +{waLoginId}</span></h3>

      <div className="row" style={{ marginBottom: 12 }}>
        <strong>Default bot</strong>
        <span className="muted small">
          (answers any contact without a specific rule)
        </span>
      </div>
      <div style={{ marginBottom: 16 }}>
        {fallback ? (
          <code>{fallback.agent_name}</code>
        ) : state.default_bot ? (
          <>
            <code>{state.default_bot.agent_name}</code>
            <span className="muted small" style={{ marginLeft: 8 }}>
              (from the bot's primary WhatsApp binding)
            </span>
          </>
        ) : (
          <span className="muted small">— none —</span>
        )}
      </div>

      <div className="row" style={{ marginBottom: 8 }}>
        <strong>Per-contact rules</strong>
        <span className="muted small">
          ({contactRules.length} active)
        </span>
      </div>
      {contactRules.length === 0 ? (
        <div className="muted small" style={{ marginBottom: 16 }}>
          No rules yet. Phase 2 will let you add per-contact overrides
          (e.g. "messages from +66 90 996 6651 go to Bot Support").
        </div>
      ) : (
        <ul className="list-unstyled" style={{ marginBottom: 16 }}>
          {contactRules.map((r) => (
            <li key={r.id} className="row" style={{ padding: '4px 0' }}>
              <code>{r.contact_phone ?? r.contact_jid}</code>
              <span className="muted small">→</span>
              <code>{r.agent_name}</code>
              <span className="muted small">priority {r.priority}</span>
            </li>
          ))}
        </ul>
      )}

      <div className="row" style={{ marginBottom: 8 }}>
        <strong>Current portal rooms</strong>
        <span className="muted small">
          ({state.portals.length} on this number)
        </span>
      </div>
      {state.portals.length === 0 ? (
        <div className="muted small">
          No portal rooms yet. They appear here once you start a WA
          conversation with this number.
        </div>
      ) : (
        <ul className="list-unstyled">
          {state.portals.map((p) => {
            const matched = ruleByJid.get(p.contact_jid);
            const routedTo =
              matched?.agent_name ??
              state.default_bot?.agent_name ??
              '— none —';
            return (
              <li key={p.portal_mxid} className="row" style={{ padding: '4px 0' }}>
                <code>{p.contact_phone ?? p.contact_jid}</code>
                <span className="muted small">→</span>
                <code>{routedTo}</code>
                {matched ? (
                  <span className="muted small">(rule)</span>
                ) : (
                  <span className="muted small">(default)</span>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <div className="muted small" style={{ marginTop: 16, fontStyle: 'italic' }}>
        Phase 1 — read-only. Add/remove controls land in Phase 2.
      </div>
    </div>
  );
}
