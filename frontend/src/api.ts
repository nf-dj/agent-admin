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
};

export interface MatrixCreds {
  matrix_user_id: string;
  homeserver: string;
  access_token: string;
  device_id: string;
}
