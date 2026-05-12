// API client for the Agent Admin backend.

export interface User {
  id: number;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  created_at: string;
  /** Optional per-user company prefix used when naming new agents. */
  company_prefix: string | null;
}

export interface UserSettingsUpdate {
  display_name?: string | null;
  /** Pass empty string to clear; null/undefined leaves untouched. */
  company_prefix?: string | null;
}

export interface UserApiKey {
  provider: string;
  has_key: boolean;
  preview: string | null;
  updated_at: string | null;
}

export interface AgentApiKey {
  provider: string;
  has_override: boolean;
  override_preview: string | null;
  override_updated_at: string | null;
  user_has_saved: boolean;
  user_saved_preview: string | null;
}

/**
 * One skill installed in an agent's workspace, parsed from the YAML
 * frontmatter of ``<workspace>/skills/<name>/SKILL.md``. All metadata
 * fields are optional because real-world SKILL.md files vary.
 */
export interface AgentSkill {
  name: string;
  description: string | null;
  version: string | null;
  author: string | null;
  updated: string | null;
  path: string;
}

export interface AgentSkillDetail extends AgentSkill {
  /** Full SKILL.md content as raw markdown source. */
  content: string;
}

/**
 * One message in a bot's room timeline. Plain text only — ``msgtype``
 * may indicate non-text events (m.image, m.file), in which case ``body``
 * falls back to a bracketed placeholder. ``is_bot`` lets the UI right-
 * align bot messages and style them as 'me' vs 'them'.
 */
export interface AgentRoomMessage {
  event_id: string;
  sender: string;
  sender_name: string;
  body: string;
  msgtype: string;
  ts: number;
  is_bot: boolean;
}

export interface AgentRoomMessages {
  messages: AgentRoomMessage[];
  prev_token: string | null;
  has_more: boolean;
}

export interface AgentRoomSendResult {
  event_id: string;
  sent_at: number;
}

/**
 * One Matrix room the bot is currently joined to. ``is_dm`` is true when
 * the room has exactly two members — a Matrix convention for DMs.
 */
export interface AgentRoom {
  room_id: string;
  name: string;
  member_count: number;
  is_dm: boolean;
  other_user_id: string | null;
}

export type AgentRole = 'owner' | 'member';

export interface Agent {
  id: number;
  harness: string;
  harness_agent_id: string;
  display_name: string;
  model: string | null;
  emoji: string | null;
  has_telegram: boolean;
  matrix_user_id: string | null;
  workspace_path: string | null;
  created_at: string;
  updated_at: string;
  /** Current user's role on this agent. */
  my_role: AgentRole;
  /** Number of Matrix rooms the bot is currently joined to. Only populated
   *  for owned agents (members don't see this). ``null`` means "don't know"
   *  — no Matrix account, lookup failed, or member view. */
  room_count: number | null;
  /** Number of skills found in the agent's workspace. Same conventions as
   *  ``room_count`` — owner-only, null means hide the badge. */
  skill_count: number | null;
}

export interface AgentDetail extends Agent {
  system_prompt: string | null;
  runtime: any | null;
}

/** Slim agent info available to members; safe to expose without leaking secrets. */
export interface AgentChatInfo {
  id: number;
  display_name: string;
  emoji: string | null;
  matrix_user_id: string | null;
  my_role: AgentRole;
}

export interface AgentMember {
  user_id: number;
  email: string;
  display_name: string | null;
  role: AgentRole;
  created_at: string;
}

export interface Model {
  id: string;
  name: string;
  provider: string;
}

/** One model definition inside a custom provider's ``models`` array. */
export interface CustomModelDef {
  id: string;
  name: string;
  reasoning?: boolean;
  input?: string[];
  cost?: { input: number; output: number };
  contextWindow?: number;
  maxTokens?: number;
  compat?: Record<string, any> | null;
}

/** User-owned custom LLM provider (BYO endpoint). */
export interface CustomProvider {
  id: number;
  slug: string;
  display_name: string;
  base_url: string;
  api_type: string;
  has_api_key: boolean;
  api_key_preview: string | null;
  models: CustomModelDef[];
  /** Namespaced id as it appears in ocplatform.json, e.g. ``u3-nucbox-llama``. */
  namespaced_id: string;
  created_at: string;
  updated_at: string;
}

export interface CustomProviderCreate {
  slug: string;
  display_name: string;
  base_url: string;
  api_type: string;
  api_key?: string | null;
  models?: CustomModelDef[];
}

export interface CustomProviderUpdate {
  display_name?: string;
  base_url?: string;
  api_type?: string;
  api_key?: string;
  /** Pass true to wipe the stored key. */
  clear_api_key?: boolean;
  models?: CustomModelDef[];
}

