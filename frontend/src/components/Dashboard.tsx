import { useEffect, useMemo, useState } from 'react';
import { api, type Agent } from '../api';

export function Dashboard({
  onCreate,
  onOpen,
  onChat,
  onRooms,
  onSkills,
}: {
  onCreate: () => void;
  onOpen: (id: number) => void;
  onChat: (id: number) => void;
  /** Open the per-bot Matrix rooms list. Owner-only feature — the button
   *  is hidden for members in the grid. */
  onRooms: (id: number, name: string) => void;
  /** Open the per-bot skills list. Owner-only — skills describe internal
   *  capabilities so members don't get the audit view. */
  onSkills: (id: number, name: string) => void;
}) {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.listAgents().then(setAgents).catch((e) => setErr(e.message));
  }, []);

  const { owned, shared } = useMemo(() => {
    const owned: Agent[] = [];
    const shared: Agent[] = [];
    for (const a of agents || []) {
      (a.my_role === 'owner' ? owned : shared).push(a);
    }
    return { owned, shared };
  }, [agents]);

  return (
    <div>
      <div className="list-head">
        <div>
          <h2>Your agents <span className="muted">({agents?.length ?? '…'})</span></h2>
          <p className="muted small">Each agent runs in its own OpenClaw workspace.</p>
        </div>
        <button className="btn btn-primary" onClick={onCreate}>+ New agent</button>
      </div>

      {err && <div className="banner-error">{err}</div>}
      {agents === null && !err && <div className="loading">Loading…</div>}
      {agents && agents.length === 0 && (
        <div className="empty-state">
          <h3>No agents yet</h3>
          <p>Create your first agent to get started.</p>
          <button className="btn btn-primary" onClick={onCreate} style={{ marginTop: 16 }}>+ New agent</button>
        </div>
      )}

      {agents && owned.length > 0 && (
        <section className="agent-section">
          <h3 className="section-heading">
            Owned by me <span className="muted small">({owned.length})</span>
          </h3>
          <AgentGrid agents={owned} onOpen={onOpen} onChat={onChat} onRooms={onRooms} onSkills={onSkills} />
        </section>
      )}

      {agents && shared.length > 0 && (
        <section className="agent-section">
          <h3 className="section-heading">
            Shared with me <span className="muted small">({shared.length})</span>
          </h3>
          <AgentGrid agents={shared} onOpen={onOpen} onChat={onChat} onRooms={onRooms} onSkills={onSkills} />
        </section>
      )}
    </div>
  );
}

/**
 * Grid of agent cards. The card behaviour differs by role:
 *  - owner  → clicking the card opens the detail view (with settings, members, etc).
 *  - member → clicking the card opens the chat (members can't edit settings).
 */
function AgentGrid({
  agents,
  onOpen,
  onChat,
  onRooms,
  onSkills,
}: {
  agents: Agent[];
  onOpen: (id: number) => void;
  onChat: (id: number) => void;
  onRooms: (id: number, name: string) => void;
  onSkills: (id: number, name: string) => void;
}) {
  return (
    <div className="agent-grid">
      {agents.map((a) => {
        const isOwner = a.my_role === 'owner';
        const cardOnClick = () => (isOwner ? onOpen(a.id) : onChat(a.id));
        return (
          <div key={a.id} className="agent-card" onClick={cardOnClick}>
            <h3>
              <span className="agent-emoji">{a.emoji || '🤖'}</span>
              <span>{a.display_name}</span>
            </h3>
            <div className="agent-id">{a.harness_agent_id}</div>
            <div className="agent-meta">
              {isOwner && (
                <>
                  <div><strong>Model:</strong> {a.model || '—'}</div>
                  <div><strong>Harness:</strong> {a.harness}</div>
                </>
              )}
              <div style={{ display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                {!isOwner && <span className="badge badge-member">Member</span>}
                {a.has_telegram && isOwner && <span className="badge badge-telegram">Telegram</span>}
                {a.matrix_user_id && <span className="badge badge-matrix">Matrix</span>}
              </div>
            </div>
            {a.matrix_user_id && (
              <div className="agent-actions" onClick={(e) => e.stopPropagation()}>
                <button
                  className="btn btn-secondary btn-sm grow-0"
                  onClick={() => onChat(a.id)}
                >
                  💬 Chat
                </button>
                {isOwner && (
                  <button
                    className="btn btn-ghost btn-sm grow-0"
                    onClick={() => onRooms(a.id, a.display_name)}
                    title="See all Matrix rooms this bot is in"
                  >
                    🚪 Rooms
                    {a.room_count !== null && a.room_count !== undefined && (
                      <span className="room-count-badge" aria-label={`${a.room_count} rooms`}>
                        {a.room_count}
                      </span>
                    )}
                  </button>
                )}
                {isOwner && (
                  <button
                    className="btn btn-ghost btn-sm grow-0"
                    onClick={() => onSkills(a.id, a.display_name)}
                    title="See the skills installed in this bot's workspace"
                  >
                    🧰 Skills
                    {a.skill_count !== null && a.skill_count !== undefined && (
                      <span className="room-count-badge" aria-label={`${a.skill_count} skills`}>
                        {a.skill_count}
                      </span>
                    )}
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
