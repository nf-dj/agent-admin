import { useEffect, useState } from 'react';
import { api, type AgentSkill, type AgentSkillDetail } from '../api';

/**
 * Owner-only: lists every skill installed in an agent's workspace.
 *
 * Skills are read from ``<workspace>/skills/<name>/SKILL.md`` (canonical
 * OpenClaw layout). Each row shows what we parsed from the manifest's
 * YAML frontmatter; clicking opens a side panel with the full markdown.
 *
 * Markdown is rendered as plain text inside a ``<pre>`` for v1 \u2014 readable
 * and dependency-free. If we ever want pretty rendering we can swap in
 * react-markdown without changing the data shape.
 */
export function SkillsView({
  agentId,
  agentName,
  onBack,
}: {
  agentId: number;
  agentName: string;
  onBack: () => void;
}) {
  const [skills, setSkills] = useState<AgentSkill[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  /** Currently-selected skill name, or null for the list view. */
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentSkillDetail | null>(null);
  const [detailErr, setDetailErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setSkills(null);
    setErr(null);
    api.listAgentSkills(agentId)
      .then((r) => { if (!cancelled) setSkills(r); })
      .catch((e) => { if (!cancelled) setErr(e?.message || 'Failed to load skills'); });
    return () => { cancelled = true; };
  }, [agentId]);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setDetailErr(null);
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailErr(null);
    api.getAgentSkill(agentId, selected)
      .then((r) => { if (!cancelled) setDetail(r); })
      .catch((e) => { if (!cancelled) setDetailErr(e?.message || 'Failed to load skill'); });
    return () => { cancelled = true; };
  }, [agentId, selected]);

  // -- Detail panel ---------------------------------------------------------
  if (selected) {
    return (
      <div className="settings-view">
        <div className="page-header">
          <button className="btn btn-ghost" onClick={() => setSelected(null)}>← Back to skills</button>
          <h2 style={{ margin: 0 }}>{selected}</h2>
        </div>

        {detailErr && <div className="error">{detailErr}</div>}
        {!detail && !detailErr && <div className="muted small">Loading…</div>}

        {detail && (
          <>
            <SkillMeta skill={detail} />
            <p className="muted small" style={{ marginTop: 8 }}>
              Path: <code>{detail.path}/SKILL.md</code>
            </p>
            {/* Plain-text markdown render \u2014 not pretty, but legible and zero JS deps. */}
            <pre className="skill-content">{detail.content}</pre>
          </>
        )}
      </div>
    );
  }

  // -- List view ------------------------------------------------------------
  return (
    <div className="settings-view">
      <div className="page-header">
        <button className="btn btn-ghost" onClick={onBack}>← Back</button>
        <h2 style={{ margin: 0 }}>Skills — {agentName}</h2>
      </div>

      <p className="muted small">
        Skills installed in this bot's workspace under{' '}
        <code>skills/&lt;name&gt;/SKILL.md</code>. Click one to see the full manifest.
      </p>

      {err && <div className="error">{err}</div>}

      {skills === null && !err && <div className="muted small">Loading…</div>}

      {skills && skills.length === 0 && (
        <div className="empty-state" style={{ marginTop: 16 }}>
          <p>No skills yet.</p>
          <p className="muted small">
            Drop a folder containing a <code>SKILL.md</code> file into{' '}
            <code>skills/</code> in this bot's workspace.
          </p>
        </div>
      )}

      {skills && skills.length > 0 && (
        <ul className="member-list" style={{ marginTop: 16 }}>
          {skills.map((s) => (
            <li
              key={s.path}
              className="member-row skill-row"
              onClick={() => setSelected(s.name)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => { if (e.key === 'Enter') setSelected(s.name); }}
            >
              <div className="member-main">
                <div className="member-name">🧰 {s.name}</div>
                {s.description && (
                  <div className="member-email" style={{ whiteSpace: 'normal' }}>
                    {s.description}
                  </div>
                )}
                <SkillMeta skill={s} compact />
              </div>
              <span className="muted small">›</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Small inline metadata strip: version · author · updated. Hides any field
 * that's null so we don't end up with a chain of "·" separators leading nowhere.
 */
function SkillMeta({ skill, compact = false }: { skill: AgentSkill; compact?: boolean }) {
  const parts: string[] = [];
  if (skill.version) parts.push(`v${skill.version}`);
  if (skill.author) parts.push(`by ${skill.author}`);
  if (skill.updated) parts.push(`updated ${skill.updated}`);
  if (parts.length === 0) return null;

  return (
    <div className={`muted ${compact ? 'small' : ''}`} style={{ marginTop: compact ? 2 : 8 }}>
      {parts.join(' · ')}
    </div>
  );
}
