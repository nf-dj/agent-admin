/**
 * MembersPanel — list current members of an agent, invite by email, remove.
 *
 * Owner-only edit surface (renders read-only for members). Drop into the
 * AgentDetailView when the current user is the owner.
 */
import { useEffect, useState } from 'react';
import { api, type AgentMember } from '../api';

export function MembersPanel({ agentId, isOwner }: { agentId: number; isOwner: boolean }) {
  const [members, setMembers] = useState<AgentMember[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [email, setEmail] = useState('');
  const [inviting, setInviting] = useState(false);
  const [info, setInfo] = useState<string | null>(null);

  async function load() {
    try {
      const ms = await api.listMembers(agentId);
      setMembers(ms);
    } catch (e: any) {
      setErr(e.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  async function invite(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    setInfo(null);
    const addr = email.trim();
    if (!addr) return;
    setInviting(true);
    try {
      const m = await api.inviteMember(agentId, addr);
      setMembers((prev) => [...(prev || []), m]);
      setEmail('');
      setInfo(`Added ${m.email} as a member.`);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setInviting(false);
    }
  }

  async function remove(userId: number, label: string) {
    if (!confirm(`Remove ${label} from this agent?`)) return;
    setErr(null);
    setInfo(null);
    try {
      await api.removeMember(agentId, userId);
      setMembers((prev) => (prev || []).filter((m) => m.user_id !== userId));
      setInfo(`Removed ${label}.`);
    } catch (e: any) {
      setErr(e.message);
    }
  }

  return (
    <div className="section">
      <h3>Members</h3>
      <p className="muted small">
        {isOwner
          ? 'Invite other users to chat with this bot. Members can chat only — they cannot edit settings or invite others.'
          : 'These users have access to this bot.'}
      </p>

      {err && <div className="banner-error">{err}</div>}
      {info && <div className="banner-info">{info}</div>}

      {members === null ? (
        <div className="loading">Loading…</div>
      ) : (
        <ul className="member-list">
          {members.map((m) => (
            <li key={m.user_id} className="member-row">
              <div className="member-main">
                <div className="member-name">
                  {m.display_name || m.email}
                  {' '}
                  <span className={`badge ${m.role === 'owner' ? 'badge-owner' : 'badge-member'}`}>
                    {m.role}
                  </span>
                </div>
                <div className="member-email muted small">{m.email}</div>
              </div>
              {isOwner && m.role !== 'owner' && (
                <button
                  className="btn btn-ghost btn-sm grow-0"
                  onClick={() => remove(m.user_id, m.email)}
                  title="Remove this member"
                >
                  ✕
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {isOwner && (
        <form onSubmit={invite} className="member-invite-form">
          <input
            type="email"
            className="input"
            placeholder="email@example.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            disabled={inviting}
            required
          />
          <button
            type="submit"
            className="btn btn-primary grow-0"
            disabled={inviting || !email.trim()}
          >
            {inviting ? 'Inviting…' : 'Invite'}
          </button>
        </form>
      )}
      {isOwner && (
        <div className="help">
          The invitee must already have an account on this site. Ask them to{' '}
          <strong>sign up first</strong>, then enter their email here.
        </div>
      )}
    </div>
  );
}
