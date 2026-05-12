import { useCallback, useEffect, useRef, useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import {
  api,
  type WhatsAppLogin,
  type WhatsAppLoginStep,
  type WhatsAppStatus,
} from '../api';

/**
 * WhatsApp bridge panel — Settings page section.
 *
 * Lets the signed-in user link/unlink WhatsApp accounts through the
 * mautrix-whatsapp bridge. Pairing happens inline:
 *
 *   1. User clicks "Link a number" → POST /login/start with flow_id=qr
 *   2. Backend returns the first step (display_and_wait with QR data)
 *   3. We render the QR client-side and start a long-poll loop on
 *      /login/step. The poll returns when the QR rotates (new data) or
 *      when the user scans (different step type).
 *   4. On "complete" we close the modal and refresh the linked list.
 *
 * Phone-code flow is supported too — the same step driver handles the
 * user_input step that asks for the phone number.
 */
export function WhatsAppPanel() {
  const [status, setStatus] = useState<WhatsAppStatus | 'loading' | 'error'>('loading');
  const [logins, setLogins] = useState<WhatsAppLogin[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pairing, setPairing] = useState<WhatsAppLoginStep | null>(null);
  const [pairBusy, setPairBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  // --- Initial load -----------------------------------------------------------
  const refresh = useCallback(async () => {
    setErr(null);
    try {
      const s = await api.whatsappStatus();
      setStatus(s);
      if (s.configured) {
        const lst = await api.whatsappListLogins();
        setLogins(lst);
      }
    } catch (e) {
      setStatus('error');
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  // Cancel any in-flight poll when the component unmounts or pairing closes.
  useEffect(() => () => abortRef.current?.abort(), []);

  // --- Pairing loop -----------------------------------------------------------
  /**
   * Drive the pairing state machine. Recursive: each step's response
   * tells us what to do next. We keep going until type === 'complete'
   * or the user cancels.
   */
  const driveStep = useCallback(async (step: WhatsAppLoginStep) => {
    setPairing(step);
    if (step.type === 'complete') {
      // Done — refresh the linked list and clear pairing UI after a beat.
      await refresh();
      setTimeout(() => setPairing(null), 1500);
      return;
    }
    if (step.type !== 'display_and_wait') {
      // user_input or other — wait for the user (handled by form submit handlers below).
      return;
    }

    // Long-poll for the next step transition (QR rotates, or user scans).
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const next = await api.whatsappLoginStep({
        login_id: step.login_id,
        step_id: step.step_id,
        action: 'display_and_wait',
      }, ac.signal);
      // Recurse — the next step might be another display_and_wait
      // (QR rotated) or 'complete' (user scanned).
      await driveStep(next);
    } catch (e) {
      if (ac.signal.aborted) return;  // user cancelled
      const msg = e instanceof Error ? e.message : String(e);
      // Gateway/504 timeouts are normal — retry the call with the same step.
      if (/504|timed out/i.test(msg)) {
        await driveStep(step);
        return;
      }
      setErr(`Pairing error: ${msg}`);
      setPairing(null);
    }
  }, [refresh]);

  const startPairing = useCallback(async (flowId: 'qr' | 'phone') => {
    setErr(null);
    setPairBusy(true);
    try {
      const first = await api.whatsappStartLogin(flowId);
      await driveStep(first);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setPairBusy(false);
    }
  }, [driveStep]);

  const cancelPairing = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPairing(null);
  }, []);

  const unlinkLogin = useCallback(async (login: WhatsAppLogin) => {
    if (!confirm(`Unlink ${login.name || login.id}? Any bots assigned to this number will be detached.`)) {
      return;
    }
    setErr(null);
    try {
      const res = await api.whatsappDeleteLogin(login.id);
      if (res.detached_bots > 0) {
        alert(`${res.detached_bots} bot(s) were detached.`);
      }
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [refresh]);

  // --- Render -----------------------------------------------------------------
  if (status === 'loading') {
    return (
      <div className="section">
        <h3>WhatsApp</h3>
        <div className="muted small">Loading…</div>
      </div>
    );
  }

  if (status === 'error' || !status.configured) {
    return (
      <div className="section">
        <h3>WhatsApp</h3>
        <div className="muted small">
          WhatsApp bridge is not configured on this server.
          {err ? <> <span style={{ color: 'var(--err)' }}>({err})</span></> : null}
        </div>
      </div>
    );
  }

  return (
    <div className="section">
      <h3>WhatsApp</h3>
      <div className="muted small" style={{ marginBottom: 12 }}>
        Link a WhatsApp number, then assign it to a bot to have the bot answer
        DMs sent to that number.
      </div>

      {err && (
        <div className="banner err" style={{ marginBottom: 12 }}>
          {err}
        </div>
      )}

      {logins === null ? (
        <div className="muted small">Loading linked numbers…</div>
      ) : logins.length === 0 ? (
        <div className="muted small" style={{ marginBottom: 12 }}>
          No WhatsApp numbers linked yet.
        </div>
      ) : (
        <ul className="list-unstyled" style={{ marginBottom: 12 }}>
          {logins.map((lg) => (
            <li key={lg.id} className="row" style={{
              padding: '8px 12px',
              border: '1px solid var(--border)',
              borderRadius: 6,
              marginBottom: 6,
              display: 'flex',
              alignItems: 'center',
              gap: 12,
            }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600 }}>
                  {lg.name || lg.id}
                </div>
                <div className="muted small" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                  {lg.id}
                </div>
              </div>
              <button className="btn-danger" onClick={() => unlinkLogin(lg)}>
                Unlink
              </button>
            </li>
          ))}
        </ul>
      )}

      <div style={{ display: 'flex', gap: 8 }}>
        <button
          className="btn"
          onClick={() => startPairing('qr')}
          disabled={pairBusy || pairing !== null}
        >
          Link a number (QR)
        </button>
        <button
          className="btn"
          onClick={() => startPairing('phone')}
          disabled={pairBusy || pairing !== null}
        >
          Link with phone code
        </button>
      </div>

      {pairing && (
        <PairingModal
          step={pairing}
          onCancel={cancelPairing}
          onSubmitInput={async (payload) => {
            // For phone-code flow: collect the number, send user_input,
            // then drive whatever step comes next.
            setErr(null);
            try {
              const next = await api.whatsappLoginStep({
                login_id: pairing.login_id,
                step_id: pairing.step_id,
                action: 'user_input',
                payload,
              });
              await driveStep(next);
            } catch (e) {
              setErr(e instanceof Error ? e.message : String(e));
            }
          }}
        />
      )}
    </div>
  );
}

// --- Pairing modal ----------------------------------------------------------

function PairingModal({
  step,
  onCancel,
  onSubmitInput,
}: {
  step: WhatsAppLoginStep;
  onCancel: () => void;
  onSubmitInput: (payload: Record<string, string>) => Promise<void>;
}) {
  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        background: 'rgba(0,0,0,0.6)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        zIndex: 1000,
      }}
      onClick={onCancel}
    >
      <div
        style={{
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          borderRadius: 8,
          padding: 24,
          maxWidth: 420,
          width: '90%',
          textAlign: 'center',
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h3 style={{ marginTop: 0 }}>Link WhatsApp</h3>
        {step.instructions && (
          <div className="muted small" style={{ marginBottom: 16 }}>
            {step.instructions}
          </div>
        )}

        <StepBody step={step} onSubmitInput={onSubmitInput} />

        {step.type === 'complete' && (
          <div style={{ marginTop: 12, color: 'var(--ok, #4ade80)' }}>
            ✓ Linked successfully
          </div>
        )}

        <div style={{ marginTop: 20 }}>
          <button className="btn" onClick={onCancel}>
            {step.type === 'complete' ? 'Close' : 'Cancel'}
          </button>
        </div>
      </div>
    </div>
  );
}

function StepBody({
  step,
  onSubmitInput,
}: {
  step: WhatsAppLoginStep;
  onSubmitInput: (payload: Record<string, string>) => Promise<void>;
}) {
  if (step.type === 'display_and_wait' && step.display_and_wait?.type === 'qr') {
    return (
      <div style={{
        background: 'white',
        padding: 16,
        borderRadius: 8,
        display: 'inline-block',
      }}>
        <QRCodeSVG value={step.display_and_wait.data} size={256} level="M" />
      </div>
    );
  }

  if (step.type === 'display_and_wait' && step.display_and_wait?.type === 'code') {
    return (
      <div style={{
        fontFamily: 'monospace',
        fontSize: 32,
        letterSpacing: 4,
        padding: '16px 24px',
        background: 'var(--surface-2, rgba(255,255,255,0.05))',
        borderRadius: 8,
        display: 'inline-block',
      }}>
        {step.display_and_wait.data}
      </div>
    );
  }

  if (step.type === 'user_input' && step.user_input?.fields) {
    return <UserInputForm fields={step.user_input.fields} onSubmit={onSubmitInput} />;
  }

  return null;
}

function UserInputForm({
  fields,
  onSubmit,
}: {
  fields: Array<{ type: string; id: string; name: string; description?: string }>;
  onSubmit: (payload: Record<string, string>) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map(f => [f.id, ''])));
  const [busy, setBusy] = useState(false);

  return (
    <form
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        try {
          await onSubmit(values);
        } finally {
          setBusy(false);
        }
      }}
      style={{ display: 'flex', flexDirection: 'column', gap: 12 }}
    >
      {fields.map((f) => (
        <label key={f.id} style={{ textAlign: 'left' }}>
          <div style={{ marginBottom: 4 }}>{f.name}</div>
          {f.description && (
            <div className="muted small" style={{ marginBottom: 4 }}>
              {f.description}
            </div>
          )}
          <input
            className="input"
            type={f.type === 'phone_number' ? 'tel' : 'text'}
            value={values[f.id] || ''}
            onChange={(e) => setValues({ ...values, [f.id]: e.target.value })}
            required
            style={{ width: '100%' }}
          />
        </label>
      ))}
      <button type="submit" className="btn" disabled={busy}>
        {busy ? 'Submitting…' : 'Continue'}
      </button>
    </form>
  );
}
