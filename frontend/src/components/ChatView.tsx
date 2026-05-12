/**
 * ChatView — DM the bot directly via Matrix (matrix-js-sdk in the browser).
 *
 * Lifecycle:
 *   1. Fetch agent details (we need its `matrix_user_id`).
 *   2. Fetch web-Matrix credentials for the current user
 *      (auto-provisioned by the backend on first call).
 *   3. Boot matrix-js-sdk, run an initial sync.
 *   4. Find an existing DM room with the bot, or create one and invite it.
 *      The bot's gateway has `autoJoin: always`, so it joins immediately.
 *   5. Stream timeline events into local state; render & let the user send.
 *
 * v1 keeps things simple: plaintext rooms (the bots run with `encryption=false`),
 * no E2EE, no history pagination beyond the initial sync, no typing indicators.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  createClient,
  type MatrixClient,
  type MatrixEvent,
  type Room,
  type RoomMember,
  ClientEvent,
  RoomEvent,
  RoomMemberEvent,
  EventType,
  Preset,
} from 'matrix-js-sdk';
import { api, type AgentChatInfo, type MatrixCreds } from '../api';

type Status =
  | { phase: 'idle' }
  | { phase: 'fetching' }
  | { phase: 'connecting' }
  | { phase: 'syncing' }
  | { phase: 'creating-room' }
  | { phase: 'ready' }
  | { phase: 'error'; message: string };

interface MessageRow {
  id: string;
  sender: string;
  body: string;
  ts: number;
  isSelf: boolean;
  pending?: boolean;
}

/** Persist (peerMxid -> roomId) so we don't re-create the DM every visit. */
function loadCachedRoomId(selfMxid: string, peerMxid: string): string | null {
  try {
    return localStorage.getItem(`web-dm:${selfMxid}:${peerMxid}`);
  } catch {
    return null;
  }
}
function cacheRoomId(selfMxid: string, peerMxid: string, roomId: string): void {
  try {
    localStorage.setItem(`web-dm:${selfMxid}:${peerMxid}`, roomId);
  } catch {
    /* quota / private-mode: ignore */
  }
}

/** Read account_data `m.direct` (peer -> room ids). */
function readMDirect(client: MatrixClient): Record<string, string[]> {
  const evt = client.getAccountData('m.direct' as any);
  if (!evt) return {};
  const c = evt.getContent() as Record<string, string[]>;
  return c || {};
}

/** Find an existing DM room with `peerMxid` that we're still joined to. */
function findExistingDmRoom(client: MatrixClient, peerMxid: string): Room | null {
  const direct = readMDirect(client);
  const ids = direct[peerMxid] || [];
  for (const id of ids) {
    const r = client.getRoom(id);
    if (r && r.getMyMembership() === 'join') return r;
  }
  // Fallback: scan all joined rooms for a 2-member room containing the peer.
  for (const r of client.getRooms()) {
    if (r.getMyMembership() !== 'join') continue;
    const members = r.getJoinedMembers();
    if (members.length === 2 && members.some((m) => m.userId === peerMxid)) {
      return r;
    }
  }
  return null;
}

/** Mark a freshly-created room as a DM in `m.direct`. */
async function markRoomAsDm(client: MatrixClient, roomId: string, peerMxid: string): Promise<void> {
  const direct = readMDirect(client);
  const current = direct[peerMxid] || [];
  if (!current.includes(roomId)) current.push(roomId);
  direct[peerMxid] = current;
  await client.setAccountData('m.direct' as any, direct as any);
}

