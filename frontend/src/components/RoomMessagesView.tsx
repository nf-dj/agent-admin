import { useEffect, useRef, useState } from 'react';
import { api, type AgentRoomMessage } from '../api';

/**
 * Owner-only: view a single room's message history and reply as the bot.
 *
 * Pulls a window of messages from Matrix on mount (latest 50), renders
 * them as chat bubbles, and lets the owner type a reply that gets sent
 * to the room **as the bot**.
 *
 * Caveat the UI makes explicit: the human chatting on the other side
 * sees the bot's name on every reply, so the owner is effectively
 * impersonating their bot. We show a "Replying as @bot" banner so the
 * owner can't forget what's happening.
 *
 * Not live: each refresh / send re-fetches. Cheaper than long-poll and
 * fine for the audit-and-occasional-reply use case.
 */
export function RoomMessagesView({
  agentId,
  agentName,
  roomId,
  roomLabel,
  onBack,
}: {
  agentId: number;
  agentName: string;
  roomId: string;
  /** Human-friendly room title (the DM partner name, group name, etc.). */
  roomLabel: string;
  onBack: () => void;
}) {
  const [messages, setMessages] = useState<AgentRoomMessage[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [prevToken, setPrevToken] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);

  // Compose box state
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [sendErr, setSendErr] = useState<string | null>(null);

  /** Scroll-to-bottom anchor so new messages stay in view. */
  const bottomRef = useRef<HTMLDivElement | null>(null);

  /** Initial / refresh load. Replaces the whole list and resets pagination. */
  async function load() {
    setErr(null);
    setMessages(null);
    try {
      const r = await api.getRoomMessages(agentId, roomId, { limit: 50 });
      setMessages(r.messages);
      setPrevToken(r.prev_token);
      setHasMore(r.has_more);
      // Defer scroll to next paint so the DOM has the new content.
      requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ block: 'end' }));
    } catch (e: any) {
      setErr(e?.message || 'Failed to load messages');
    }
  }

  useEffect(() => { void load(); /* eslint-disable-next-line */ }, [agentId, roomId]);

  /** Paginate backwards. Prepends older messages and keeps scroll position
   *  roughly stable (we don't bother with anchor math — the user is
   *  reading old stuff, so jumping to the top is acceptable). */
  async function loadOlder() {
    if (!prevToken || loadingOlder) return;
    setLoadingOlder(true);
    setErr(null);
    try {
      const r = await api.getRoomMessages(agentId, roomId, { limit: 50, from: prevToken });
      setMessages((m) => (m ? [...r.messages, ...m] : r.messages));
      setPrevToken(r.prev_token);
      setHasMore(r.has_more);
    } catch (e: any) {
      setErr(e?.message || 'Failed to load older messages');
    } finally {
      setLoadingOlder(false);
    }
  }

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setSendErr(null);
    try {
      await api.sendRoomMessage(agentId, roomId, text);
      setDraft('');
      // Re-fetch so we pick up the new event with its real event_id +
      // server timestamp. Cheap (50 events) and avoids drift between
      // optimistic local state and what Matrix actually shows.
      await load();
    } catch (e: any) {
      setSendErr(e?.message || 'Failed to send');
    } finally {
      setSending(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Enter = send, Shift+Enter = newline. Matches Telegram / Slack convention.
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  return (
    <div className="room-view">
      <div className="page-header">
        <button className="btn btn-ghost" onClick={onBack}>← Back</button>
        <div style={{ flex: 1, minWidth: 0 }}>
          <h2 style={{ margin: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {roomLabel}
          </h2>
          <div className="muted small">{agentName}</div>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => void load()} title="Refresh">
          ↻
        </button>
      </div>

      {/* Reminder that messages you type here are sent as the bot, not as you. */}
      <div className="reply-as-banner">
        💡 You're replying <strong>as the bot</strong>. The other party sees it from {agentName}, not from you.
      </div>

      {err && <div className="error">{err}</div>}

      <div className="message-list">
        {messages === null && !err && (
          <div className="muted small" style={{ padding: 24, textAlign: 'center' }}>Loading…</div>
        )}

        {messages && hasMore && (
          <div style={{ textAlign: 'center', padding: '8px 0' }}>
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => void loadOlder()}
              disabled={loadingOlder}
            >
              {loadingOlder ? 'Loading…' : 'Load older messages'}
            </button>
          </div>
        )}

        {messages && messages.length === 0 && (
          <div className="muted small" style={{ padding: 24, textAlign: 'center' }}>
            No messages yet. Be the first.
          </div>
        )}

        {messages && messages.map((m) => (
          <MessageBubble key={m.event_id} msg={m} />
        ))}

        <div ref={bottomRef} />
      </div>

      <div className="compose">
        {sendErr && <div className="error" style={{ marginBottom: 8 }}>{sendErr}</div>}
        <textarea
          className="compose-input"
          placeholder={`Message as ${agentName}…`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={2}
          maxLength={4000}
          disabled={sending}
        />
        <button
          className="btn btn-primary"
          onClick={() => void send()}
          disabled={sending || !draft.trim()}
        >
          {sending ? 'Sending…' : 'Send'}
        </button>
      </div>
    </div>
  );
}

/** Single chat bubble. Right-aligned + accent-tinted when sent by the bot. */
function MessageBubble({ msg }: { msg: AgentRoomMessage }) {
  const time = new Date(msg.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const isNonText = msg.msgtype !== 'm.text' && msg.msgtype !== 'm.notice' && msg.msgtype !== 'm.emote';

  return (
    <div className={`bubble-row ${msg.is_bot ? 'bubble-row-bot' : ''}`}>
      <div className={`bubble ${msg.is_bot ? 'bubble-bot' : ''}`}>
        {!msg.is_bot && <div className="bubble-sender">{msg.sender_name}</div>}
        <div className={`bubble-body ${isNonText ? 'bubble-body-meta' : ''}`}>{msg.body}</div>
        <div className="bubble-time">{time}</div>
      </div>
    </div>
  );
}