export interface ProviderTestResult {
  ok: boolean;
  status_code: number | null;
  latency_ms: number | null;
  discovered_models: string[] | null;
  error: string | null;
  endpoint: string | null;
}

export interface Harness {
  name: string;
  display_name: string;
  available: boolean;
}

async function req<T = any>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...opts,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(opts.headers || {}),
    },
  });
  const ct = res.headers.get('content-type') || '';
  const data = ct.includes('application/json') ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = typeof data === 'object' && data?.detail
      ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      : `HTTP ${res.status}`;
    throw new Error(msg);
  }
  return data as T;
}

export const api = {
  // Auth
  me: () => req<{ authenticated: boolean; user?: User; allow_signup?: boolean }>('/api/auth/me'),
  signup: (body: { email: string; password: string; display_name?: string }) =>
    req<User>('/api/auth/signup', { method: 'POST', body: JSON.stringify(body) }),
  login: (body: { email: string; password: string }) =>
    req<User>('/api/auth/login', { method: 'POST', body: JSON.stringify(body) }),
  logout: () => req<{ ok: true }>('/api/auth/logout', { method: 'POST' }),

  // Metadata
  models: (harness = 'openclaw') => req<Model[]>(`/api/models?harness=${encodeURIComponent(harness)}`),
  harnesses: () => req<Harness[]>('/api/harnesses'),

  // Agents
  listAgents: () => req<Agent[]>('/api/agents'),
  getAgent: (id: number) => req<AgentDetail>(`/api/agents/${id}`),
  getAgentRuntime: (id: number) => req<any>(`/api/agents/${id}/runtime`),
  createAgent: (body: {
    display_name: string;
    slug?: string;
    model?: string;
    emoji?: string;
    system_prompt?: string;
    telegram_bot_token?: string;
    harness?: string;
    /** API key for DeepSeek, OpenAI, Anthropic-direct, etc. */
    provider_api_key?: string;
    /** Claude.ai subscription session key (sk-ant-sid01-…). */
    claude_subscription_key?: string;
  }) => req<AgentDetail>('/api/agents', { method: 'POST', body: JSON.stringify(body) }),
  updateAgent: (id: number, body: {
    display_name?: string;
    model?: string;
    emoji?: string;
    system_prompt?: string;
    telegram_bot_token?: string;
  }) => req<AgentDetail>(`/api/agents/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteAgent: (id: number) => req<{ ok: true }>(`/api/agents/${id}`, { method: 'DELETE' }),
  resyncAgent: (id: number) => req<{ ok: true; op: string }>(`/api/agents/${id}/sync`, { method: 'POST' }),

  // Slim agent info available to members (no secrets) — used by ChatView.
  getAgentChatInfo: (id: number) => req<AgentChatInfo>(`/api/agents/${id}/chat-info`),

  // Member management
  listMembers: (agentId: number) => req<AgentMember[]>(`/api/agents/${agentId}/members`),
  inviteMember: (agentId: number, email: string) =>
    req<AgentMember>(`/api/agents/${agentId}/members`, {
      method: 'POST', body: JSON.stringify({ email }),
    }),
  removeMember: (agentId: number, userId: number) =>
    req<{ ok: true }>(`/api/agents/${agentId}/members/${userId}`, { method: 'DELETE' }),

  // Web-chat: lazily provisions a Matrix account for the logged-in user
  // and returns credentials the browser uses with matrix-js-sdk.
  getMatrixCreds: () => req<MatrixCreds>('/api/me/matrix-creds'),

  getSettings: () => req<User>('/api/me/settings'),
  updateSettings: (body: UserSettingsUpdate) =>
    req<User>('/api/me/settings', { method: 'PATCH', body: JSON.stringify(body) }),

  // --- API keys ---
  listApiKeys: () => req<UserApiKey[]>('/api/me/api-keys'),
  setApiKey: (provider: string, api_key: string) =>
    req<UserApiKey>(`/api/me/api-keys/${encodeURIComponent(provider)}`, {
      method: 'PUT', body: JSON.stringify({ api_key }),
    }),
  deleteApiKey: (provider: string) =>
    req<UserApiKey>(`/api/me/api-keys/${encodeURIComponent(provider)}`, { method: 'DELETE' }),

  // --- Per-agent API key overrides (owner-only) ---
  listAgentRooms: (agentId: number) =>
    req<AgentRoom[]>(`/api/agents/${agentId}/rooms`),

  getRoomMessages: (agentId: number, roomId: string,
                    opts: { limit?: number; from?: string | null } = {}) => {
    const qs = new URLSearchParams();
    if (opts.limit) qs.set('limit', String(opts.limit));
    if (opts.from) qs.set('from', opts.from);
    const q = qs.toString();
    return req<AgentRoomMessages>(
      `/api/agents/${agentId}/rooms/${encodeURIComponent(roomId)}/messages${q ? `?${q}` : ''}`
    );
  },

  sendRoomMessage: (agentId: number, roomId: string, text: string) =>
    req<AgentRoomSendResult>(
      `/api/agents/${agentId}/rooms/${encodeURIComponent(roomId)}/send`,
      { method: 'POST', body: JSON.stringify({ text }) }
    ),

  listAgentSkills: (agentId: number) =>
    req<AgentSkill[]>(`/api/agents/${agentId}/skills`),

  getAgentSkill: (agentId: number, skillName: string) =>
    req<AgentSkillDetail>(`/api/agents/${agentId}/skills/${encodeURIComponent(skillName)}`),

  listAgentApiKeys: (agentId: number) =>
    req<AgentApiKey[]>(`/api/agents/${agentId}/api-keys`),
  setAgentApiKey: (agentId: number, provider: string, api_key: string) =>
    req<AgentApiKey>(`/api/agents/${agentId}/api-keys/${encodeURIComponent(provider)}`, {
      method: 'PUT', body: JSON.stringify({ api_key }),
    }),
  deleteAgentApiKey: (agentId: number, provider: string) =>
    req<AgentApiKey>(`/api/agents/${agentId}/api-keys/${encodeURIComponent(provider)}`, {
      method: 'DELETE',
    }),

  // --- Custom providers (BYO LLM endpoints) ---
  listCustomProviders: () =>
    req<CustomProvider[]>('/api/me/providers'),
  createCustomProvider: (body: CustomProviderCreate) =>
    req<CustomProvider>('/api/me/providers', {
      method: 'POST', body: JSON.stringify(body),
    }),
  updateCustomProvider: (id: number, body: CustomProviderUpdate) =>
    req<CustomProvider>(`/api/me/providers/${id}`, {
      method: 'PATCH', body: JSON.stringify(body),
    }),
  deleteCustomProvider: (id: number) =>
    req<void>(`/api/me/providers/${id}`, { method: 'DELETE' }),
  testCustomProvider: (id: number) =>
    req<ProviderTestResult>(`/api/me/providers/${id}/test`, { method: 'POST' }),
  testCustomProviderPayload: (body: {
    base_url: string;
    api_type: string;
    api_key?: string | null;
  }) =>
    req<ProviderTestResult>('/api/me/providers/test', {
      method: 'POST', body: JSON.stringify(body),
    }),

  // --- WhatsApp bridge ---
  whatsappStatus: () =>
    req<WhatsAppStatus>('/api/me/whatsapp/status'),
  whatsappListLogins: () =>
    req<WhatsAppLogin[]>('/api/me/whatsapp/logins'),
  whatsappStartLogin: (flow_id: string) =>
    req<WhatsAppLoginStep>('/api/me/whatsapp/login/start', {
      method: 'POST', body: JSON.stringify({ flow_id }),
    }),
  whatsappLoginStep: (body: {
    login_id: string;
    step_id: string;
    action?: string;
    payload?: Record<string, unknown> | null;
  }, signal?: AbortSignal) =>
    req<WhatsAppLoginStep>('/api/me/whatsapp/login/step', {
      method: 'POST',
      body: JSON.stringify({ action: 'display_and_wait', ...body }),
      signal,
    }),
  whatsappDeleteLogin: (login_id: string) =>
    req<{ ok: boolean; detached_bots: number }>(
      `/api/me/whatsapp/logins/${encodeURIComponent(login_id)}`,
      { method: 'DELETE' },
    ),
};

// --- WhatsApp bridge types ---
export interface WhatsAppStatus {
  configured: boolean;
  mxid?: string | null;
  flows?: Array<{ id: string; name: string; description?: string }>;
}

export interface WhatsAppLogin {
  id: string;
  name?: string | null;
  profile?: Record<string, unknown> | null;
  state?: Record<string, unknown> | null;
}

/** Shape returned by /login/start and /login/step. Mirrors mautrix bridgev2. */
export interface WhatsAppLoginStep {
  login_id: string;
  step_id: string;
  instructions?: string;
  type:
    | 'display_and_wait'
    | 'user_input'
    | 'cookies'
    | 'complete'
    | string;
  /** When type === 'display_and_wait', the thing to render. */
  display_and_wait?: {
    type: 'qr' | 'code' | 'emoji' | string;
    data: string;
    image_url?: string;
  };
  /** When type === 'user_input', the fields to collect from the user. */
  user_input?: {
    fields: Array<{ type: string; id: string; name: string; description?: string }>;
  };
  /** When type === 'complete'. */
  complete?: {
    user_login_id?: string;
    user_login?: WhatsAppLogin;
  };
}

export interface MatrixCreds {
  matrix_user_id: string;
  homeserver: string;
  access_token: string;
  device_id: string;
}
