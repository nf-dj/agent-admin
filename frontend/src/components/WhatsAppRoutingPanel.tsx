import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  api,
  type WhatsAppRoutingState,
  type WhatsAppContactOption,
  type Agent,
} from '../api';

/**
 * WhatsApp per-contact routing.
 *
 * A WA number can be shared across multiple bots. The "default bot" (from
 * ``agents.whatsapp_login_id``) answers any contact without a rule. Each
 * row in ``rules`` overrides routing for a specific contact JID.
 *
 * Phase 2: full CRUD on rules. Rules are declarative — saving one does
 * not yet move bots between portal rooms (that's Phase 3). The "Current
 * portals" list shows the routing decision each contact *would* get if
 * Phase 3 were enabled.
 */
export function WhatsAppRoutingPanel({ waLoginId }: { waLoginId: string }) {
  const [state, setState] = useState<WhatsAppRoutingState | null>(null);
  const [contacts, setContacts] = useState<WhatsAppContactOption[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Add-rule form state
  const [contactPick, setContactPick] = useState<string>('');     // dropdown
  const [contactManual, setContactManual] = useState<string>(''); // text fallback
  const [agentPick, setAgentPick] = useState<string>('');
  const [priority, setPriority] = useState<string>('100');

  const load = useCallback(async () => {
    setErr(null);
    try {
      const [routing, contactList, agentList] = await Promise.all([
        api.listWhatsAppRouting(waLoginId),
        api.listWhatsAppContacts(waLoginId).catch(() => [] as WhatsAppContactOption[]),
        api.listAgents().catch(() => [] as Agent[]),
      ]);
      setState(routing);
      setContacts(contactList);
      setAgents(agentList);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [waLoginId]);

  useEffect(() => { void load(); }, [load]);

  // Bots that can be routed to: caller's own bots that have a Matrix MXID.
  // ``Agent`` summary doesn't carry matrix_user_id; backend will reject
  // bots without it. We just filter by owner here for the dropdown.
  const eligibleAgents = useMemo(
    () => agents.filter((a) => a.my_role === 'owner'),
    [agents],
  );

  const reset = () => {
    setContactPick('');
    setContactManual('');
    setAgentPick('');
    setPriority('100');
  };

  const submit = useCallback(async () => {
    if (!agentPick) {
      setErr('Pick a bot.');
      return;
    }
    const contact = (contactManual.trim() || contactPick).trim();
    if (!contact) {
      setErr('Pick a contact or enter a phone / "*".');
      return;
    }
    const prio = Number.parseInt(priority, 10);
    if (!Number.isFinite(prio)) {
      setErr('Priority must be a number.');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const next = await api.upsertWhatsAppRoutingRule(waLoginId, {
        contact,
        agent_id: Number.parseInt(agentPick, 10),
        priority: prio,
      });
      setState(next);
      reset();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [agentPick, contactManual, contactPick, priority, waLoginId]);

  const remove = useCallback(async (ruleId: number) => {
    if (!confirm('Delete this routing rule?')) return;
    setBusy(true);
    setErr(null);
    try {
      const next = await api.deleteWhatsAppRoutingRule(waLoginId, ruleId);
      setState(next);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [waLoginId]);

  if (!state && !err) {
    return (
      <div className="section">
        <h3>Routing rules</h3>
        <div className="muted small">Loading…</div>
      </div>
    );
  }

  const fallback = state?.rules.find((r) => r.contact_jid === '*');
  const contactRules = state?.rules.filter((r) => r.contact_jid !== '*') ?? [];

  // Index rules by contact_jid for the portals list.
  const ruleByJid = new Map(contactRules.map((r) => [r.contact_jid, r]));

  // Contacts already covered by a rule — hide them from the dropdown so
  // the user can't accidentally double-route. (Editing comes via "click
  // the rule → delete → re-add"; for v1 that's fine.)
  const ruleJids = new Set(contactRules.map((r) => r.contact_jid));
  const availableContacts = contacts.filter((c) => !ruleJids.has(c.contact_jid));

  return (
    <div className="section">
      <h3>Routing rules <span className="muted small">for +{waLoginId}</span></h3>

      {err && <div className="banner err" style={{ marginBottom: 12 }}>{err}</div>}

      <div className="row" style={{ marginBottom: 12 }}>
        <strong>Default bot</strong>
        <span className="muted small">
          (answers any contact without a specific rule)
        </span>
      </div>
      <div style={{ marginBottom: 16 }}>
        {fallback ? (
          <code>{fallback.agent_name}</code>
        ) : state?.default_bot ? (
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
        <span className="muted small">({contactRules.length} active)</span>
      </div>
      {contactRules.length === 0 ? (
        <div className="muted small" style={{ marginBottom: 16 }}>
          No rules yet. Use the form below to route specific contacts to
          specific bots.
        </div>
      ) : (
        <ul className="list-unstyled" style={{ marginBottom: 16 }}>
          {contactRules.map((r) => (
            <li key={r.id} className="row" style={{ padding: '4px 0' }}>
              <code>{r.contact_phone ?? r.contact_jid}</code>
              <span className="muted small">→</span>
              <code>{r.agent_name}</code>
              <span className="muted small">priority {r.priority}</span>
              <button
                className="btn-secondary"
                style={{ marginLeft: 'auto' }}
                onClick={() => void remove(r.id)}
                disabled={busy}
              >
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}

      {/* --- Add rule form --- */}
      <div
        className="row"
        style={{
          marginBottom: 16,
          gap: 8,
          flexWrap: 'wrap',
          alignItems: 'flex-end',
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 220 }}>
          <label className="muted small" htmlFor="wa-rule-contact">
            Contact
          </label>
          <select
            id="wa-rule-contact"
            className="input"
            value={contactPick}
            onChange={(e) => { setContactPick(e.target.value); setContactManual(''); }}
            disabled={busy}
          >
            <option value="">— pick a contact —</option>
            <option value="*">* (fallback for all unmatched)</option>
            {availableContacts.map((c) => (
              <option key={c.contact_jid} value={c.contact_jid}>
                {c.contact_phone ?? c.contact_jid}
                {c.name ? ` — ${c.name}` : ''}
              </option>
            ))}
          </select>
          <input
            className="input"
            style={{ marginTop: 4 }}
            placeholder="…or type a phone (e.g. 66909966651)"
            value={contactManual}
            onChange={(e) => { setContactManual(e.target.value); setContactPick(''); }}
            disabled={busy}
          />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', minWidth: 180 }}>
          <label className="muted small" htmlFor="wa-rule-bot">Bot</label>
          <select
            id="wa-rule-bot"
            className="input"
            value={agentPick}
            onChange={(e) => setAgentPick(e.target.value)}
            disabled={busy}
          >
            <option value="">— pick a bot —</option>
            {eligibleAgents.map((a) => (
              <option key={a.id} value={a.id}>
                {a.display_name}
              </option>
            ))}
          </select>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', maxWidth: 90 }}>
          <label className="muted small" htmlFor="wa-rule-prio">Priority</label>
          <input
            id="wa-rule-prio"
            className="input"
            type="number"
            value={priority}
            onChange={(e) => setPriority(e.target.value)}
            disabled={busy}
          />
        </div>

        <button className="btn" onClick={() => void submit()} disabled={busy}>
          {busy ? 'Saving…' : 'Add rule'}
        </button>
      </div>

      <div className="row" style={{ marginBottom: 8 }}>
        <strong>Current portal rooms</strong>
        <span className="muted small">
          ({state?.portals.length ?? 0} on this number)
        </span>
      </div>
      {(state?.portals.length ?? 0) === 0 ? (
        <div className="muted small">
          No portal rooms yet. They appear here once you start a WA
          conversation with this number.
        </div>
      ) : (
        <ul className="list-unstyled">
          {state!.portals.map((p) => {
            const matched = ruleByJid.get(p.contact_jid);
            const routedTo =
              matched?.agent_name ??
              state!.default_bot?.agent_name ??
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
        Rules are declarative for now — saving one doesn't yet move bots
        between portal rooms. That worker lands in Phase 3.
      </div>
    </div>
  );
}