export function ChatView({ agentId, onBack }: { agentId: number; onBack: () => void }) {
  const [agent, setAgent] = useState<AgentChatInfo | null>(null);
  const [creds, setCreds] = useState<MatrixCreds | null>(null);
  const [status, setStatus] = useState<Status>({ phase: 'idle' });
  const [messages, setMessages] = useState<MessageRow[]>([]);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  /** mxids of OTHER users currently typing in the active room. */
  const [typing, setTyping] = useState<string[]>([]);

  // Refs (don't trigger re-renders).
  const clientRef = useRef<MatrixClient | null>(null);
  const roomIdRef = useRef<string | null>(null);
  const stoppedRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Sticky-scroll state. When the user is at (or near) the bottom we
  // auto-follow new messages; if they've scrolled up to read history we
  // *don't* yank them down, and instead surface a "jump to bottom" pill
  // that resumes following on click.
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  /** Updated by the onScroll handler; the auto-scroll effect reads it. */
  const stickToBottomRef = useRef(true);

  // 1) Load agent + creds in parallel.
  useEffect(() => {
    setStatus({ phase: 'fetching' });
    // `getAgentChatInfo` works for owners AND members (no secrets in payload).
    Promise.all([api.getAgentChatInfo(agentId), api.getMatrixCreds()])
      .then(([a, c]) => {
        if (!a.matrix_user_id) {
          setStatus({ phase: 'error', message: 'This agent has no Matrix account configured.' });
          return;
        }
        setAgent(a);
        setCreds(c);
      })
      .catch((e) => setStatus({ phase: 'error', message: e.message || String(e) }));
  }, [agentId]);

  // 2) Boot matrix client once we have everything.
  useEffect(() => {
    if (!agent || !creds || !agent.matrix_user_id) return;
    stoppedRef.current = false;

    const client = createClient({
      baseUrl: creds.homeserver,
      accessToken: creds.access_token,
      userId: creds.matrix_user_id,
      deviceId: creds.device_id,
    });
    clientRef.current = client;

    const onTimeline = (
      event: MatrixEvent,
      room: Room | undefined,
      toStartOfTimeline: boolean | undefined,
    ) => {
      if (toStartOfTimeline) return;
      if (event.getType() !== EventType.RoomMessage) return;
      if (!room || room.roomId !== roomIdRef.current) return;
      const content = event.getContent() as { body?: string; msgtype?: string };
      const body = content.body || '';
      if (!body) return;
      const sender = event.getSender() || '?';
      const row: MessageRow = {
        id: event.getId() || `${sender}-${event.getTs()}`,
        sender,
        body,
        ts: event.getTs() || Date.now(),
        isSelf: sender === creds.matrix_user_id,
      };
      setMessages((prev) => {
        // De-dupe (echoes from our own send).
        if (prev.some((m) => m.id === row.id)) return prev;
        // Replace any pending row with the same body from us.
        if (row.isSelf) {
          const idx = prev.findIndex((m) => m.pending && m.body === row.body);
          if (idx !== -1) {
            const copy = [...prev];
            copy[idx] = row;
            return copy;
          }
        }
        return [...prev, row];
      });
      // When the bot sends, drop them from the typing list defensively.
      // (Synapse usually fires typing:false first, but some bots skip it.)
      if (!row.isSelf) {
        setTyping((prev) => prev.filter((u) => u !== sender));
      }
    };

    /**
     * Matrix-js-sdk fires RoomMember.typing whenever m.typing ephemeral
     * events update for a room member. We only care about members of the
     * active DM room, and we filter out our own user.
     */
    const onTyping = (_event: MatrixEvent, member: RoomMember) => {
      if (!member) return;
      if (member.roomId !== roomIdRef.current) return;
      if (member.userId === creds.matrix_user_id) return;
      setTyping((prev) => {
        const isTyping = member.typing;
        const has = prev.includes(member.userId);
        if (isTyping && !has) return [...prev, member.userId];
        if (!isTyping && has) return prev.filter((u) => u !== member.userId);
        return prev;
      });
    };

    setStatus({ phase: 'connecting' });

    (async () => {
      try {
        client.on(RoomEvent.Timeline, onTimeline as any);
        client.on(RoomMemberEvent.Typing, onTyping as any);
        client.on(ClientEvent.Sync, (state) => {
          if (stoppedRef.current) return;
          if (state === 'PREPARED' || state === 'SYNCING') {
            if (status.phase === 'connecting') setStatus({ phase: 'syncing' });
          }
        });

        await client.startClient({ initialSyncLimit: 30 });

        // Wait for initial sync to expose getRooms / account_data.
        await new Promise<void>((resolve, reject) => {
          if (stoppedRef.current) return resolve();
          const t = setTimeout(() => reject(new Error('Initial sync timeout')), 20_000);
          const handler = (state: string) => {
            if (state === 'PREPARED') {
              clearTimeout(t);
              client.removeListener(ClientEvent.Sync as any, handler as any);
              resolve();
            }
          };
          client.on(ClientEvent.Sync, handler as any);
        });

        if (stoppedRef.current) return;

        const peerMxid = agent.matrix_user_id!;
        const selfMxid = creds.matrix_user_id;

        // Find or create the DM room.
        let room: Room | null = findExistingDmRoom(client, peerMxid);
        if (!room) {
          const cachedId = loadCachedRoomId(selfMxid, peerMxid);
          if (cachedId) {
            const r = client.getRoom(cachedId);
            if (r && r.getMyMembership() === 'join') room = r;
          }
        }
        if (!room) {
          setStatus({ phase: 'creating-room' });
          const created = await client.createRoom({
            preset: Preset.TrustedPrivateChat,
            is_direct: true,
            invite: [peerMxid],
            visibility: 'private' as any,
          });
          await markRoomAsDm(client, created.room_id, peerMxid);
          cacheRoomId(selfMxid, peerMxid, created.room_id);
          // Wait briefly for the room to appear in the local store.
          for (let i = 0; i < 50; i++) {
            const r = client.getRoom(created.room_id);
            if (r) {
              room = r;
              break;
            }
            await new Promise((r2) => setTimeout(r2, 100));
          }
          if (!room) throw new Error('Room created but not visible in store yet');
        }

        roomIdRef.current = room.roomId;
        cacheRoomId(selfMxid, peerMxid, room.roomId);

        // Backfill any existing timeline messages.
        const existing: MessageRow[] = [];
        for (const ev of room.getLiveTimeline().getEvents()) {
          if (ev.getType() !== EventType.RoomMessage) continue;
          const content = ev.getContent() as { body?: string };
          const body = content.body || '';
          if (!body) continue;
          const sender = ev.getSender() || '?';
          existing.push({
            id: ev.getId() || `${sender}-${ev.getTs()}`,
            sender,
            body,
            ts: ev.getTs() || Date.now(),
            isSelf: sender === selfMxid,
          });
        }
        existing.sort((a, b) => a.ts - b.ts);
        setMessages(existing);
        setStatus({ phase: 'ready' });
      } catch (e: any) {
        if (!stoppedRef.current) {
          setStatus({ phase: 'error', message: e?.message || String(e) });
        }
      }
    })();

    return () => {
      stoppedRef.current = true;
      try {
        client.removeAllListeners();
        client.stopClient();
      } catch {
        /* ignore */
      }
      clientRef.current = null;
      roomIdRef.current = null;
    };
    // We intentionally exclude `status.phase` — it's only used to update a UI hint.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agent, creds]);

  // Auto-scroll to bottom on new message OR when typing indicator appears,
  // BUT only if the user is already pinned to the bottom. If they've
  // scrolled up to read history we leave them in place — the floating
  // "jump to bottom" button is their way back.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages.length, typing.length]);

  /** Treat "within 80px of the bottom" as still pinned — catches the case
   *  where a new message just bumped the height by a row or two. */
  const NEAR_BOTTOM_PX = 80;

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distanceFromBottom <= NEAR_BOTTOM_PX;
    stickToBottomRef.current = atBottom;
    setShowJumpToBottom(!atBottom);
  }, []);

  const jumpToBottom = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    stickToBottomRef.current = true;
    setShowJumpToBottom(false);
  }, []);

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    const client = clientRef.current;
    const roomId = roomIdRef.current;
    if (!client || !roomId || !creds) return;
    setSending(true);
    const pendingId = `pending-${Date.now()}`;
    setMessages((prev) => [
      ...prev,
      {
        id: pendingId,
        sender: creds.matrix_user_id,
        body: text,
        ts: Date.now(),
        isSelf: true,
        pending: true,
      },
    ]);
    setInput('');
    try {
      await client.sendTextMessage(roomId, text);
      // The Timeline listener will replace the pending row.
    } catch (e: any) {
      // Mark pending row as failed.
      setMessages((prev) =>
        prev.map((m) =>
          m.id === pendingId
            ? { ...m, body: `${m.body}  (failed: ${e.message || e})`, pending: false }
            : m,
        ),
      );
    } finally {
      setSending(false);
    }
  };

  const phaseLabel = useMemo(() => {
    switch (status.phase) {
      case 'idle':
      case 'fetching':
        return 'Loading…';
      case 'connecting':
        return 'Connecting to Matrix…';
      case 'syncing':
        return 'Syncing…';
      case 'creating-room':
        return 'Starting chat…';
      case 'ready':
        return 'Connected';
      case 'error':
        return `Error: ${status.message}`;
    }
  }, [status]);

  return (
    <div className="chat-view">
      <div className="list-head">
        <div>
          <button className="btn btn-ghost btn-sm" onClick={onBack}>← Back</button>
          <h2 style={{ marginTop: 8 }}>
            {agent?.emoji || '🤖'} {agent?.display_name || 'Chat'}
          </h2>
          <div className="agent-id">{agent?.matrix_user_id || ''}</div>
        </div>
        <div className="chat-status">
          <span className={`chat-status-pill ${status.phase === 'ready' ? 'ok' : status.phase === 'error' ? 'err' : 'pending'}`}>
            {phaseLabel}
          </span>
        </div>
      </div>

      <div className="chat-box">
        <div className="chat-messages" ref={scrollRef} onScroll={handleScroll}>
          {messages.length === 0 && status.phase === 'ready' && (
            <div className="chat-empty">Say hi 👋</div>
          )}
          {messages.map((m) => (
            <div key={m.id} className={`chat-msg ${m.isSelf ? 'self' : 'other'} ${m.pending ? 'pending' : ''}`}>
              <div className="chat-msg-body">{m.body}</div>
              <div className="chat-msg-meta">
                {new Date(m.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                {m.pending && ' · sending…'}
              </div>
            </div>
          ))}
          {typing.length > 0 && (
            <div className="chat-msg other chat-typing" aria-live="polite">
              <div className="typing-dots">
                <span /><span /><span />
              </div>
            </div>
          )}
        </div>

        {showJumpToBottom && (
          <button
            type="button"
            className="chat-jump-to-bottom"
            onClick={jumpToBottom}
            aria-label="Jump to latest messages"
            title="Jump to latest"
          >
            ↓
          </button>
        )}

        <form
          className="chat-input"
          onSubmit={(e) => {
            e.preventDefault();
            send();
          }}
        >
          <input
            className="input"
            placeholder={status.phase === 'ready' ? 'Type a message…' : 'Connecting…'}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={status.phase !== 'ready' || sending}
            autoFocus
          />
          <button
            type="submit"
            className="btn btn-primary grow-0"
            disabled={status.phase !== 'ready' || sending || !input.trim()}
          >
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
