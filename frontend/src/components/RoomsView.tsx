import { useEffect, useState } from 'react';
import { api, type AgentRoom } from '../api';
import { RoomMessagesView } from './RoomMessagesView';

/**
 * Owner-only audit view: every Matrix room this bot is currently in.
 *
 * Lists DMs first (typically the most interesting — "who's talking to my
 * bot"), then other rooms. Pulled live from the bot's own Matrix account
 * via the backend (which logs in as the bot with its stored access token).
 *
 * Empty state covers two cases:
 *   * Bot has no Matrix account provisioned yet (rare — only for legacy
 *     agents created before Matrix integration).
 *   * Bot has a Matrix account but is in zero rooms.
 *
 * No actions yet — just visibility. Future: kick, leave, mute.
 */
export function RoomsView({
  agentId,
  agentName,
  onBack,
}: {
  agentId: number;
  agentName: string;
  onBack: () => void;
}) {
  const [rooms, setRooms] = useState<AgentRoom[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  /** Currently-selected room (drill-down to message timeline). */
  const [openRoom, setOpenRoom] = useState<{ id: string; label: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRooms(null);
    setErr(null);
    api.listAgentRooms(agentId)
      .then((r) => { if (!cancelled) setRooms(r); })
      .catch((e) => { if (!cancelled) setErr(e?.message || 'Failed to load rooms'); });
    return () => { cancelled = true; };
  }, [agentId]);

  /** Strip the homeserver part of a matrix id for compact display. */
  const shortId = (mxid: string) => mxid.split(':')[0];

  // Drill-down view: message timeline + compose box for the selected room.
  if (openRoom) {
    return (
      <RoomMessagesView
        agentId={agentId}
        agentName={agentName}
        roomId={openRoom.id}
        roomLabel={openRoom.label}
        onBack={() => setOpenRoom(null)}
      />
    );
  }

  return (
    <div className="settings-view">
      <div className="page-header">
        <button className="btn btn-ghost" onClick={onBack}>← Back</button>
        <h2 style={{ margin: 0 }}>Rooms — {agentName}</h2>
      </div>

      <p className="muted small">
        Every Matrix room this bot is currently joined to. Useful for seeing
        who has an open conversation with your bot.
      </p>

      {err && <div className="error">{err}</div>}

      {rooms === null && !err && (
        <div className="muted small">Loading…</div>
      )}

      {rooms !== null && rooms.length === 0 && (
        <div className="empty-state" style={{ marginTop: 16 }}>
          <p>No rooms yet.</p>
          <p className="muted small">
            When someone starts a chat with this bot — from this app or any
            Matrix client — the room will appear here.
          </p>
        </div>
      )}

      {rooms && rooms.length > 0 && (
        <ul className="member-list" style={{ marginTop: 16 }}>
          {rooms.map((r) => (
            <li
              key={r.room_id}
              className="member-row skill-row"
              onClick={() => setOpenRoom({
                id: r.room_id,
                label: r.is_dm && r.other_user_id ? shortId(r.other_user_id) : r.name,
              })}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter') setOpenRoom({
                  id: r.room_id,
                  label: r.is_dm && r.other_user_id ? shortId(r.other_user_id) : r.name,
                });
              }}
            >
              <div className="member-main">
                <div className="member-name">
                  {r.is_dm && r.other_user_id ? shortId(r.other_user_id) : r.name}
                </div>
                <div className="member-email" style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  {r.is_dm ? (
                    <span className="badge badge-matrix">DM</span>
                  ) : (
                    <span className="badge">{r.member_count} member{r.member_count === 1 ? '' : 's'}</span>
                  )}
                  <span className="muted" style={{ fontFamily: 'monospace', fontSize: 11 }}>
                    {shortId(r.room_id)}
                  </span>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
